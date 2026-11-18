"""PCB 候选/采用/过期：per-工作区持久化（方案 §6.1/§6.2）。

存储：<ws>/.cp/pcb_versions/<id>.json（不可变候选）与 .cp/pcb_current.json
（当前采用指针，含 revision）。语义与 app/project.py 一致：
- 候选不可变（迟到任务只能留候选，不得覆盖用户已采用的板）；
- 采用/回退带 revision 乐观并发，冲突抛 project.Conflict；
- 过期判定用电气内容哈希对比当前源电路网表，不凭版本号。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .. import i18n
from ..project import Conflict  # 与主项目同一冲突语义，前端提示一致
from . import schema

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock(ws) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(str(ws.root), threading.RLock())


def _versions_dir(ws) -> Path:
    return ws.cp_dir / "pcb_versions"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def save_candidate(ws, version: dict) -> str:
    """保存不可变候选版本，返回其 id。同 id 重复保存是幂等的刷新（补哈希）。"""
    schema.validate_version(version)
    vid = version["pcb_version_id"]
    path = _versions_dir(ws) / f"{vid}.json"
    with _lock(ws):
        if path.exists():
            old = json.loads(path.read_text(encoding="utf-8"))
            version = {**old, **{k: v for k, v in version.items() if v is not None}}
        _atomic_write(path, version)
    return vid


def get_version(ws, version_id: str) -> dict | None:
    if not version_id or any(c in version_id for c in "/\\.."):
        return None
    path = _versions_dir(ws) / f"{version_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_versions(ws, limit: int = 50) -> list[dict]:
    d = _versions_dir(ws)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def current(ws) -> dict | None:
    """当前采用指针 {version_id, revision, adopted_at}；未采用返回 None。"""
    path = ws.cp_dir / "pcb_current.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def adopt(ws, version_id: str, expected_revision: int | None = None,
          require_fresh_source: bool = True) -> dict:
    """采用候选为当前板。双重修订保护（验收 F06 / 方案 §6.2）：

    - expected_revision 不匹配 → Conflict（当前板指针的乐观并发）；
    - require_fresh_source：候选绑定的电气内容哈希必须与项目当前已验证
      网表一致，否则 Conflict（源电路在任务途中/之后变化，迟到结果不得
      冒充当前电路的板）。
    """
    version = get_version(ws, version_id)
    if version is None:
        raise ValueError(i18n.t("api.pcb_candidate_missing", id=version_id))
    if require_fresh_source and version.get("status") not in (schema.CHECKED,):
        raise Conflict(i18n.t("api.pcb_status", status=version.get("status")))
    if require_fresh_source:
        from .. import project as _project
        try:
            now_netlist = _project.active_netlist(ws.load_project(), verified=True)
        except ValueError as e:
            raise Conflict(i18n.t("api.pcb_source", msg=e)) from e
        if now_netlist.strip() and schema.electrical_hash(now_netlist) != \
                version.get("source_netlist_hash"):
            raise Conflict(i18n.t("api.pcb_stale_adopt"))
    with _lock(ws):
        cur = current(ws)
        revision = (cur or {}).get("revision", 0) + 1
        if expected_revision is not None and (cur or {}).get("revision", 0) != expected_revision:
            raise Conflict(i18n.t("api.pcb_pointer"))
        pointer = {"version_id": version_id, "revision": revision,
                   "adopted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _atomic_write(ws.cp_dir / "pcb_current.json", pointer)
        return pointer


def update_version_status(ws, version_id: str, **fields) -> None:
    """回写版本记录的补充字段（复核结果等）。save_candidate 的合并语义
    保证非 None 字段覆盖、其余保留。"""
    version = get_version(ws, version_id)
    if version is None:
        return
    version.update(fields)
    save_candidate(ws, version)


def is_stale(ws, netlist_now: str) -> dict:
    """当前采用的板对现在的源电路是否过期（电气内容哈希口径）。"""
    cur = current(ws)
    if cur is None:
        return {"stale": False, "reason": i18n.t("api.pcb_stale_none")}
    version = get_version(ws, cur["version_id"])
    if version is None:
        return {"stale": True, "reason": i18n.t("api.pcb_stale_file")}
    now_hash = schema.electrical_hash(netlist_now)
    if now_hash != version.get("source_netlist_hash"):
        return {"stale": True, "reason": i18n.t("api.pcb_stale_hash"),
                "pcb_version_id": cur["version_id"]}
    return {"stale": False, "reason": i18n.t("api.pcb_fresh"), "pcb_version_id": cur["version_id"]}
