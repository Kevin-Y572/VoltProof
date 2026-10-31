"""pipeline 层结果缓存：相同请求直接返回完整证据，不重跑 LLM 与仿真。

用途：
  - 演示防翻车：演示前预跑一遍，现场同问题秒回
  - 省钱省时：重复请求零成本

规则：
  - 有 validators 时不缓存（跑批要真实测量，不能吃缓存）
  - 只缓存 ok=True 的结果
  - key = sha256(请求 + 上一轮网表 + backend + 模型)——多轮会话链各自独立缓存
  - 默认开启（VOLTPROOF_CACHE=0 关闭，调试/对比实验时用）
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_LEGACY_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
_ENABLED = os.environ.get("VOLTPROOF_CACHE", "1") != "0"


def _key(request: str, previous_netlist: str | None, backend: str, context=None) -> str:
    from . import llm
    from .ngspice_runner import _NGSPICE
    try:
        stamp = Path(_NGSPICE).stat().st_mtime_ns
    except OSError:
        stamp = 0
    code_hash = hashlib.sha256(b"".join((Path(__file__).parent / name).read_bytes()
        for name in ("measure.py", "specs.py", "prompts.py", "ngspice_runner.py"))).hexdigest()
    raw = json.dumps(["proof-v2", llm._MODEL, llm.current_config().get("base_url"), backend,
                      request, previous_netlist or "", _NGSPICE, stamp, code_hash, context], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(request: str, previous_netlist: str | None, backend: str,
        cache_dir: Path | None = None, context=None) -> dict | None:
    if not _ENABLED:
        return None
    f = (cache_dir or _LEGACY_CACHE_DIR) / f"{_key(request, previous_netlist, backend, context)}.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("ok") and data.get("validation", {}).get("measurement") == "passed":
            data["from_cache"] = True
            return data
    except (json.JSONDecodeError, OSError):
        return None
    return None


def put(request: str, previous_netlist: str | None, backend: str, evidence: dict,
        cache_dir: Path | None = None, context=None) -> None:
    if not _ENABLED or not evidence.get("ok"):
        return
    base = cache_dir or _LEGACY_CACHE_DIR
    base.mkdir(parents=True, exist_ok=True)
    f = base / f"{_key(request, previous_netlist, backend, context)}.json"
    try:
        f.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 磁盘满等异常不阻塞主管线
