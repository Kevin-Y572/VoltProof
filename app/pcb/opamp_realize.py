"""理想运放 → 审校库 LM358 的新电路候选。

三脚理想运放不能直接变成封装。这里按子电路里的 E/G 源辨认同相、反相和输出，
补上正负电源与去耦，写成目录里 LM358 的五脚位序，再仿真。通过后只新增
项目候选，不改当前已经采用的版本。

仿真用的 LM358 是行为级替身（保留原增益），不是厂家宏模型。双运放里没用到的
那一路只在 PCB 焊盘上端接，不写进这个子电路。电源或脚序补不齐时返回阻断项，
不产出半块能映射的板。
"""
from __future__ import annotations

import re

from .. import i18n, project

_GAIN = re.compile(
    r"^[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?\d+)?(?:[kKmMuUnNpP]|meg|Meg)?$")
_IDEAL_NAMES = {"opamp", "ideal", "ideal_opamp"}


def assess(netlist: str) -> dict:
    """预检用：要不要提供「换成 LM358」这一步。"""
    found = _scan(netlist)
    if found["errors"]:
        return {"needed": True, "ready": False, "blockers": found["errors"]}
    if not found["ideals"]:
        return {"needed": False, "ready": False, "blockers": []}
    built = realize_netlist(netlist)
    return {"needed": True, "ready": bool(built["ok"]), "blockers": built["blockers"]}


def realize_netlist(netlist: str) -> dict:
    """纯变换。成功时 netlist 是新文本；失败时 blockers 说明缺什么。"""
    found = _scan(netlist)
    if found["errors"]:
        return {"ok": False, "netlist": None, "blockers": found["errors"], "changes": []}
    ideals = found["ideals"]
    if not ideals:
        return {"ok": False, "netlist": None, "blockers": [
            {"code": "no_ideal", "reason": i18n.t("pcb.opamp.none")}], "changes": []}
    if not found["has_ground"]:
        return {"ok": False, "netlist": None, "blockers": [
            {"code": "no_ground", "reason": i18n.t("pcb.opamp.no_ground")}], "changes": []}
    rails = found["rails"]
    if rails["count"] == 1:
        only = rails["pos"] or rails["neg"]
        return {"ok": False, "netlist": None, "blockers": [{
            "code": "single_rail",
            "reason": i18n.t("pcb.opamp.single_rail", ref=only["ref"], net=only["net"]),
        }], "changes": []}

    gains = []
    for item in ideals:
        if not _GAIN.match(item["gain"]):
            return {"ok": False, "netlist": None, "blockers": [{
                "code": "gain_unsafe",
                "reason": i18n.t("pcb.opamp.gain", ref=item["ref"], gain=item["gain"]),
            }], "changes": []}
        gains.append(item["gain"])

    existing_nets = found["nets"]
    changes = []
    if rails["pos"] and rails["neg"]:
        vcc, vee = rails["pos"]["net"], rails["neg"]["net"]
        add_sources = []
    else:
        vcc = _fresh(existing_nets, "vcc")
        vee = _fresh(existing_nets | {vcc}, "vee")
        existing_nets |= {vcc, vee}
        add_sources = [
            ( _fresh_ref(found["refs"], "VCCSRC"), vcc, "12"),
            ( _fresh_ref(found["refs"], "VEESRC"), vee, "-12"),
        ]
        for ref, net, volts in add_sources:
            found["refs"].add(ref)
            changes.append({"op": "add_supply", "ref": ref, "net": net, "volts": volts})

    for item in ideals:
        for net in (item["inp"], item["inn"], item["out"], vcc, vee):
            if net in {vcc, vee} and net in {item["inp"], item["inn"], item["out"]}:
                return {"ok": False, "netlist": None, "blockers": [{
                    "code": "supply_short",
                    "reason": i18n.t("pcb.opamp.short", ref=item["ref"], net=net),
                }], "changes": []}

    caps = []
    for net, base in ((vcc, "CDVCC"), (vee, "CDVEE")):
        if not _has_decap(found["caps"], net):
            ref = _fresh_ref(found["refs"], base)
            found["refs"].add(ref)
            caps.append((ref, net))
            changes.append({"op": "add_decap", "ref": ref, "net": net, "value": "100n"})

    models = _models_for(ideals)
    for item in ideals:
        changes.append({
            "op": "replace_opamp",
            "ref": item["ref"],
            "model": models[item["gain"]],
            "pins": {"IN+": item["inp"], "IN-": item["inn"], "V+": vcc, "V-": vee, "OUT": item["out"]},
        })

    text = _render(netlist, ideals, add_sources, caps, models, vcc, vee)
    return {"ok": True, "netlist": text, "blockers": [], "changes": changes,
            "supply": {"vcc": vcc, "vee": vee}}


def reverify_candidate(ws, netlist: str, *, title: str, requirements=None,
                       changes=None, simulate=None) -> dict:
    """仿真通过后写入新候选。不调用 project.apply。"""
    before = ws.load_project().get("netlist")
    sim = simulate(netlist) if simulate is not None else _simulate_real(
        ws, netlist, requirements or [])
    if not sim.get("ok"):
        return {"ok": False, "applied": False, "version_id": None,
                "blockers": sim.get("blockers") or [], "netlist": netlist, "changes": changes or []}
    adopted = ws.load_project()
    evidence = {
        "netlist": netlist,
        "ok": True,
        "snapshot": True,
        "verdict": sim.get("verdict") or "simulated",
        "source": "opamp-realize",
        "task_id": sim.get("task_id") or "",
        "requirements": requirements or [],
    }
    canvas = sim.get("canvas") or {"comps": [], "wires": [], "labels": []}
    version = project.candidate(ws, evidence, canvas, project.revision(adopted))
    after = ws.load_project().get("netlist")
    if after != before:
        return {"ok": False, "applied": True, "version_id": version["id"], "blockers": [{
            "code": "mutated",
            "reason": i18n.t("pcb.opamp.mutated"),
        }]}
    return {
        "ok": True,
        "applied": False,
        "version_id": version["id"],
        "base_revision": version["base_revision"],
        "verdict": evidence["verdict"],
        "netlist": netlist,
        "changes": changes or [],
        "blockers": [],
        "note": i18n.t("pcb.opamp.note"),
    }


def _simulate_real(ws, netlist: str, requirements: list) -> dict:
    from .. import checks, measure, specs
    from ..ngspice_runner import check_netlist_safety, run_netlist

    tid = ws.new_task_id()
    sim_dir = ws.task_sim_dir(tid)
    problems = check_netlist_safety(netlist, root=sim_dir)
    if problems:
        return {"ok": False, "blockers": [{"code": "safety", "reason": p} for p in problems]}
    chk = checks.run_checks(netlist)
    if not chk.ok:
        return {"ok": False, "blockers": [{"code": "checks", "reason": e} for e in chk.errors]}
    sim = run_netlist(netlist, workdir=sim_dir)
    if not sim.ok or sim.raw_path is None:
        return {"ok": False, "blockers": [{
            "code": "sim_failed",
            "reason": sim.error_snippet or i18n.t("pcb.opamp.sim"),
        }]}
    try:
        traces = measure.load_traces(sim.raw_path)
        measure.extract_metrics(traces)
    except Exception as exc:  # noqa: BLE001 —— 波形读不出来就不能当成验证通过
        return {"ok": False, "blockers": [{
            "code": "measure", "reason": i18n.t("pcb.opamp.measure", msg=str(exc)[:200])}]}
    if requirements:
        rows = specs.evaluate(traces, requirements, netlist)
        failed = [c for c in rows if not c.get("ok")]
        if failed:
            return {"ok": False, "blockers": [{
                "code": "requirements",
                "reason": c.get("detail") or c.get("name") or i18n.t("pcb.opamp.requirements"),
            } for c in failed]}
        verdict = "passed"
    else:
        verdict = "simulated"
    return {"ok": True, "verdict": verdict, "task_id": tid}


def _scan(netlist: str) -> dict:
    lines = netlist.splitlines()
    subckts, dup_errors = _subckts(lines)
    errors = list(dup_errors)
    ideals = []
    refs = set()
    nets = set()
    caps = []
    rails = {"pos": None, "neg": None, "count": 0}
    has_ground = False
    depth = 0
    in_control = False
    body_lines = lines[1:] if lines else []
    # 子电路内部的 0 也算有地（理想模型几乎都把输出参考接到 0）。
    for raw in lines:
        for tok in raw.split():
            if tok.lower() in {"0", "gnd"}:
                has_ground = True
    for raw in body_lines:
        stripped = raw.strip()
        low = stripped.lower()
        if low == ".control":
            in_control = True
            continue
        if low == ".endc":
            in_control = False
            continue
        if low.startswith(".subckt"):
            depth += 1
            continue
        if low.startswith(".ends"):
            depth = max(0, depth - 1)
            continue
        if depth or in_control or not stripped or stripped.startswith(("*", ".", "#")):
            continue
        toks = stripped.split()
        if not toks or not toks[0][0].isalpha():
            continue
        kind = toks[0][0].upper()
        refs.add(toks[0])
        if kind == "X" and len(toks) >= 3:
            model = toks[-1]
            nodes = toks[1:-1]
            nets.update(nodes)
            sub = subckts.get(model.lower())
            ideal_name = model.lower() in _IDEAL_NAMES
            three = sub is not None and len(sub["pins"]) == 3 and len(nodes) == 3
            if three or (ideal_name and len(nodes) == 3):
                if sub is None:
                    errors.append({
                        "code": "unparsed_subckt",
                        "ref": toks[0],
                        "reason": i18n.t("pcb.opamp.no_subckt", ref=toks[0], model=model),
                    })
                    continue
                roles = _roles(sub)
                if roles is None:
                    errors.append({
                        "code": "unparsed_subckt",
                        "ref": toks[0],
                        "reason": i18n.t("pcb.opamp.unparsed", ref=toks[0], model=model),
                    })
                    continue
                index = {pin: i for i, pin in enumerate(sub["pins"])}
                ideals.append({
                    "ref": toks[0],
                    "model": model,
                    "inp": nodes[index[roles["IN+"]]],
                    "inn": nodes[index[roles["IN-"]]],
                    "out": nodes[index[roles["OUT"]]],
                    "gain": roles["gain"],
                    "line": stripped,
                })
            continue
        if kind == "V" and len(toks) >= 4:
            n1, n2 = toks[1], toks[2]
            nets.update((n1, n2))
            rail = _rail(toks)
            supply_net = n1 if n2.lower() in {"0", "gnd"} else n2
            if rail is not None and _looks_like_rail(toks[0], supply_net):
                slot = "pos" if rail > 0 else "neg"
                if rails[slot] is None:
                    rails[slot] = {"ref": toks[0], "net": supply_net, "volts": rail}
                    rails["count"] = int(rails["pos"] is not None) + int(rails["neg"] is not None)
        if kind == "C" and len(toks) >= 4:
            nets.update((toks[1], toks[2]))
            caps.append({"ref": toks[0], "nets": {toks[1], toks[2]}, "value": toks[3]})
        if kind in "RCLD" and len(toks) >= 3:
            nets.update(toks[1:3])
    if rails["pos"] and rails["neg"] and rails["pos"]["net"] == rails["neg"]["net"]:
        errors.append({
            "code": "single_rail",
            "reason": i18n.t("pcb.opamp.same_net", net=rails["pos"]["net"]),
        })
    return {"ideals": ideals, "errors": errors, "refs": refs, "nets": nets,
            "caps": caps, "rails": rails, "has_ground": has_ground, "subckts": subckts}


def _subckts(lines: list[str]) -> tuple[dict, list[dict]]:
    found = {}
    errors = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.lower().startswith(".subckt"):
            toks = stripped.split()
            if len(toks) < 2:
                i += 1
                continue
            name = toks[1]
            pins = toks[2:]
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().lower().startswith(".ends"):
                body.append(lines[i])
                i += 1
            key = name.lower()
            if key in found:
                errors.append({
                    "code": "unparsed_subckt",
                    "reason": i18n.t("pcb.opamp.dup_subckt", model=name),
                })
            found[key] = {"name": name, "pins": pins, "body": body}
        i += 1
    return found, errors


def _roles(sub: dict) -> dict | None:
    pins = set(sub["pins"])
    if len(sub["pins"]) != 3:
        return None
    for raw in sub["body"]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("*"):
            continue
        toks = stripped.split()
        if not toks or toks[0][0].upper() not in "EG" or len(toks) < 6:
            continue
        n_plus, n_minus, nc_plus, nc_minus, gain = toks[1:6]
        if n_minus.lower() not in {"0", "gnd"}:
            continue
        if n_plus in pins and nc_plus in pins and nc_minus in pins and len({n_plus, nc_plus, nc_minus}) == 3:
            return {"IN+": nc_plus, "IN-": nc_minus, "OUT": n_plus, "gain": gain}
    return None


def _rail(toks: list[str]) -> float | None:
    """顶层直流电源：一端接地，且不是 SIN/PULSE 信号源。"""
    if toks[2].lower() not in {"0", "gnd"} and toks[1].lower() not in {"0", "gnd"}:
        return None
    upper = [t.upper() for t in toks]
    if any(t.startswith(("SIN", "PULSE", "EXP", "SFFM", "AM")) for t in upper):
        return None
    value = None
    if "DC" in upper:
        idx = upper.index("DC")
        if idx + 1 < len(toks):
            value = _volts(toks[idx + 1])
    if value is None and len(toks) >= 4:
        value = _volts(toks[3])
    if value is None or abs(value) < 1.5:
        return None
    return value


def _looks_like_rail(ref: str, net: str) -> bool:
    """信号源（Vin/in）不是电源轨。名字里带 vcc/vee/vdd/vss 才复用。"""
    blob = f"{ref} {net}".lower()
    return any(key in blob for key in ("vcc", "vee", "vdd", "vss", "v+", "v-", "rail"))


def _volts(token: str) -> float | None:
    match = re.fullmatch(r"([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(meg|Meg|[kKmMuUnNpP])?", token)
    if not match:
        return None
    scale = {None: 1, "": 1, "k": 1e3, "K": 1e3, "meg": 1e6, "Meg": 1e6,
             "m": 1e-3, "u": 1e-6, "U": 1e-6, "n": 1e-9, "N": 1e-9, "p": 1e-12, "P": 1e-12}
    try:
        return float(match.group(1)) * scale[match.group(2)]
    except (ValueError, KeyError):
        return None


def _has_decap(caps: list[dict], net: str) -> bool:
    from .parts import _parse_value
    for cap in caps:
        if net not in cap["nets"]:
            continue
        other = next(iter(cap["nets"] - {net}), "")
        if other.lower() not in {"0", "gnd"}:
            continue
        farads = _parse_value(str(cap["value"]))
        if farads is not None and farads <= 1e-6:
            return True
    return False


def _fresh(nets: set[str], base: str) -> str:
    if base not in nets and base.lower() not in {n.lower() for n in nets}:
        return base
    n = 2
    while f"{base}{n}" in nets:
        n += 1
    return f"{base}{n}"


def _fresh_ref(refs: set[str], base: str) -> str:
    if base not in refs:
        return base
    n = 2
    while f"{base}{n}" in refs:
        n += 1
    return f"{base}{n}"


def _models_for(ideals: list[dict]) -> dict[str, str]:
    """增益相同共用一个子电路名；不同增益用仍含 lm358 的名字，好命中器件库。"""
    names = {}
    for gain in dict.fromkeys(item["gain"] for item in ideals):
        names[gain] = "LM358" if not names else f"LM358G{len(names) + 1}"
    if len(names) == 1:
        only = next(iter(names))
        names[only] = "LM358"
    return names


def _render(netlist: str, ideals, sources, caps, models, vcc: str, vee: str) -> str:
    replaced = {item["line"] for item in ideals}
    ideal_names = {item["model"].lower() for item in ideals}
    lines = netlist.splitlines()
    out = []
    depth = 0
    skipping = False
    for raw in lines:
        stripped = raw.strip()
        low = stripped.lower()
        if low.startswith(".subckt"):
            name = stripped.split()[1].lower() if len(stripped.split()) > 1 else ""
            depth += 1
            skipping = name in ideal_names
            if skipping:
                continue
        if low.startswith(".ends"):
            ended = skipping
            depth = max(0, depth - 1)
            if ended:
                skipping = False
                continue
        if skipping:
            continue
        if stripped in replaced:
            item = next(it for it in ideals if it["line"] == stripped)
            model = models[item["gain"]]
            out.append(f"{item['ref']} {item['inp']} {item['inn']} {vcc} {vee} {item['out']} {model}")
            continue
        if low == ".end":
            break
        out.append(raw)
    added = [f"{ref} {net} 0 DC {volts}" for ref, net, volts in sources]
    added += [f"{ref} {net} 0 100n" for ref, net in caps]
    seen = set()
    for gain, name in models.items():
        if name in seen:
            continue
        seen.add(name)
        added += [
            f".subckt {name} inp inn vp vm out",
            "* VoltProof behavioral stand-in for the LM358 pinout. Not a vendor macromodel.",
            f"E1 out 0 inp inn {gain}",
            "Rbp vp 0 10Meg",
            "Rbm vm 0 10Meg",
            ".ends",
        ]
    insert_at = next((i for i, line in enumerate(out)
                      if line.strip().lower().startswith((".control", ".tran", ".ac", ".op", ".dc"))),
                     len(out))
    out[insert_at:insert_at] = added
    out.append(".end")
    return "\n".join(out) + "\n"
