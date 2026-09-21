"""pipeline 层结果缓存：相同请求直接返回完整证据，不重跑 LLM 与仿真。

用途（路线图 2.6 规划项）：
  - 演示防翻车：演示前预跑一遍，现场同问题秒回
  - 省钱省时：重复请求零成本

规则：
  - 有 validators 时不缓存（跑批要真实测量，不能吃缓存）
  - 只缓存 ok=True 的结果
  - key = sha256(请求 + 上一轮网表 + backend + 模型)——多轮会话链各自独立缓存
  - 默认开启（CIRCUITPILOT_CACHE=0 关闭，调试/对比实验时用）
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
_ENABLED = os.environ.get("CIRCUITPILOT_CACHE", "1") != "0"


def _key(request: str, previous_netlist: str | None, backend: str) -> str:
    from . import llm
    raw = f"{llm._MODEL}|{backend}|{request}|{previous_netlist or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(request: str, previous_netlist: str | None, backend: str) -> dict | None:
    if not _ENABLED:
        return None
    f = _CACHE_DIR / f"{_key(request, previous_netlist, backend)}.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("ok"):
            data["from_cache"] = True
            return data
    except (json.JSONDecodeError, OSError):
        return None
    return None


def put(request: str, previous_netlist: str | None, backend: str, evidence: dict) -> None:
    if not _ENABLED or not evidence.get("ok"):
        return
    _CACHE_DIR.mkdir(exist_ok=True)
    f = _CACHE_DIR / f"{_key(request, previous_netlist, backend)}.json"
    try:
        f.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 磁盘满等异常不阻塞主管线
