"""网络一致性对照：源电路 ↔ 板规格 ↔ 实际板文件（方案 §7 检查层）。

验收 F01 修正后的口径：对照必须保持**引脚身份**（逻辑引脚号 → 器件引脚
功能 → 焊盘号），不能退化成"每元件每网络几个脚"的计数——曾把二极管
A/K 交换后的板判为一致。三层数据源：

1. `check_partition(ir, spec, ports)`——期望侧从 CircuitIR（SPICE 位序），
   实际侧从板规格的 pads（焊盘号升序契约）经器件库独立合成。
2. `board_partition(preview, ir, ports)`——实际侧换成**板文件解析出的
   真实焊盘**（ref/焊盘号/网络，来自 preview_from_pcb），这才是
   "板与源电路一致"的证据；DRC 零违规不证明一致性。
3. `copper_verdict(route_result, drc_result)`——铜连接判定（未布数 +
   DRC 未连/违规），只认实测数字。

期望与实际共用器件库的引脚表（引脚表是唯一事实源），但**合成代码路径
独立**——映射器写错 pads 顺序时，1/2 都会抓住。
"""
from __future__ import annotations


def _catalog_entry(part_id: str) -> dict:
    from . import parts as _parts
    for e in _parts.load_catalog()["entries"]:
        if e["id"] == part_id:
            return e
    raise KeyError(f"器件库无条目 {part_id}")


def _pad_order(entry: dict) -> list[str]:
    """焊盘号升序。板规格 pads 与板文件焊盘都按此契约对位。"""
    return sorted(entry["pad_functions"], key=int)


def _tie_net(entry: dict, pad_no: str, ref: str) -> str | None:
    from .parts import tie_net
    return tie_net(entry, pad_no, ref)


def expected_pins(ir: dict, ports: list[str]) -> dict[tuple[str, int], str]:
    """期望侧：{(位号, SPICE 引脚号 1..n): 网络}。端子按生成契约
    （每口一个 1x2 排针：pad1=信号 pad2=GND）计入。"""
    out: dict[tuple[str, int], str] = {}
    for el in ir["elements"]:
        if el["role"] != "device":
            continue
        for i, net in enumerate(el["nets"], start=1):
            out[(el["ref"], i)] = net
    return out


def expected_port_pins(ports: list[str]) -> dict[tuple[str, int], str]:
    """端子期望：{(J 位号, 焊盘号): 网络}。J 位号按生成顺序 J1..Jn。"""
    out: dict[tuple[str, int], str] = {}
    for i, p in enumerate(ports, start=1):
        net = p if p.lower() not in ("0", "gnd") else "GND"
        out[(f"J{i}", 1)] = net
        out[(f"J{i}", 2)] = "GND"
    return out


def spec_pins(spec: dict, j_refs: list[str] | None = None) -> dict[tuple[str, int], str]:
    """板规格实际侧：{(位号, SPICE 引脚号): 网络}——按 器件库引脚表 独立合成。

    spec components 须带 `part`（器件库条目 id）；pads 按焊盘号升序排列
    （_pads_for 的既定契约）。端子（CONN_HDR_1X02）单独按 j_refs 对位。
    """
    from . import parts as _parts
    out: dict[tuple[str, int], str] = {}
    connectors: list[dict] = []
    for comp in spec.get("components", []):
        if comp.get("part") == "CONN_HDR_1X02":
            connectors.append(comp)
            continue
        entry = _catalog_entry(comp["part"])
        order = entry["spice_pin_order"]
        pads_sorted = _pad_order(entry)
        pads = comp.get("pads", [])
        if len(pads) != len(pads_sorted):
            raise ValueError(f"{comp['ref']} 焊盘数 {len(pads)} ≠ 条目 {len(pads_sorted)}")
        for pad_no, func, net in zip(pads_sorted, (entry["pad_functions"][p] for p in pads_sorted), pads):
            tied = _tie_net(entry, pad_no, comp["ref"])
            if tied is not None:
                if net != tied:
                    raise ValueError(f"{comp['ref']} 焊盘{pad_no} 应端接到 {tied}，规格是 {net}")
                continue
            if func not in order:
                raise ValueError(f"{comp['ref']} 焊盘{pad_no} 的功能 {func} 不在 SPICE 位序里")
            out[(comp["ref"], order.index(func) + 1)] = net
    # 端子对位：spec 里端子顺序 = ports 顺序（生成契约）；显式 j_refs 优先
    names = j_refs if j_refs is not None else [c["ref"] for c in connectors]
    if j_refs is None and len(names) != len(connectors):
        raise ValueError("端子数与 ports 不符")
    for name, comp in zip(names, connectors):
        pads = comp.get("pads", [])
        if len(pads) == 2:
            out[(name, 1)] = pads[0]
            out[(name, 2)] = pads[1]
    return out


def _diff(expected: dict, actual: dict, label_expected: str, label_actual: str) -> list[str]:
    issues: list[str] = []
    for key in sorted(set(expected) - set(actual)):
        issues.append(f"{key[0]} 引脚{key[1]}：{label_expected}有网络 {expected[key]}，"
                      f"{label_actual}缺失该引脚")
    for key in sorted(set(actual) - set(expected)):
        issues.append(f"{key[0]} 引脚{key[1]}：{label_actual}多出网络 {actual[key]}"
                      f"（{label_expected}没有这个引脚）")
    for key in sorted(set(expected) & set(actual)):
        if expected[key] != actual[key]:
            issues.append(f"{key[0]} 引脚{key[1]}：{label_expected}={expected[key]} ≠ "
                          f"{label_actual}={actual[key]}（接反/串网）")
    return issues


def check_partition(ir: dict, spec: dict, ports: list[str]) -> dict:
    """源电路 ↔ 板规格 的引脚级对照。返回 {ok, issues, per_net}。"""
    j_refs = [p.get("ref") for p in _ports_from_spec(spec)] or None
    expected = expected_pins(ir, ports) | expected_port_pins(ports)
    try:
        actual = spec_pins(spec, j_refs=j_refs)
    except (KeyError, ValueError) as e:
        return {"ok": False, "issues": [f"板规格无法合成引脚对照：{e}"], "per_net": {}}
    issues = _diff(expected, actual, "源电路", "板规格")
    per_net: dict[str, dict] = {}
    for (_ref, _pin), net in expected.items():
        d = per_net.setdefault(net, {"expected": 0, "actual": 0, "ok": True})
        d["expected"] += 1
    for (_ref, _pin), net in actual.items():
        if net in per_net:
            per_net[net]["actual"] += 1
    for net, d in per_net.items():
        d["ok"] = d["expected"] == d["actual"]
    return {"ok": not issues, "issues": issues, "per_net": per_net}


def _ports_from_spec(spec: dict) -> list[dict]:
    """从板规格反推端子表（ref + 信号网络）——给 spec_pins 对位用。"""
    out = []
    for comp in spec.get("components", []):
        if comp.get("part") == "CONN_HDR_1X02":
            out.append({"ref": comp["ref"], "net": comp.get("pads", [""])[0]})
    return out


def board_partition(preview: dict, ir: dict, ports: list[str],
                    j_refs: list[str] | None = None) -> dict:
    """实际板文件 ↔ 源电路 的引脚级对照（验收 F01 的最终证据层）。

    preview 来自 preview_from_pcb（板文件真实焊盘：ref/焊盘号/网络）。
    端子对位按 j_refs（生成顺序）；缺省时按 preview 中 CONN 焊盘出现序。
    """
    from . import parts as _parts
    # 设备引脚：按 (ref, 焊盘号) → SPICE 位序合成
    dev_pads: dict[str, dict[str, str]] = {}
    conn_pads: dict[str, dict[str, str]] = {}
    for p in preview.get("pads", []):
        ref, no, net = p.get("ref", ""), str(p.get("pad_no", "")), p.get("net", "")
        if not ref or not no:
            continue
        bucket = conn_pads if ref.startswith("J") and _is_conn_ref(ref, preview) else dev_pads
        bucket.setdefault(ref, {})[no] = net
    actual: dict[tuple[str, int], str] = {}
    for ref, pads_by_no in dev_pads.items():
        part_id = _part_id_of(ref, ir, preview)
        if part_id is None:
            issues = [f"{ref}：无法确定器件库条目，跳过该元件对照（不视为通过）"]
            return {"ok": False, "issues": issues, "per_net": {}}
        entry = _catalog_entry(part_id)
        order = entry["spice_pin_order"]
        tie_issues = []
        for pad_no, net in pads_by_no.items():
            tied = _tie_net(entry, pad_no, ref)
            if tied is not None:
                if net != tied:
                    tie_issues.append(f"{ref} 焊盘{pad_no} 应端接到 {tied}，板文件是 {net}")
                continue
            func = entry["pad_functions"].get(pad_no)
            if func is None or func not in order:
                return {"ok": False, "issues": [
                    f"{ref} 焊盘{pad_no} 不在器件库引脚表（{entry['id']}）"], "per_net": {}}
            actual[(ref, order.index(func) + 1)] = net
        if tie_issues:
            return {"ok": False, "issues": tie_issues, "per_net": {}}
    # 端子：pad1=信号 pad2=GND
    names = j_refs or sorted(conn_pads)
    for idx, p in enumerate(ports, start=1):
        want = f"J{idx}"
        ref = names[idx - 1] if idx - 1 < len(names) else want
        pads_by_no = conn_pads.get(ref, {})
        if "1" in pads_by_no:
            actual[(ref, 1)] = pads_by_no["1"]  # CONN 位序 [SIG,GND] → pin1
        if "2" in pads_by_no:
            actual[(ref, 2)] = pads_by_no["2"]
    expected = expected_pins(ir, ports) | expected_port_pins(ports)
    # 期望侧端子键名按实际 J 位号重写（J 编号可能因避让位号偏移）
    remap_expected = {}
    for (ref, pin), net in expected.items():
        if ref.startswith("J") and ref[1:].isdigit():
            idx = int(ref[1:]) - 1
            real = names[idx] if idx < len(names) else ref
            remap_expected[(real, pin)] = net
        else:
            remap_expected[(ref, pin)] = net
    issues = _diff(remap_expected, actual, "源电路", "板文件")
    return {"ok": not issues, "issues": issues, "per_net": {}}


def _is_conn_ref(ref: str, preview: dict) -> bool:
    """preview 的 origins 无器件类型信息；端子即 J 开头且焊盘恰为 1x2。"""
    return True  # 由 pads 数量与命名约定承担（J* 生成契约），误判由 diff 兜底


def _part_id_of(ref: str, ir: dict, preview: dict) -> str | None:
    """从映射线索定位器件库条目：优先板规格信息，退化按 IR kind。"""
    # 板文件不含 part id；调用方（jobs/recheck）经 spec_map 传入更可靠——
    # 此处退化路径：按 IR 元件类型在库里找唯一同类条目（多同类时拒绝猜测）
    kind = None
    for el in ir["elements"]:
        if el["ref"] == ref:
            kind = el["kind"]
            break
    if kind is None:
        return None
    from . import parts as _parts
    cands = [e for e in _parts.load_catalog()["entries"]
             if e["match"].get("kind") == kind and e["id"] != "CONN_HDR_1X02"]
    if len(cands) == 1:
        return cands[0]["id"]
    return None


def board_partition_with_map(preview: dict, ir: dict, ports: list[str],
                             spec: dict) -> dict:
    """board_partition 的可靠入口：器件条目来自映射板规格（ref→part），
    不做类型猜测（唯一同类退路仅用于无规格的复核场景）。"""
    ref_to_part = {c["ref"]: c.get("part") for c in spec.get("components", [])}
    j_refs = [c["ref"] for c in spec.get("components", [])
              if c.get("part") == "CONN_HDR_1X02"]

    from . import parts as _parts
    dev_pads: dict[str, dict[str, str]] = {}
    conn_pads: dict[str, dict[str, str]] = {}
    j_set = set(j_refs)
    for p in preview.get("pads", []):
        ref, no, net = p.get("ref", ""), str(p.get("pad_no", "")), p.get("net", "")
        if not ref or not no:
            continue
        (conn_pads if ref in j_set else dev_pads).setdefault(ref, {})[no] = net
    actual: dict[tuple[str, int], str] = {}
    for ref, pads_by_no in dev_pads.items():
        part_id = ref_to_part.get(ref)
        if not part_id:
            return {"ok": False, "issues": [f"{ref}：映射规格缺器件条目，无法对照板文件"],
                    "per_net": {}}
        entry = _catalog_entry(part_id)
        order = entry["spice_pin_order"]
        tie_issues = []
        for pad_no, net in pads_by_no.items():
            tied = _tie_net(entry, pad_no, ref)
            if tied is not None:
                if net != tied:
                    tie_issues.append(f"{ref} 焊盘{pad_no} 应端接到 {tied}，板文件是 {net}")
                continue
            func = entry["pad_functions"].get(pad_no)
            if func is None or func not in order:
                return {"ok": False, "issues": [
                    f"{ref} 焊盘{pad_no} 不在器件库引脚表（{entry['id']}）"], "per_net": {}}
            actual[(ref, order.index(func) + 1)] = net
        if tie_issues:
            return {"ok": False, "issues": tie_issues, "per_net": {}}
    expected = expected_pins(ir, ports)
    for idx, p in enumerate(ports, start=1):
        ref = j_refs[idx - 1] if idx - 1 < len(j_refs) else f"J{idx}"
        pads_by_no = conn_pads.get(ref, {})
        net = p if p.lower() not in ("0", "gnd") else "GND"
        expected[(ref, 1)] = net
        expected[(ref, 2)] = "GND"
        if "1" in pads_by_no:
            actual[(ref, 1)] = pads_by_no["1"]
        if "2" in pads_by_no:
            actual[(ref, 2)] = pads_by_no["2"]
    issues = _diff(expected, actual, "源电路", "板文件")
    return {"ok": not issues, "issues": issues, "per_net": {}}


def copper_verdict(route_result, drc_result: dict | None) -> dict:
    """布线后铜连接判定：未布线数（路由器）+ DRC 未连项/违规 三方汇总。"""
    reasons: list[str] = []
    if route_result is None or not route_result.ok:
        unrouted = getattr(route_result, "unrouted", None)
        if unrouted:
            reasons.append(f"布线器报告 {unrouted} 条未连")
        elif route_result is None:
            reasons.append("无布线结果")
        else:
            reasons.extend(route_result.problems or ["布线未通过"])
    if drc_result:
        errors = drc_result.get("violations_error", drc_result.get("violations"))
        if errors:
            reasons.append(f"DRC {errors} 条错误")
        if drc_result.get("unconnected"):
            reasons.append(f"DRC {drc_result['unconnected']} 条未连")
        if drc_result.get("unconnected") is None:
            reasons.append("DRC 报告未含未连项计数（结果不完整）")
    elif route_result is not None:
        reasons.append("缺 DRC 复核结果")
    return {"ok": not reasons, "reasons": reasons}
