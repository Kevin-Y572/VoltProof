"""项目草稿、不可变候选版本与乐观并发控制。"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class Conflict(ValueError):
    pass


def lock(ws):
    with _locks_guard:
        return _locks.setdefault(str(ws.root), threading.RLock())


def digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def revision(p: dict) -> int:
    return int(p.get("revision", 0))


def _check(p, expected):
    if expected is not None and revision(p) != expected:
        raise Conflict("项目已被其他操作更新，请重新载入后再应用；当前结果已保留为候选版本")


def active_netlist(p: dict, verified: bool = False) -> str:
    if p.get("dirty"):
        if verified:
            raise Conflict("画布已修改，尚未重新验证。请先仿真，再运行扫描或解读")
        return p.get("draft_netlist") or ""
    return p.get("netlist") or ""


def update_draft(ws, *, name=None, canvas=None, analysis=None, draft_netlist=None, expected=None):
    with lock(ws):
        p = ws.load_project()
        _check(p, expected)
        before = copy.deepcopy(p)
        if name is not None:
            p["name"] = name[:80]
        changed = False
        for key, value in (("canvas", canvas), ("analysis", analysis)):
            if value is not None and edit_digest(key, p.get(key)) != edit_digest(key, value):
                p[key] = value
                changed = True
        if changed:
            p["dirty"] = True
            p["draft_netlist"] = draft_netlist or ""
        elif p.get("dirty") and draft_netlist is not None:
            p["draft_netlist"] = draft_netlist
        if p != before:
            p["revision"] = revision(p) + 1
            p["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            ws.save_project(p)
        return p


def edit_digest(key, value):
    value = copy.deepcopy(value)
    if key == "canvas" and isinstance(value, dict):
        for items in value.values():
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        item.pop("id", None)
        value.pop("analysis", None)
    if key == "analysis" and isinstance(value, dict):
        fields = {"op": ("type",), "tran": ("type", "tstop"), "ac": ("type", "fstart", "fstop")}
        value = {k: str(value.get(k, "")) for k in fields.get(value.get("type"), tuple(value))}
    return digest(value)


def _version_path(ws, version_id):
    if not isinstance(version_id, str) or len(version_id) > 64 or not version_id or any(c not in "0123456789abcdef" for c in version_id):
        raise ValueError("电路版本编号非法")
    return ws.resolve(Path(".cp") / "versions" / f"{version_id}.json")


def read_version(ws, version_id):
    path = _version_path(ws, version_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError("电路版本不存在或已损坏") from e


def candidate(ws, evidence: dict, canvas, base_revision: int, *, analysis=...) -> dict:
    version_id = uuid.uuid4().hex
    p = ws.load_project()
    v = {"id": version_id, "parent": p.get("version_id"), "base_revision": base_revision,
         "netlist": evidence.get("netlist", ""), "netlist_hash": digest(evidence.get("netlist", "")),
         "canvas": canvas, "analysis": ((canvas or {}).get("analysis") or p.get("analysis")) if analysis is ... else analysis,
         "requirements": evidence.get("requirements", []), "evidence_id": evidence.get("task_id", ""),
         "verdict": evidence.get("verdict", "simulated"), "ok": bool(evidence.get("ok")),
         "created": time.strftime("%Y-%m-%d %H:%M:%S"), "source": evidence.get("source", "")}
    f = _version_path(ws, version_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")
    return v


def apply(ws, version_id: str, expected=None) -> dict:
    with lock(ws):
        v = read_version(ws, version_id)
        if not v.get("ok"):
            raise ValueError("此候选版本未完成验证，不能采用")
        p = ws.load_project()
        _check(p, v["base_revision"])
        _check(p, expected)
        p.update({"netlist": v["netlist"], "canvas": v.get("canvas"), "analysis": v.get("analysis"),
                  "requirements": v.get("requirements", []), "evidence_id": v.get("evidence_id"),
                  "version_id": version_id, "netlist_hash": v["netlist_hash"], "dirty": False,
                  "requirements_pending": False,
                  "draft_netlist": None, "revision": revision(p) + 1,
                  "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
        ws.save_project(p)
        return p


def restore(ws, version_id: str, expected: int, requirements=None) -> dict:
    # 恢复也创建新版本，保留历史来源和完整证据关系。
    if not version_id:
        with lock(ws):
            p = ws.load_project()
            _check(p, expected)
            p.update(netlist="", canvas={"comps": [], "wires": [], "labels": []}, analysis=None,
                     requirements=requirements or [], evidence_id="", version_id="", netlist_hash="", dirty=False,
                     requirements_pending=bool(requirements), draft_netlist=None,
                     revision=revision(p) + 1, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
            ws.save_project(p)
            return p
    v = read_version(ws, version_id)
    with lock(ws):
        _check(ws.load_project(), expected)
        restored = candidate(ws, {"netlist": v["netlist"], "requirements": v.get("requirements", []),
                                  "task_id": v.get("evidence_id"), "verdict": v.get("verdict"),
                                  "ok": v.get("ok"), "source": "restore"}, v.get("canvas"), expected,
                             analysis=v.get("analysis"))
        p=apply(ws, restored["id"], expected)
        if requirements is not None and requirements != p.get("requirements", []):
            p.update(requirements=requirements, requirements_pending=True, dirty=True,
                     draft_netlist=p.get("netlist"), revision=revision(p)+1)
            ws.save_project(p)
        return p


def list_versions(ws) -> list[dict]:
    base = ws.resolve(Path(".cp") / "versions")
    if not base.is_dir():
        return []
    versions = []
    for f in base.glob("*.json"):
        try:
            v = json.loads(f.read_text(encoding="utf-8"))
            versions.append({k: v.get(k) for k in ("id", "parent", "created", "source", "evidence_id", "verdict", "base_revision")})
        except (OSError, ValueError):
            continue
    return sorted(versions, key=lambda v: v.get("created") or "", reverse=True)


def compare(ws, left_id, right_id):
    import difflib
    left, right = read_version(ws, left_id), read_version(ws, right_id)
    from .pipeline import _load_archived_evidence
    a = _load_archived_evidence(ws, left.get("evidence_id", "")) or {}
    b = _load_archived_evidence(ws, right.get("evidence_id", "")) or {}
    def measurements(ev):
        return {(c.get("metric"), c.get("out"), c.get("in"), c.get("name")):c for c in ev.get("checks", [])}
    aa, bb = measurements(a), measurements(b)
    rows=[]
    for key in dict.fromkeys([*aa, *bb]):
        ac, bc=aa.get(key, {}), bb.get(key, {})
        av, bv=ac.get("measured"), bc.get("measured")
        rows.append({"name":(bc or ac).get("name"),"unit":(bc or ac).get("unit", ""),
                     "left":av,"right":bv,"delta":bv-av if av is not None and bv is not None else None})
    return {"left_id":left_id,"right_id":right_id,"measurements":rows,
            "left_verdict":a.get("verdict", "incomplete"),"right_verdict":b.get("verdict", "incomplete"),
            "note":"比较的是各版本已保存的证据；没有重新统一输入、负载与测量工况，请同时核对各版本验收条件。",
            "changes":"\n".join(difflib.unified_diff(left.get("netlist", "").splitlines(),right.get("netlist", "").splitlines(),
                 fromfile=left_id[:8],tofile=right_id[:8],lineterm=""))}


def ensure_version(ws) -> dict:
    """首次访问时为旧项目建立快照，确保首次 AI 应用也能完整撤销。"""
    with lock(ws):
        p = ws.load_project()
        if p.get("netlist") and not p.get("version_id"):
            v = candidate(ws, {"netlist": p["netlist"], "requirements": p.get("requirements", []),
                               "ok": True, "verdict": "simulated", "source": "legacy",
                               "task_id": p.get("evidence_id", "")}, p.get("canvas"), revision(p))
            p["version_id"] = v["id"]
            p["netlist_hash"] = v["netlist_hash"]
            ws.save_project(p)
        return p
