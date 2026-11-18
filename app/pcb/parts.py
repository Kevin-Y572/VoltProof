"""审校器件库与映射：CircuitIR + ports → 板规格（build_board 输入）+ 映射报告。

方案 §4.2：器件库是"审校过的"——每个条目固化封装、引脚表（SPICE 引脚序
→ 实物引脚功能 → 焊盘号）、极性与数据来源，型号按条目映射而不是按类别
推断（"NPN"推断不出 TO-92 的 E-B-C 排列）。首批 ~10 条：R/C/L/D/Q/端子，
半导体只收 2N3904/2N3906 + 1N4148/1N4007/LED。

映射语义（方案 §4 的硬规则，全部显式报告、不静默处理）：
- 激励源不是实物：其非地网络必须被声明为外部端子（ports），否则阻断；
- 0V 检测源、子电路/理想运放、受控源 → 阻断并给原因（见 circuit_ir）；
- 电解电容（值≥1uF）按"网表第 1 节点为正极"约定映射，报告标注须人工复核；
- 教学模型（QNPN/QPNP/DMOD）→ 审校型号映射，报告标注额定参数须核对。

布局为确定性占位（网格 + 端子靠边），阶段 C 换信号流布局（方案 §5.3）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .. import i18n, specs
from ..circuit_ir import netlist_to_ir
from . import kicad_backend, placement

_CATALOG_PATH = Path(__file__).resolve().parent / "part_catalog" / "catalog.json"


class MappingBlocked(Exception):
    """映射存在阻断项（message 汇总原因；details 在 result 里）。"""


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


def _parse_value(value: str) -> float | None:
    try:
        return specs.number(str(value))
    except (ValueError, AttributeError):
        return None


def resolve_entry(kind: str, value: str, mount: str = "tht") -> tuple[dict | None, str]:
    """按元件类型 + 值/型号串 + 安装方式（tht/smd，默认 tht）匹配审校条目。"""
    entries = [e for e in load_catalog()["entries"]
               if e["match"].get("kind") == kind and e.get("mount", "tht") == mount]
    if not entries:
        alt = "smd" if mount == "tht" else "tht"
        has_alt = any(e["match"].get("kind") == kind
                      for e in load_catalog()["entries"])
        if has_alt:
            return None, i18n.t("api.block.entry_alt", kind=kind, mount=mount)
        return None, i18n.t("api.block.entry_none", kind=kind, mount=mount)
    text = str(value).lower()
    farads = _parse_value(value) if kind == "C" else None
    # 先试带条件的条目（型号关键字 / 容值阈值），命中多条时取条件最严的
    for e in sorted(entries, key=lambda e: len(e["match"]), reverse=True):
        keys = [k.lower() for k in e["match"].get("model_contains", [])]
        if keys and any(k in text for k in keys):
            return e, ""
    for e in sorted(entries, key=lambda e: e["match"].get("min_value_farads", 0),
                    reverse=True):
        threshold = e["match"].get("min_value_farads")
        if threshold is not None and farads is not None and farads >= threshold:
            return e, ""
    # 无条件默认条目（R/L/小电容）
    for e in entries:
        if not e["match"].get("model_contains") and "min_value_farads" not in e["match"]:
            return e, ""
    return None, i18n.t("api.block.value", kind=kind, value=repr(value))


def _pads_for(entry: dict, nets: list[str]) -> list[str]:
    """按 焊盘号→引脚功能→SPICE 位序 组装 pads（焊盘顺序的净表）。"""
    order = entry["spice_pin_order"]
    pads: list[str] = []
    for pad in sorted(entry["pad_functions"], key=int):
        func = entry["pad_functions"][pad]
        pads.append(nets[order.index(func)])
    return pads


def map_netlist(netlist: str, ports: list[str], options: dict | None = None) -> dict:
    """网表 + 外部端子声明 + 板选项 → {ok, spec|None, report}。不抛异常。"""
    ir = netlist_to_ir(netlist)
    return map_ir(ir, ports, options)


def map_ir(ir: dict, ports: list[str], options: dict | None = None) -> dict:
    report: dict = {"ok": False, "mapped": [], "ports": [], "blocked": [], "warnings": []}
    blockers: list[dict] = []
    for el in ir["elements"]:
        if el["role"] == "blocked":
            blockers.append({"ref": el["ref"],
                             "reason": el["block_reason"] or el["note"]})
    # 激励源端子完整性：非地网络必须被声明
    declared = {netlist_alias(net) for net in ports}
    for net in ir["port_candidates"]:
        if net not in declared:
            blockers.append({"ref": net, "reason": i18n.t("api.block.ports")})
    report["blocked"] = blockers
    report["net_renames"] = ir["net_renames"]

    devices = [el for el in ir["elements"] if el["role"] == "device"]
    components: list[dict] = []
    for el in devices:
        entry, why = resolve_entry(el["kind"], el["value"],
                                   mount=str((options or {}).get("mount", "tht")))
        if entry is None:
            blockers.append({"ref": el["ref"], "reason": why})
            continue
        if len(el["nets"]) != len(entry["spice_pin_order"]):
            blockers.append({"ref": el["ref"], "reason":
                             i18n.t("api.block.pins", nets=len(el["nets"]), part=el["part"], pins=len(entry["spice_pin_order"]))})
            continue
        pads = _pads_for(entry, el["nets"])
        components.append({"ref": el["ref"], "part": entry["id"],
                           "lib": entry["footprint"]["lib"],
                           "footprint": entry["footprint"]["name"], "pads": pads})
        item = {"ref": el["ref"], "value": el["value"], "part": entry["id"],
                "part_name": entry["part"],
                "footprint": f"{entry['footprint']['lib']}:{entry['footprint']['name']}",
                "pin_map": {pad: {"function": entry["pad_functions"][pad],
                                  "net": pads[i]}
                            for i, pad in enumerate(sorted(entry["pad_functions"], key=int))},
                "notes": [entry["notes"]] if entry["notes"] else []}
        if entry["polarized"]:
            item["notes"].append(i18n.t("api.block.polar"))
        if any(k in str(el["value"]).lower()
               for k in ("qnpn", "qpnp", "dmod")):
            item["notes"].append(i18n.t("api.block.teach"))
        report["mapped"].append(item)

    if blockers:
        return {"ok": False, "spec": None, "report": report}

    # 信号流布局（placement.py）：器件按 BFS 分层排布、端子靠边；
    # 坐标槽位 __port__i 与 ports 同序
    used = {c["ref"].upper() for c in components}
    conn = next(e for e in load_catalog()["entries"] if e["id"] == "CONN_HDR_1X02")
    gnd = "GND"
    opts = options or {}
    w, h, pos = placement.place(components, ports)
    # 板选项：显式尺寸只放大不缩小（保证庭院间距）；安装孔四角；禁布区透传
    w = max(w, float(opts.get("board_w", 0) or 0))
    h = max(h, float(opts.get("board_h", 0) or 0))
    holes: list[dict] = []
    if opts.get("mounting_holes"):
        hole = next(e for e in load_catalog()["entries"] if e["id"] == "HOLE_M3")
        inset = 4.0
        n = 0
        for hx, hy in ((inset, inset), (w - inset, inset), (w - inset, h - inset), (inset, h - inset)):
            n += 1
            holes.append({"ref": f"H{n}", "part": hole["id"],
                          "lib": hole["footprint"]["lib"],
                          "footprint": hole["footprint"]["name"],
                          "x": round(hx, 2), "y": round(hy, 2), "pads": []})
    for comp in components:
        comp["x"], comp["y"] = pos[comp["ref"]]
    for i, net in enumerate(ports):
        ref = f"J{i + 1}"
        while ref.upper() in used:
            ref = "J" + str(int(ref[1:]) + 1)
        used.add(ref.upper())
        x, y = pos[f"__port__{i}"]
        components.append({"ref": ref, "part": conn["id"],
                           "lib": conn["footprint"]["lib"],
                           "footprint": conn["footprint"]["name"],
                           "x": x, "y": y,
                           "pads": [netlist_alias(net), gnd]})
        report["ports"].append({"ref": ref, "net": netlist_alias(net),
                                "pads": ["信号", "GND"]})
    components.extend(holes)

    keepouts = []
    for k in opts.get("keepouts", []) or []:
        try:
            ko = {"x": float(k["x"]), "y": float(k["y"]),
                  "w": float(k["w"]), "h": float(k["h"])}
            if ko["w"] <= 0 or ko["h"] <= 0:
                raise ValueError
            keepouts.append(ko)
        except (KeyError, TypeError, ValueError):
            blockers.append({"ref": "<keepout>", "reason": f"禁布区参数非法：{k!r}"})
    if blockers:
        report["blocked"] = blockers
        return {"ok": False, "spec": None, "report": report}
    spec = {"name": "board", "size_mm": [w, h],
            "nets": sorted(set(ir["nets"]) | declared | {gnd}),
            "components": components,
            "keepouts": keepouts,
            "zones": [{"net": gnd, "layer": "B.Cu",
                       "outline_mm": [[4, 4], [w - 4, 4], [w - 4, h - 4], [4, h - 4]]}]}
    try:
        kicad_backend.validate_spec(spec)
    except kicad_backend.SpecError as e:  # 内部错误兜底：不该发生，防漏网
        blockers.append({"ref": "<spec>", "reason": f"生成的板规格未通过校验：{e}"})
        report["blocked"] = blockers
        return {"ok": False, "spec": None, "report": report}
    report["ok"] = True
    return {"ok": True, "spec": spec, "report": report}


def netlist_alias(net: str) -> str:
    """端子网络名的 GND 规范（与 circuit_ir 同规则）。"""
    return "GND" if net.lower() in ("0", "gnd") else net
