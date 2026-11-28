"""PCB 版本记录的构造、校验与三类哈希（方案 §6.1）。

版本记录至少绑定：源电路版本（电气内容哈希）、器件映射哈希、规则哈希、
工具版本、任务输入、最终板文件哈希。过期判定用【电气内容哈希】而不是
版本号——注释/分析语句变化不该把已布的板判过期（方案 §6.1 原文）。

状态机（§6.2）：NEEDS_MAPPING → READY → ROUTED → CHECKED，
另有 FAILED / CANCELLED / PARTIAL / STALE。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid

SCHEMA_VERSION = 1

NEEDS_MAPPING = "needs_mapping"
READY = "ready"
ROUTED = "routed"
CHECKED = "checked"
RECHECK_FAILED = "recheck_failed"   # 回导复核未过：导出必须拒绝（验收 F04）
FAILED = "failed"
CANCELLED = "cancelled"
PARTIAL = "partial"
STALE = "stale"

_TERMINAL_OK = {CHECKED}
_STATES = {NEEDS_MAPPING, READY, ROUTED, CHECKED, RECHECK_FAILED,
           FAILED, CANCELLED, PARTIAL, STALE}


def _sha(value) -> str:
    text = value if isinstance(value, str) else json.dumps(
        value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# 电气内容 = 全部元件行（含完整值/参数写法）+ 器件模型定义；不含注释、
# 标题、分析语句与控制块。验收 F06：曾只哈希"位号/类型/单 token 值"，
# DC 5→DC 50、.model 参数变化都测不出来。
_DEFN_PREFIXES = (".model", ".subckt", ".include", ".lib", ".param", ".func")


def electrical_hash(netlist: str) -> str:
    """电气内容哈希：元件完整语义 + 模型定义，与注释/分析语句/空白无关。"""
    from .. import checks as _checks
    lines = netlist.splitlines()[1:] if netlist.splitlines() else []
    elems = sorted(re.sub(r"\s+", " ", line.strip())
                   for _i, line in _checks.iter_element_lines(lines))
    defns = sorted(re.sub(r"\s+", " ", ln.strip()) for ln in lines
                   if ln.strip().lower().startswith(_DEFN_PREFIXES))
    return _sha({"elements": elems, "definitions": defns})


def mapping_hash(report: dict) -> str:
    """映射哈希：条目、引脚表与封装内容任一变化都使旧板不可比（验收 F06：
    曾只含条目 ID——改引脚表不影响哈希）。"""
    from . import parts as _parts
    entries = {e["id"]: e for e in _parts.load_catalog()["entries"]}
    used = {}
    for m in report.get("mapped", []):
        e = entries.get(m["part"], {})
        used[m["ref"]] = {
            "part": m["part"],
            "footprint": e.get("footprint"),
            "spice_pin_order": e.get("spice_pin_order"),
            "pad_functions": e.get("pad_functions"),
        }
    return _sha({
        "catalog_schema": _catalog_schema(),
        "parts": used,
        "ports": sorted(p["net"] for p in report.get("ports", [])),
    })


def _catalog_schema() -> str:
    from . import parts as _parts
    return str(_parts.load_catalog().get("schema_version", "?"))


def default_rules() -> dict:
    """默认规则 = 制造底线 + 尚未提取的布局约束槽位。

    制造底线与 KiCad 默认布线宽度一致（track 0.2 / 净距 0.2 / 过孔 0.6·0.3），
    也是 DSN 导出的默认宽度。自定义更严的底线会进入 DRC（0.25mm 走线在
    1.0mm 最小线宽下被报错，实测）。底线只许加严，布局修复不得放宽。

    groups / proximity / locked / edge / net_classes / keepouts /
    keepout_intents / isolation / rotations 由 constraints.build 按已验证
    电路填实后再写入 rules.json。空表表示还没提取，不是把硬约束丢掉。
    """
    return {
        "schema": SCHEMA_VERSION,
        "layers": 2,
        "track_mm": 0.2,
        "clearance_mm": 0.2,
        "via_drill_mm": 0.3,
        "via_diameter_mm": 0.6,
        "zone_net": "GND",
        "groups": [],
        "proximity": [],
        "locked": [],
        "edge": [],
        "net_classes": [],
        "keepouts": [],
        "keepout_intents": [],
        "isolation": [],
        "rotations": {"*": [0, 90, 180, 270]},
        "placement": "signal_flow",
    }


def rules_hash(rules: dict) -> str:
    return _sha(rules or default_rules())


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


def new_version(*, netlist: str, report: dict, rules: dict | None,
                tool_versions: dict, job_id: str,
                source_version_id: int | None = None) -> dict:
    """构造候选版本记录（板文件哈希在检查通过后由调用方补上）。

    source_version_id：任务提交时主项目电路的修订号——采用时的源电路
    绑定校验用（验收 F06：计划 §6.1 的源版本绑定）。
    """
    return {
        "schema": SCHEMA_VERSION,
        "pcb_version_id": new_job_id() + "-pcb",
        "job_id": job_id,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_netlist_hash": electrical_hash(netlist),
        "source_version_id": source_version_id,
        "mapping_hash": mapping_hash(report),
        "rules_hash": rules_hash(rules),
        "tools": tool_versions,
        "board_hash": None,
        "status": ROUTED,
    }


def validate_version(v: dict) -> None:
    if not isinstance(v, dict):
        raise ValueError("版本记录必须是对象")
    for field in ("pcb_version_id", "source_netlist_hash", "mapping_hash",
                  "rules_hash", "status"):
        if not v.get(field):
            raise ValueError(f"版本记录缺字段：{field}")
    if v["status"] not in _STATES:
        raise ValueError(f"非法状态：{v['status']}")
    if len(str(v["pcb_version_id"])) > 80:
        raise ValueError("版本 id 过长")
