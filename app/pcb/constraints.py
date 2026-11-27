"""从已验证电路提取布局约束，并在任何坐标被采用前做闭合校验。

产物就是 jobs 写入 input/rules.json 的那份规则：制造底线（线宽、净距、过孔、
铺铜网）和布局意图（功能组、去耦近距、靠边/锁定、电源网络类、禁布、允许角度）
在同一个对象里。校验失败抛 ConstraintError，调用方停止任务，不删掉硬约束
也不生成板。
"""
from __future__ import annotations

import copy
import math
import re

from .. import i18n
from . import schema
from .legalizer import courtyard

_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,40}$")
_NOISY = re.compile(r"(clk|pwm|switc|noisy|xtal)", re.I)
_POWER = {
    "gnd", "vcc", "vee", "vdd", "vss", "vp", "vm", "v+", "v-",
}
_POWER_PIN = {"V+", "V-", "VCC", "VEE", "VP", "VM"}
_INPUT_PIN = {"IN+", "IN-", "IN_A+", "IN_A-"}


class ConstraintError(ValueError):
    """约束不合法或互相冲突。issues 是结构化原因，message 只是摘要。"""

    def __init__(self, issues: list[dict]):
        self.issues = issues
        msg = "；".join(i.get("reason", "") for i in issues[:4]) or "约束不合法"
        super().__init__(msg)


def _issue(code: str, reason: str, **extra) -> dict:
    item = {"code": code, "reason": reason}
    item.update(extra)
    return item


def _num(value, lo: float, hi: float, path: str, issues: list[dict]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(_issue("type", f"{path} 必须是数字", path=path))
        return None
    if not math.isfinite(float(value)) or not (lo <= float(value) <= hi):
        issues.append(_issue("range", f"{path} 必须在 {lo}–{hi} 之间", path=path))
        return None
    return float(value)


def _power_net(name: str) -> bool:
    low = str(name).lower()
    return low in _POWER or low.startswith(("vcc", "vee", "vdd", "vss", "vp_", "vm_"))


def build(ir: dict, spec: dict, rules: dict | None = None) -> dict:
    """合并制造底线、调用方规则和从电路推出的意图。冲突则抛 ConstraintError。"""
    issues: list[dict] = []
    user = copy.deepcopy(rules or {})
    known = set(schema.default_rules())
    unknown = sorted(k for k in user if k not in known)
    if unknown:
        issues.append(_issue(
            "unknown",
            "规则里有未识别字段，不能悄悄忽略：" + ", ".join(unknown)))
        raise ConstraintError(issues)
    merged = schema.default_rules()
    for key in ("layers", "track_mm", "clearance_mm", "via_drill_mm",
                "via_diameter_mm", "zone_net"):
        if key in user and user[key] is not None:
            merged[key] = user[key]
    _validate_floor(merged, issues)
    if issues:
        raise ConstraintError(issues)

    derived = _derive(ir, spec, merged)
    _merge_lists(merged, user, derived, spec, issues)
    _check_refs(merged, spec, issues)
    _check_geometry_possible(merged, spec, issues)
    if issues:
        raise ConstraintError(issues)
    merged["placement"] = "signal_flow"
    return merged


def _validate_floor(rules: dict, issues: list[dict]) -> None:
    if int(rules.get("layers") or 0) != 2:
        issues.append(_issue("layers", "当前只接受双面板（layers=2）"))
    track = _num(rules.get("track_mm"), 0.05, 10, "track_mm", issues)
    clear = _num(rules.get("clearance_mm"), 0.05, 10, "clearance_mm", issues)
    drill = _num(rules.get("via_drill_mm"), 0.1, 2, "via_drill_mm", issues)
    dia_raw = rules.get("via_diameter_mm")
    if dia_raw in (None, 0, 0.0):
        dia = (drill or 0.3) + 0.3
        rules["via_diameter_mm"] = dia
    else:
        dia = _num(dia_raw, 0.2, 5, "via_diameter_mm", issues)
    if drill is not None and dia is not None and dia <= drill + 0.02:
        issues.append(_issue("via", "过孔外径必须大于钻孔"))
    zone = str(rules.get("zone_net") or "")
    if not _SAFE.match(zone):
        issues.append(_issue("zone_net", f"铺铜网络名非法：{zone!r}"))
    rules["track_mm"] = track if track is not None else rules.get("track_mm")
    rules["clearance_mm"] = clear if clear is not None else rules.get("clearance_mm")
    rules["via_drill_mm"] = drill if drill is not None else rules.get("via_drill_mm")


def _catalog() -> dict[str, dict]:
    from . import parts
    return {e["id"]: e for e in parts.load_catalog()["entries"]}


def _derive(ir: dict, spec: dict, floor: dict) -> dict:
    entries = _catalog()
    ir_by_ref = {el["ref"]: el for el in ir.get("elements") or []}
    comps = [c for c in spec.get("components") or []]
    by_ref = {c["ref"]: c for c in comps}
    nets = set(spec.get("nets") or [])

    ics = []
    for comp in comps:
        entry = entries.get(comp.get("part") or "")
        if entry and entry.get("category") == "ic-opamp":
            ics.append(comp)

    power_pins: dict[str, set[str]] = {}
    input_pins: dict[str, set[str]] = {}
    for comp in ics:
        entry = entries[comp["part"]]
        pads = list(comp.get("pads") or [])
        numbers = sorted(entry["pad_functions"], key=int)
        power_pins[comp["ref"]] = set()
        input_pins[comp["ref"]] = set()
        for pad_no, net in zip(numbers, pads):
            if str(pad_no) in (entry.get("pad_ties") or {}):
                continue
            func = entry["pad_functions"][pad_no]
            if func in _POWER_PIN or _power_net(net):
                power_pins[comp["ref"]].add(net)
            # 输入脚若直接接到地或电源，那张网不是高阻敏感节点。
            if func in _INPUT_PIN and str(net).upper() != "GND" and not _power_net(net):
                input_pins[comp["ref"]].add(net)

    power_nets = {n for n in nets if _power_net(n) and n != "GND"}
    for nets_of in power_pins.values():
        power_nets |= {n for n in nets_of if n != "GND"}

    proximity = []
    decap_refs: set[str] = set()
    for comp in comps:
        el = ir_by_ref.get(comp["ref"])
        if el is None or el.get("kind") != "C":
            continue
        from .parts import _parse_value
        farads = _parse_value(str(el.get("value") or ""))
        if farads is None or farads > 1e-6:
            continue
        pad_nets = [n for n in comp.get("pads") or []]
        if len(pad_nets) != 2:
            continue
        gnd_side = [n for n in pad_nets if str(n).upper() == "GND"]
        hot = [n for n in pad_nets if n not in gnd_side and (_power_net(n) or n in power_nets)]
        if not gnd_side or not hot:
            continue
        parents = [ic["ref"] for ic in ics if hot[0] in power_pins.get(ic["ref"], set())]
        if not parents:
            parents = [ic["ref"] for ic in ics]
        if not parents:
            continue
        parent = sorted(parents)[0]
        proximity.append({
            "ref": comp["ref"],
            "parent": parent,
            "max_mm": 20.0,
            "reason": i18n.t("pcb.constraint.decap", ref=comp["ref"], parent=parent),
        })
        decap_refs.add(comp["ref"])

    groups = []
    for ic in ics:
        members = [ic["ref"]]
        signal_nets = set()
        entry = entries[ic["part"]]
        numbers = sorted(entry["pad_functions"], key=int)
        for pad_no, net in zip(numbers, ic.get("pads") or []):
            if str(pad_no) in (entry.get("pad_ties") or {}):
                continue
            func = entry["pad_functions"][pad_no]
            if func in _POWER_PIN or _power_net(net) or str(net).upper() == "GND":
                continue
            signal_nets.add(net)
        for comp in comps:
            if comp["ref"] == ic["ref"]:
                continue
            if comp["ref"] in decap_refs and any(
                    n in power_pins.get(ic["ref"], set()) for n in comp.get("pads") or []):
                members.append(comp["ref"])
                continue
            if signal_nets.intersection(comp.get("pads") or []):
                members.append(comp["ref"])
        members = list(dict.fromkeys(members))
        if len(members) >= 2:
            groups.append({
                "id": f"fn:{ic['ref']}",
                "refs": members,
                "max_span_mm": 70.0,
                "reason": i18n.t("pcb.constraint.group", ref=ic["ref"]),
            })

    locked = []
    edge = []
    for comp in comps:
        part = str(comp.get("part") or "")
        if part == "HOLE_M3":
            locked.append({"ref": comp["ref"], "x": float(comp["x"]), "y": float(comp["y"]), "rot": 0})
        elif part == "CONN_HDR_1X02" or part.startswith("CONN_"):
            edge.append({"ref": comp["ref"], "band_mm": 12.0,
                         "reason": i18n.t("pcb.constraint.edge", ref=comp["ref"])})

    classes = []
    if "GND" in nets:
        classes.append({
            "name": "Ground",
            "nets": ["GND"],
            "track_mm": float(floor["track_mm"]),
            "clearance_mm": float(floor["clearance_mm"]),
            "priority": 20,
            "pour": str(floor.get("zone_net")) == "GND",
        })
    hot_nets = sorted(n for n in power_nets if n in nets and n != "GND")
    if hot_nets:
        classes.append({
            "name": "Power",
            "nets": hot_nets,
            "track_mm": float(floor["track_mm"]),
            "clearance_mm": float(floor["clearance_mm"]),
            "priority": 10,
            "pour": False,
        })

    intents = []
    sensitive = set()
    for ic in ics:
        for net in sorted(input_pins.get(ic["ref"], set())):
            sensitive.add(net)
            intents.append({
                "net": net,
                "kind": "sensitive",
                "anchor_refs": [ic["ref"]],
                "size_mm": 4.0,
                "reason": i18n.t("pcb.constraint.sensitive", net=net, ref=ic["ref"]),
            })
    for net in sorted(nets):
        if _NOISY.search(str(net)):
            anchors = [c["ref"] for c in comps if net in (c.get("pads") or [])]
            intents.append({
                "net": net,
                "kind": "noisy",
                "anchor_refs": anchors[:3] or [comps[0]["ref"]],
                "size_mm": 4.0,
                "reason": i18n.t("pcb.constraint.noisy", net=net),
            })

    isolation = []
    if sensitive and ics:
        away = []
        for comp in comps:
            if comp["ref"] in decap_refs:
                continue
            if any(_power_net(n) and n != "GND" for n in comp.get("pads") or []):
                if comp["ref"] not in {ic["ref"] for ic in ics}:
                    away.append(comp["ref"])
        near = []
        for comp in comps:
            if comp["ref"] in {ic["ref"] for ic in ics}:
                continue
            if sensitive.intersection(comp.get("pads") or []):
                near.append(comp["ref"])
        if near and away:
            isolation.append({
                "refs": near,
                "away_from": away,
                "min_mm": 2.0,
                "reason": i18n.t("pcb.constraint.isolation"),
            })

    keepouts = [dict(k) for k in (spec.get("keepouts") or [])]
    rotations = {"*": [0, 90, 180, 270]}
    for item in locked:
        rotations[item["ref"]] = [0]
    return {
        "groups": groups,
        "proximity": proximity,
        "locked": locked,
        "edge": edge,
        "net_classes": classes,
        "keepouts": keepouts,
        "keepout_intents": intents,
        "isolation": isolation,
        "rotations": rotations,
    }


def _merge_lists(merged: dict, user: dict, derived: dict, spec: dict, issues: list[dict]) -> None:
    floor_t = float(merged["track_mm"])
    floor_c = float(merged["clearance_mm"])
    merged["net_classes"] = _merge_net_classes(
        user.get("net_classes"), derived["net_classes"], floor_t, floor_c, spec, issues)
    merged["groups"] = _merge_groups(user.get("groups"), derived["groups"], issues)
    merged["proximity"] = _merge_proximity(user.get("proximity"), derived["proximity"], issues)
    merged["locked"] = _merge_locked(user.get("locked"), derived["locked"], issues)
    merged["edge"] = _merge_edge(user.get("edge"), derived["edge"], merged["locked"], spec, issues)
    merged["keepouts"] = _merge_keepouts(user.get("keepouts"), derived["keepouts"], issues)
    merged["keepout_intents"] = _merge_intents(
        user.get("keepout_intents"), derived["keepout_intents"], issues)
    merged["isolation"] = _merge_isolation(user.get("isolation"), derived["isolation"], issues)
    merged["rotations"] = _merge_rotations(user.get("rotations"), derived["rotations"], issues)


def _as_list(value, path: str, issues: list[dict]) -> list | None:
    if value is None:
        return None
    if not isinstance(value, list):
        issues.append(_issue("type", f"{path} 必须是数组", path=path))
        return []
    return value


def _merge_net_classes(user, derived, floor_t, floor_c, spec, issues) -> list[dict]:
    user_list = _as_list(user, "net_classes", issues)
    source = derived if not user_list else user_list
    # 用户给了网络类时，仍把推导类里尚未被覆盖的网络补上（不覆盖用户已声明的网）。
    claimed: dict[str, str] = {}
    out = []
    items = list(source)
    if user_list:
        covered = {n for c in user_list if isinstance(c, dict) for n in (c.get("nets") or [])}
        for c in derived:
            if any(n not in covered for n in c["nets"]):
                extra = dict(c)
                extra["nets"] = [n for n in c["nets"] if n not in covered]
                if extra["nets"]:
                    items.append(extra)
    known_nets = set(spec.get("nets") or [])
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            issues.append(_issue("type", f"net_classes[{index}] 必须是对象"))
            continue
        name = str(raw.get("name") or "")
        if not _SAFE.match(name):
            issues.append(_issue("net_class", f"网络类名称非法：{name!r}"))
            continue
        nets = raw.get("nets")
        if not isinstance(nets, list) or not nets:
            issues.append(_issue("net_class", f"网络类 {name} 没有网络"))
            continue
        clean_nets = []
        for net in nets:
            if str(net) not in known_nets:
                issues.append(_issue(
                    "net_class",
                    f"网络类 {name} 引用了板上不存在的网络 {net}",
                    net=str(net)))
                continue
            if str(net) in claimed:
                issues.append(_issue(
                    "net_class_conflict",
                    f"网络 {net} 同时属于 {claimed[str(net)]} 和 {name}",
                    net=str(net)))
                continue
            claimed[str(net)] = name
            clean_nets.append(str(net))
        track = _num(raw.get("track_mm", floor_t), 0.05, 10, f"{name}.track_mm", issues)
        clear = _num(raw.get("clearance_mm", floor_c), 0.05, 10, f"{name}.clearance_mm", issues)
        priority = raw.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int) or not (0 <= priority <= 100):
            issues.append(_issue("net_class", f"网络类 {name} 的 priority 必须是 0–100 的整数"))
            priority = 0
        if track is not None and track + 1e-9 < floor_t:
            issues.append(_issue(
                "relax",
                f"网络类 {name} 的线宽 {track}mm 低于制造底线 {floor_t}mm，拒绝放宽",
                name=name))
        if clear is not None and clear + 1e-9 < floor_c:
            issues.append(_issue(
                "relax",
                f"网络类 {name} 的净距 {clear}mm 低于制造底线 {floor_c}mm，拒绝放宽",
                name=name))
        pour = bool(raw.get("pour", False))
        if clean_nets and track is not None and clear is not None:
            out.append({
                "name": name,
                "nets": clean_nets,
                "track_mm": track,
                "clearance_mm": clear,
                "priority": int(priority),
                "pour": pour,
            })
    return out


def _merge_groups(user, derived, issues) -> list[dict]:
    user_list = _as_list(user, "groups", issues)
    return _merge_by_id(user_list, derived, "id", "groups", issues, _stricter_span)


def _stricter_span(user_item, derived_item, issues) -> dict:
    if float(user_item["max_span_mm"]) > float(derived_item["max_span_mm"]) + 0.05:
        issues.append(_issue(
            "conflict",
            f"功能组 {user_item['id']} 的跨度 {user_item['max_span_mm']}mm "
            f"宽于从电路推出的 {derived_item['max_span_mm']}mm，不能放宽硬约束"))
        return derived_item
    return user_item


def _merge_proximity(user, derived, issues) -> list[dict]:
    user_list = _as_list(user, "proximity", issues)
    derived_map = {(d["ref"], d["parent"]): d for d in derived}
    if not user_list:
        return copy.deepcopy(derived)
    out = []
    seen = set()
    for item in user_list:
        if not isinstance(item, dict):
            issues.append(_issue("type", "proximity 的条目必须是对象"))
            continue
        key = (str(item.get("ref")), str(item.get("parent")))
        seen.add(key)
        base = derived_map.get(key)
        norm = _norm_proximity(item, issues)
        if norm is None:
            continue
        if base and float(norm["max_mm"]) > float(base["max_mm"]) + 0.05:
            issues.append(_issue(
                "conflict",
                f"{norm['ref']} 靠近 {norm['parent']} 的距离被放宽到 {norm['max_mm']}mm，"
                f"电路要求 ≤ {base['max_mm']}mm"))
            out.append(copy.deepcopy(base))
        else:
            out.append(norm)
    for key, base in derived_map.items():
        if key not in seen:
            out.append(copy.deepcopy(base))
    return out


def _norm_proximity(item, issues) -> dict | None:
    ref, parent = str(item.get("ref") or ""), str(item.get("parent") or "")
    dist = _num(item.get("max_mm"), 1, 200, f"proximity.{ref}", issues)
    if not ref or not parent or dist is None:
        issues.append(_issue("proximity", "近距约束需要 ref、parent 和 max_mm"))
        return None
    return {"ref": ref, "parent": parent, "max_mm": dist,
            "reason": str(item.get("reason") or "")[:200]}


def _merge_locked(user, derived, issues) -> list[dict]:
    user_list = _as_list(user, "locked", issues)
    by_ref = {item["ref"]: item for item in derived}
    if not user_list:
        return copy.deepcopy(derived)
    out = []
    seen = set()
    for item in user_list:
        if not isinstance(item, dict) or not item.get("ref"):
            issues.append(_issue("locked", "锁定条目需要 ref/x/y"))
            continue
        ref = str(item["ref"])
        seen.add(ref)
        x = _num(item.get("x"), -10, 500, f"locked.{ref}.x", issues)
        y = _num(item.get("y"), -10, 500, f"locked.{ref}.y", issues)
        rot = item.get("rot", 0)
        if x is None or y is None:
            continue
        if ref in by_ref:
            base = by_ref[ref]
            if abs(x - float(base["x"])) > 0.05 or abs(y - float(base["y"])) > 0.05:
                issues.append(_issue(
                    "conflict",
                    f"{ref} 的锁定坐标 ({x}, {y}) 与机械孔的固定位置 "
                    f"({base['x']}, {base['y']}) 冲突",
                    ref=ref))
                out.append(copy.deepcopy(base))
                continue
        out.append({"ref": ref, "x": x, "y": y, "rot": int(rot or 0) % 360})
    for ref, base in by_ref.items():
        if ref not in seen:
            out.append(copy.deepcopy(base))
    return out


def _merge_edge(user, derived, locked, spec, issues) -> list[dict]:
    user_list = _as_list(user, "edge", issues)
    source = derived if not user_list else user_list
    # 用户若没提到某个连接器，仍保留推导出的靠边要求。
    if user_list:
        named = {str(i.get("ref")) for i in user_list if isinstance(i, dict)}
        source = list(user_list) + [d for d in derived if d["ref"] not in named]
    out = []
    locked_by = {i["ref"]: i for i in locked}
    width, height = float(spec["size_mm"][0]), float(spec["size_mm"][1])
    for item in source:
        if not isinstance(item, dict) or not item.get("ref"):
            issues.append(_issue("edge", "靠边约束需要 ref"))
            continue
        ref = str(item["ref"])
        band = _num(item.get("band_mm", 12), 2, 40, f"edge.{ref}", issues)
        if band is None:
            continue
        lock = locked_by.get(ref)
        if lock is not None:
            dist = min(float(lock["x"]), width - float(lock["x"]),
                       float(lock["y"]), height - float(lock["y"]))
            if dist > band + 0.05:
                issues.append(_issue(
                    "conflict",
                    f"{ref} 被锁定在离板边 {dist:.1f}mm 处，靠边约束要求 ≤ {band}mm",
                    ref=ref))
        out.append({"ref": ref, "band_mm": band, "reason": str(item.get("reason") or "")[:200]})
    return out


def _merge_keepouts(user, derived, issues) -> list[dict]:
    user_list = _as_list(user, "keepouts", issues)
    derived_norm = _norm_keepouts(derived, issues)
    # 缺省空表表示“还没另外声明”，板上已有的禁布区仍然保留。
    if not user_list:
        return derived_norm
    user_norm = _norm_keepouts(user_list, issues)

    def _key(item: dict) -> tuple:
        return (round(item["x"], 2), round(item["y"], 2),
                round(item["w"], 2), round(item["h"], 2))

    have = {_key(item) for item in user_norm}
    for item in derived_norm:
        if _key(item) not in have:
            issues.append(_issue(
                "conflict",
                "已声明的禁布区不在提交的规则里，不能把它删掉",
                keepout=item))
    extra = [item for item in user_norm if _key(item) not in {_key(k) for k in derived_norm}]
    return derived_norm + extra


def _norm_keepouts(source, issues) -> list[dict]:
    out = []
    for index, raw in enumerate(source or []):
        if not isinstance(raw, dict):
            issues.append(_issue("keepout", f"keepouts[{index}] 必须是对象"))
            continue
        x = _num(raw.get("x"), -10, 500, f"keepouts[{index}].x", issues)
        y = _num(raw.get("y"), -10, 500, f"keepouts[{index}].y", issues)
        w = _num(raw.get("w"), 0.2, 500, f"keepouts[{index}].w", issues)
        h = _num(raw.get("h"), 0.2, 500, f"keepouts[{index}].h", issues)
        if None in (x, y, w, h):
            continue
        item = {"x": x, "y": y, "w": w, "h": h}
        if raw.get("net"):
            item["net"] = str(raw["net"])[:40]
        if raw.get("reason"):
            item["reason"] = str(raw["reason"])[:200]
        if raw.get("generated"):
            item["generated"] = True
        out.append(item)
    return out


def _merge_intents(user, derived, issues) -> list[dict]:
    user_list = _as_list(user, "keepout_intents", issues)
    if not user_list:
        return copy.deepcopy(derived)
    # 用户意图追加，推导意图按网络保留。同网更小的 size 视为加严。
    by_net = {i["net"]: copy.deepcopy(i) for i in derived}
    for item in user_list:
        if not isinstance(item, dict) or not item.get("net"):
            issues.append(_issue("keepout", "禁布意图需要 net"))
            continue
        net = str(item["net"])
        size = _num(item.get("size_mm", 4), 1, 40, f"keepout_intents.{net}", issues)
        if size is None:
            continue
        prev = by_net.get(net)
        if prev and size + 0.05 < float(prev["size_mm"]):
            prev["size_mm"] = size
            continue
        if prev and size > float(prev["size_mm"]) + 0.05:
            issues.append(_issue(
                "conflict",
                f"网络 {net} 的禁布尺寸被放宽到 {size}mm，电路要求 {prev['size_mm']}mm"))
            continue
        if prev is None:
            by_net[net] = {
                "net": net,
                "kind": str(item.get("kind") or "user"),
                "anchor_refs": [str(r) for r in item.get("anchor_refs") or []],
                "size_mm": size,
                "reason": str(item.get("reason") or "")[:200],
            }
    return list(by_net.values())


def _merge_isolation(user, derived, issues) -> list[dict]:
    user_list = _as_list(user, "isolation", issues)
    if not user_list:
        return copy.deepcopy(derived)
    out = copy.deepcopy(derived)
    for item in user_list:
        if not isinstance(item, dict):
            issues.append(_issue("isolation", "隔离约束必须是对象"))
            continue
        gap = _num(item.get("min_mm"), 0, 50, "isolation.min_mm", issues)
        if gap is None:
            continue
        out.append({
            "refs": [str(r) for r in item.get("refs") or []],
            "away_from": [str(r) for r in item.get("away_from") or []],
            "min_mm": gap,
            "reason": str(item.get("reason") or "")[:200],
        })
    return out


def _merge_rotations(user, derived, issues) -> dict:
    base = copy.deepcopy(derived)
    if not isinstance(user, dict) or not user:
        # default_rules 里有 "*"，调用方没改时 user 仍带默认值。
        # 只有出现非默认的收紧才覆盖。
        if isinstance(user, dict) and user and user != {"*": [0, 90, 180, 270]}:
            pass
        else:
            return base
    source = user if isinstance(user, dict) else {}
    if source == {"*": [0, 90, 180, 270]}:
        return base
    for ref, angles in source.items():
        if not isinstance(angles, list) or not angles:
            issues.append(_issue("rotation", f"{ref} 的允许角度必须是非空数组"))
            continue
        clean = []
        for ang in angles:
            if isinstance(ang, bool) or not isinstance(ang, (int, float)):
                issues.append(_issue("rotation", f"{ref} 的角度 {ang!r} 非法"))
                continue
            turned = int(ang) % 360
            if turned not in (0, 90, 180, 270):
                issues.append(_issue("rotation", f"{ref} 只允许 0/90/180/270，收到 {ang}"))
                continue
            clean.append(turned)
        if not clean:
            continue
        prev = base.get(str(ref))
        if prev and not set(clean) <= set(prev):
            issues.append(_issue(
                "conflict",
                f"{ref} 的允许角度 {clean} 超出已有限制 {prev}"))
            continue
        base[str(ref)] = clean
    if "*" not in base:
        base["*"] = [0, 90, 180, 270]
    return base


def _merge_by_id(user_list, derived, id_key, path, issues, on_conflict) -> list[dict]:
    if not user_list:
        return copy.deepcopy(derived)
    derived_map = {item[id_key]: item for item in derived}
    out = []
    seen = set()
    for item in user_list:
        if not isinstance(item, dict) or not item.get(id_key):
            issues.append(_issue("type", f"{path} 的条目需要 {id_key}"))
            continue
        ident = str(item[id_key])
        seen.add(ident)
        norm = _norm_group(item, issues)
        if norm is None:
            continue
        base = derived_map.get(ident)
        out.append(on_conflict(norm, base, issues) if base else norm)
    for ident, base in derived_map.items():
        if ident not in seen:
            out.append(copy.deepcopy(base))
    return out


def _norm_group(item, issues) -> dict | None:
    refs = item.get("refs")
    span = _num(item.get("max_span_mm"), 5, 400, f"groups.{item.get('id')}", issues)
    if not isinstance(refs, list) or len(refs) < 2 or span is None:
        issues.append(_issue("group", f"功能组 {item.get('id')} 至少要有两个位号和跨度"))
        return None
    return {"id": str(item["id"]), "refs": [str(r) for r in refs], "max_span_mm": span,
            "reason": str(item.get("reason") or "")[:200]}


def _check_refs(rules: dict, spec: dict, issues: list[dict]) -> None:
    known = {c["ref"] for c in spec.get("components") or []}
    for item in rules.get("locked") or []:
        if item["ref"] not in known:
            issues.append(_issue("locked_missing", f"锁定位号 {item['ref']} 不在板上", ref=item["ref"]))
    for item in rules.get("edge") or []:
        if item["ref"] not in known:
            issues.append(_issue("edge_missing", f"靠边位号 {item['ref']} 不在板上", ref=item["ref"]))
    for item in rules.get("proximity") or []:
        if item["ref"] not in known or item["parent"] not in known:
            issues.append(_issue(
                "proximity_missing",
                f"近距约束 {item['ref']}→{item['parent']} 有位号不在板上"))
    for group in rules.get("groups") or []:
        missing = [r for r in group["refs"] if r not in known]
        if missing:
            issues.append(_issue("group_missing", f"功能组 {group['id']} 缺少位号 {missing}"))


def _check_geometry_possible(rules: dict, spec: dict, issues: list[dict]) -> None:
    by_ref = {c["ref"]: c for c in spec.get("components") or []}
    for item in rules.get("proximity") or []:
        child = by_ref.get(item["ref"])
        parent = by_ref.get(item["parent"])
        if child is None or parent is None:
            continue
        wa, ha = courtyard(child)
        wb, hb = courtyard(parent)
        minimum = min((wa + wb) / 2, (ha + hb) / 2)
        if float(item["max_mm"]) + 1e-6 < minimum:
            issues.append(_issue(
                "impossible",
                f"{item['ref']} 要放在离 {item['parent']} {item['max_mm']}mm 内，"
                f"但庭院至少需要 {minimum:.1f}mm，约束互相矛盾",
                ref=item["ref"]))
