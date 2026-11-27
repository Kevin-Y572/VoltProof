"""布局代理：模型只许提出坐标和有界修改，执行和否决都在确定性工具里。

模型不可用、提案不合法、或用户没打开这一步时，退回信号流布局。
同一份不走模型的输入仍由信号流 + 合法化决定，结果可复现。

走线仍是 Freerouting。本模块不向模型要铜皮坐标，也不放宽制造底线。
修复回合会整板重新导出 DSN：未锁定的旧走线不会保留。手工重布线路径
可以跳过 IsLocked 的走线，这里不会把旧铜锁上冒充增量布线。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .. import llm
from . import legalizer
from .constraints import _issue, _num

ROUTE_REUSE = (
    "本轮重新导出 DSN 并完整调用 Freerouting。"
    "未锁定的已布走线不会保留：手工重布线可以跳过 IsLocked 的走线，"
    "布局代理不会把旧铜锁定来假装增量布线。"
)

_MAX_REPAIRS = 2
_EDIT_KEYS = {
    "move": {"op", "ref", "x", "y"},
    "rotate": {"op", "ref", "rot"},
    "enlarge_board": {"op", "w", "h"},
    "net_class": {"op", "name", "nets", "track_mm", "clearance_mm", "priority", "pour"},
    "keepout": {"op", "x", "y", "w", "h", "net"},
}


def choose_placement(spec: dict, rules: dict, agent: dict | None, archive: Path) -> dict:
    """选定一份合法布局。成功时改 rules['placement'] 并写 archive/agent/placement.json。"""
    opts = agent or {}
    enabled = bool(opts.get("enabled"))
    record = {
        "source": "signal_flow",
        "fallback": None,
        "provider": None,
        "prompt_sha256": None,
        "raw_proposal": None,
        "validation": None,
        "repairs": [],
        "ok": False,
        "errors": [],
        "route_reuse": ROUTE_REUSE,
    }
    if enabled:
        system, user = _placement_prompt(spec, rules)
        record["prompt_sha256"] = _sha(system + "\n" + user)
        try:
            raw, provider = _invoke_propose(system, user, opts)
            record["raw_proposal"] = raw
            record["provider"] = provider
            issues = validate_proposal(raw, spec, rules)
            record["validation"] = {"ok": not issues, "errors": issues}
            if not issues:
                seeded = apply_proposal(spec, raw)
                placed = legalizer.legalize(seeded, rules)
                if placed["ok"]:
                    record["source"] = "model"
                    record["repairs"] = placed["repairs"]
                    record["ok"] = True
                    record["spec"] = placed["spec"]
                    rules["placement"] = "model"
                    _write_json(Path(archive) / "agent" / "placement.json", _public(record))
                    return record
                record["fallback"] = "legalizer_rejected"
                record["validation"]["legalizer"] = placed["errors"]
            else:
                record["fallback"] = "proposal_invalid"
        except Exception as exc:  # noqa: BLE001 —— 模型失败必须退回，不能中断确定性路径
            record["fallback"] = "model_unavailable"
            record["provider"] = record["provider"] or {"provider": "unavailable", "model": ""}
            record["validation"] = {"ok": False, "errors": [
                _issue("model", str(exc)[:300])]}

    placed = legalizer.legalize(spec, rules)
    rules["placement"] = "signal_flow"
    record["source"] = "signal_flow"
    record["repairs"] = placed["repairs"]
    if not placed["ok"]:
        record["ok"] = False
        record["errors"] = placed["errors"]
        record["spec"] = None
    else:
        record["ok"] = True
        record["spec"] = placed["spec"]
        record["errors"] = []
    _write_json(Path(archive) / "agent" / "placement.json", _public(record))
    return record


def validate_proposal(raw, spec: dict, rules: dict) -> list[dict]:
    """提案只许含板尺寸和位号坐标。多出来的字段（含走线）一律拒绝。"""
    issues: list[dict] = []
    if not isinstance(raw, dict):
        return [_issue("proposal", "布局提案必须是 JSON 对象")]
    extra = set(raw) - {"board_mm", "placements"}
    if extra:
        issues.append(_issue("proposal", "提案含未允许的字段：" + ", ".join(sorted(extra))))
    placements = raw.get("placements")
    if not isinstance(placements, list):
        issues.append(_issue("proposal", "placements 必须是数组"))
        return issues
    known = {c["ref"]: c for c in spec.get("components") or []}
    seen = []
    allowed = rules.get("rotations") or {"*": [0, 90, 180, 270]}
    width, height = float(spec["size_mm"][0]), float(spec["size_mm"][1])
    board = raw.get("board_mm")
    if board is not None:
        if (not isinstance(board, list) or len(board) != 2):
            issues.append(_issue("proposal", "board_mm 必须是 [宽, 高]"))
        else:
            bw = _num(board[0], width, 500, "board_mm[0]", issues)
            bh = _num(board[1], height, 500, "board_mm[1]", issues)
            if bw is not None and bw + 1e-6 < width:
                issues.append(_issue("proposal", "提案把板宽缩小了"))
            if bh is not None and bh + 1e-6 < height:
                issues.append(_issue("proposal", "提案把板高缩小了"))
    for index, item in enumerate(placements):
        if not isinstance(item, dict):
            issues.append(_issue("proposal", f"placements[{index}] 必须是对象"))
            continue
        extra_item = set(item) - {"ref", "x", "y", "rot"}
        if extra_item:
            issues.append(_issue("proposal", f"{item.get('ref')} 含未允许的字段 {sorted(extra_item)}"))
        ref = str(item.get("ref") or "")
        if ref not in known:
            issues.append(_issue("proposal", f"未知位号 {ref}", ref=ref))
            continue
        seen.append(ref)
        _num(item.get("x"), 0, 500, f"{ref}.x", issues)
        _num(item.get("y"), 0, 500, f"{ref}.y", issues)
        rot = item.get("rot", 0)
        opts = allowed.get(ref, allowed.get("*", [0, 90, 180, 270]))
        if isinstance(rot, bool) or not isinstance(rot, (int, float)) or int(rot) % 360 not in {int(a) % 360 for a in opts}:
            issues.append(_issue("rotation", f"{ref} 的角度 {rot} 不在允许集合 {opts}", ref=ref))
    missing = sorted(set(known) - set(seen))
    if missing:
        issues.append(_issue("proposal", "提案不是完整布局，缺少 " + ", ".join(missing)))
    dupes = sorted({r for r in seen if seen.count(r) > 1})
    if dupes:
        issues.append(_issue("proposal", "提案重复位号 " + ", ".join(dupes)))
    return issues


def apply_proposal(spec: dict, raw: dict) -> dict:
    spec = copy.deepcopy(spec)
    by_ref = {c["ref"]: c for c in spec["components"]}
    old = (float(spec["size_mm"][0]), float(spec["size_mm"][1]))
    board = raw.get("board_mm")
    if isinstance(board, list) and len(board) == 2:
        spec["size_mm"] = [float(board[0]), float(board[1])]
        _resize_standard_zones(spec, old, spec["size_mm"])
    for item in raw["placements"]:
        comp = by_ref[str(item["ref"])]
        comp["x"] = round(float(item["x"]), 2)
        comp["y"] = round(float(item["y"]), 2)
        comp["rot"] = int(item.get("rot") or 0) % 360
    return spec


def collect_evidence(archive: Path, route_result, drc_result, problems: list[str],
                     rules: dict, spec: dict) -> dict:
    """布线或 DRC 失败时给模型看的证据。不含制造底线的放宽许可。"""
    items = []
    drc_path = Path(archive) / "checks" / "drc.json"
    if drc_path.is_file():
        try:
            data = json.loads(drc_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for viol in (data.get("violations") or [])[:20]:
            if not isinstance(viol, dict):
                continue
            items.append({
                "type": viol.get("type"),
                "severity": viol.get("severity"),
                "description": str(viol.get("description") or "")[:200],
                "pos": _violation_pos(viol),
            })
    unrouted = None
    route_problems = []
    if route_result is not None:
        unrouted = getattr(route_result, "unrouted", None)
        route_problems = list(getattr(route_result, "problems", []) or [])
    return {
        "unrouted_count": unrouted,
        "route_problems": route_problems[:8],
        "drc_violations": None if not drc_result else drc_result.get("violations"),
        "drc_unconnected": None if not drc_result else drc_result.get("unconnected"),
        "drc_items": items,
        "check_problems": [str(p)[:300] for p in problems[:8]],
        "board_mm": list(spec.get("size_mm") or []),
        "placement": [
            {"ref": c["ref"], "x": c.get("x"), "y": c.get("y"), "rot": c.get("rot", 0)}
            for c in spec.get("components") or []
        ],
        "constraints": {
            "track_mm": rules.get("track_mm"),
            "clearance_mm": rules.get("clearance_mm"),
            "net_classes": rules.get("net_classes") or [],
            "keepouts": rules.get("keepouts") or [],
            "groups": [{"id": g.get("id"), "refs": g.get("refs")} for g in rules.get("groups") or []],
            "proximity": rules.get("proximity") or [],
        },
        "route_reuse": ROUTE_REUSE,
        "forbidden": ["relax_clearance", "relax_track", "relax_via", "tracks", "kicad_pcb"],
    }


def apply_repair(spec: dict, rules: dict, raw, archive: Path, round_index: int,
                 evidence: dict) -> dict:
    """校验并应用一轮修复。不合法则整轮作废，规则保持原样。"""
    folder = Path(archive) / "agent" / f"round-{round_index}"
    folder.mkdir(parents=True, exist_ok=True)
    proposal_issues = validate_edits(raw, spec, rules)
    _write_json(folder / "proposal.json", raw if isinstance(raw, dict) else {"raw": str(raw)[:500]})
    _write_json(folder / "evidence.json", evidence)
    if proposal_issues:
        _write_json(folder / "validation.json", {"ok": False, "errors": proposal_issues, "applied": False})
        _write_json(folder / "result.json", {
            "ok": False, "applied": False, "errors": proposal_issues, "route_reuse": ROUTE_REUSE})
        return {"ok": False, "errors": proposal_issues, "spec": spec, "rules": rules}

    new_spec, new_rules, apply_issues = _apply_edits(spec, rules, raw["edits"])
    if apply_issues:
        _write_json(folder / "validation.json", {"ok": False, "errors": apply_issues, "applied": False})
        _write_json(folder / "result.json", {
            "ok": False, "applied": False, "errors": apply_issues, "route_reuse": ROUTE_REUSE})
        return {"ok": False, "errors": apply_issues, "spec": spec, "rules": rules}

    placed = legalizer.legalize(new_spec, new_rules)
    validation = {"ok": placed["ok"], "errors": placed["errors"], "repairs": placed["repairs"],
                  "applied": placed["ok"]}
    _write_json(folder / "validation.json", validation)
    if not placed["ok"]:
        _write_json(folder / "result.json", {
            "ok": False, "applied": False, "errors": placed["errors"], "route_reuse": ROUTE_REUSE})
        return {"ok": False, "errors": placed["errors"], "spec": spec, "rules": rules}
    _write_json(folder / "result.json", {
        "ok": True,
        "applied": True,
        "pending_reroute": True,
        "board_mm": placed["spec"]["size_mm"],
        "route_reuse": ROUTE_REUSE,
        "note": "合法化已通过。随后的布线结果在本轮 reroute 字段补上。",
    })
    return {"ok": True, "errors": [], "spec": placed["spec"], "rules": new_rules, "folder": str(folder)}


def finish_round(folder: Path | str, outcome: dict) -> None:
    path = Path(folder) / "result.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        current = {}
    current["reroute"] = outcome
    current["route_reuse"] = ROUTE_REUSE
    current["pending_reroute"] = False
    _write_json(path, current)


def validate_edits(raw, spec: dict, rules: dict) -> list[dict]:
    issues: list[dict] = []
    if not isinstance(raw, dict):
        return [_issue("repair", "修复提案必须是 JSON 对象")]
    extra = set(raw) - {"edits"}
    if extra:
        issues.append(_issue(
            "repair",
            "修复菜单只接受 edits。未识别字段：" + ", ".join(sorted(map(str, extra)))))
    edits = raw.get("edits")
    if not isinstance(edits, list) or not edits:
        issues.append(_issue("repair", "edits 必须是非空数组"))
        return issues
    if len(edits) > 8:
        issues.append(_issue("repair", "一轮最多 8 条修改"))
    known = {c["ref"] for c in spec.get("components") or []}
    allowed = rules.get("rotations") or {"*": [0, 90, 180, 270]}
    floor_t = float(rules["track_mm"])
    floor_c = float(rules["clearance_mm"])
    classes = {c["name"]: c for c in rules.get("net_classes") or []}
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            issues.append(_issue("repair", f"edits[{index}] 必须是对象"))
            continue
        op = edit.get("op")
        if op not in _EDIT_KEYS:
            issues.append(_issue("repair", f"不支持的修改 {op!r}。允许：move/rotate/enlarge_board/net_class/keepout"))
            continue
        extra_item = set(edit) - _EDIT_KEYS[op]
        if extra_item:
            issues.append(_issue("repair", f"{op} 含未允许的字段 {sorted(extra_item)}"))
        if op in {"move", "rotate"}:
            ref = str(edit.get("ref") or "")
            if ref not in known:
                issues.append(_issue("repair", f"未知位号 {ref}", ref=ref))
        if op == "move":
            _num(edit.get("x"), 0, 500, f"move.{edit.get('ref')}.x", issues)
            _num(edit.get("y"), 0, 500, f"move.{edit.get('ref')}.y", issues)
        if op == "rotate":
            ref = str(edit.get("ref") or "")
            rot = edit.get("rot")
            opts = {int(a) % 360 for a in allowed.get(ref, allowed.get("*", [0, 90, 180, 270]))}
            if isinstance(rot, bool) or not isinstance(rot, (int, float)) or int(rot) % 360 not in opts:
                issues.append(_issue("rotation", f"{ref} 的角度 {rot} 不在允许集合 {sorted(opts)}", ref=ref))
        if op == "enlarge_board":
            w = _num(edit.get("w"), 5, 500, "enlarge.w", issues)
            h = _num(edit.get("h"), 5, 500, "enlarge.h", issues)
            if w is not None and w + 1e-6 < float(spec["size_mm"][0]):
                issues.append(_issue("repair", "enlarge_board 不能把板改小"))
            if h is not None and h + 1e-6 < float(spec["size_mm"][1]):
                issues.append(_issue("repair", "enlarge_board 不能把板改小"))
        if op == "net_class":
            name = str(edit.get("name") or "")
            track = _num(edit.get("track_mm", floor_t), 0.05, 10, f"{name}.track_mm", issues)
            clear = _num(edit.get("clearance_mm", floor_c), 0.05, 10, f"{name}.clearance_mm", issues)
            existing = classes.get(name)
            current_t = float(existing["track_mm"]) if existing else floor_t
            current_c = float(existing["clearance_mm"]) if existing else floor_c
            if track is not None and (track + 1e-9 < floor_t or track + 1e-9 < current_t):
                issues.append(_issue(
                    "relax",
                    f"网络类 {name} 的线宽不能低于制造底线 {floor_t}mm 或当前 {current_t}mm"))
            if clear is not None and (clear + 1e-9 < floor_c or clear + 1e-9 < current_c):
                issues.append(_issue(
                    "relax",
                    f"网络类 {name} 的净距不能低于制造底线 {floor_c}mm 或当前 {current_c}mm"))
            nets = edit.get("nets")
            if not isinstance(nets, list) or not nets:
                issues.append(_issue("net_class", f"网络类 {name} 需要 nets"))
            else:
                known_nets = set(spec.get("nets") or [])
                for net in nets:
                    if str(net) not in known_nets:
                        issues.append(_issue("net_class", f"网络 {net} 不在板上"))
        if op == "keepout":
            _num(edit.get("x"), -10, 500, "keepout.x", issues)
            _num(edit.get("y"), -10, 500, "keepout.y", issues)
            _num(edit.get("w"), 0.2, 500, "keepout.w", issues)
            _num(edit.get("h"), 0.2, 500, "keepout.h", issues)
    return issues


def _apply_edits(spec: dict, rules: dict, edits: list[dict]) -> tuple[dict, dict, list[dict]]:
    spec = copy.deepcopy(spec)
    rules = copy.deepcopy(rules)
    issues: list[dict] = []
    by_ref = {c["ref"]: c for c in spec["components"]}
    enlarges = [e for e in edits if e.get("op") == "enlarge_board"]
    rest = [e for e in edits if e.get("op") != "enlarge_board"]
    old = (float(spec["size_mm"][0]), float(spec["size_mm"][1]))
    for edit in enlarges:
        spec["size_mm"] = [max(old[0], float(edit["w"])), max(old[1], float(edit["h"]))]
    if spec["size_mm"] != [old[0], old[1]]:
        _resize_standard_zones(spec, old, spec["size_mm"])
    for edit in rest:
        op = edit["op"]
        if op == "move":
            comp = by_ref[str(edit["ref"])]
            comp["x"] = round(float(edit["x"]), 2)
            comp["y"] = round(float(edit["y"]), 2)
        elif op == "rotate":
            comp = by_ref[str(edit["ref"])]
            comp["rot"] = int(edit["rot"]) % 360
        elif op == "keepout":
            ko = {"x": float(edit["x"]), "y": float(edit["y"]),
                  "w": float(edit["w"]), "h": float(edit["h"])}
            if edit.get("net"):
                ko["net"] = str(edit["net"])[:40]
            spec.setdefault("keepouts", []).append(ko)
            rules.setdefault("keepouts", []).append(ko)
        elif op == "net_class":
            _upsert_class(rules, edit, issues)
    _recheck_duplicate_nets(rules, issues)
    return spec, rules, issues


def _upsert_class(rules: dict, edit: dict, issues: list[dict]) -> None:
    name = str(edit["name"])
    nets = [str(n) for n in edit["nets"]]
    others = []
    for cls in rules.get("net_classes") or []:
        if cls["name"] == name:
            continue
        overlap = sorted(set(cls["nets"]) & set(nets))
        if overlap:
            issues.append(_issue(
                "net_class_conflict",
                f"网络 {overlap} 已属于 {cls['name']}，不能再划给 {name}"))
        else:
            others.append(cls)
    if issues:
        return
    prev = next((c for c in rules.get("net_classes") or [] if c["name"] == name), None)
    floor_t = float(rules["track_mm"])
    floor_c = float(rules["clearance_mm"])
    item = {
        "name": name,
        "nets": nets,
        "track_mm": float(edit.get("track_mm", prev["track_mm"] if prev else floor_t)),
        "clearance_mm": float(edit.get("clearance_mm", prev["clearance_mm"] if prev else floor_c)),
        "priority": int(edit.get("priority", prev["priority"] if prev else 0)),
        "pour": bool(edit.get("pour", prev["pour"] if prev else False)),
    }
    rules["net_classes"] = others + [item]


def _recheck_duplicate_nets(rules: dict, issues: list[dict]) -> None:
    seen: dict[str, str] = {}
    for cls in rules.get("net_classes") or []:
        for net in cls["nets"]:
            if net in seen and seen[net] != cls["name"]:
                issues.append(_issue(
                    "net_class_conflict",
                    f"网络 {net} 同时属于 {seen[net]} 和 {cls['name']}",
                    net=net))
            seen[net] = cls["name"]


def request_repair(evidence: dict, spec: dict, rules: dict, agent: dict) -> dict:
    system, user = _repair_prompt(evidence, spec, rules)
    raw, provider = _invoke_repair(system, user, agent or {})
    return {"raw": raw, "provider": provider, "prompt_sha256": _sha(system + "\n" + user)}


def _placement_prompt(spec: dict, rules: dict) -> tuple[str, str]:
    system = (
        "你是 VoltProof 的实验性 PCB 布局助手。只输出一个 JSON 对象，"
        "描述每个封装的中心坐标（毫米）和角度。不要输出走线、铜皮坐标或 kicad_pcb。"
        "角度只能取该位号允许的值。板可以放大，不能缩小。json"
    )
    brief = {
        "board_mm": spec.get("size_mm"),
        "components": [
            {
                "ref": c["ref"],
                "part": c.get("part"),
                "footprint": c.get("footprint"),
                "pads": c.get("pads"),
                "seed_xy": [c.get("x"), c.get("y")],
                "courtyard_mm": list(legalizer.courtyard(c)),
            }
            for c in spec.get("components") or []
        ],
        "groups": rules.get("groups") or [],
        "proximity": rules.get("proximity") or [],
        "edge": rules.get("edge") or [],
        "locked": rules.get("locked") or [],
        "rotations": rules.get("rotations") or {},
        "net_classes": rules.get("net_classes") or [],
    }
    user = (
        "下面是已经仿真验证过的电路映射。请给出完整布局 JSON，格式为 "
        '{"board_mm":[宽,高],"placements":[{"ref":"R1","x":20,"y":12,"rot":0}]}。\n'
        + json.dumps(brief, ensure_ascii=False)
    )
    return system, user


def _repair_prompt(evidence: dict, spec: dict, rules: dict) -> tuple[str, str]:
    system = (
        "你是 VoltProof 的实验性 PCB 修复助手。布线或 DRC 失败后，"
        "你只能从菜单里选修改：move、rotate、enlarge_board、net_class、keepout。"
        "不能放宽线宽、净距或过孔，不能输出走线坐标或 kicad_pcb。"
        "只输出 JSON：{\"edits\":[...]} 。json"
    )
    menu = {
        "edits_allowed": [
            {"op": "move", "ref": "<位号>", "x": 0, "y": 0},
            {"op": "rotate", "ref": "<位号>", "rot": 90},
            {"op": "enlarge_board", "w": "<不小于当前宽>", "h": "<不小于当前高>"},
            {"op": "net_class", "name": "Power", "nets": ["vcc"],
             "track_mm": rules.get("track_mm"), "clearance_mm": rules.get("clearance_mm"),
             "priority": 10, "pour": False},
            {"op": "keepout", "x": 0, "y": 0, "w": 4, "h": 4, "net": "<可选>"},
        ],
        "manufacturing_floor": {
            "track_mm": rules.get("track_mm"),
            "clearance_mm": rules.get("clearance_mm"),
            "via_drill_mm": rules.get("via_drill_mm"),
            "via_diameter_mm": rules.get("via_diameter_mm"),
        },
        "evidence": evidence,
    }
    user = "根据证据提出一轮修复。不要放宽 manufacturing_floor。\n" + json.dumps(
        menu, ensure_ascii=False)
    return system, user


def _invoke_propose(system: str, user: str, opts: dict):
    hook = opts.get("propose")
    if hook is not None:
        return hook({"system": system, "user": user}), {"provider": "mock", "model": "mock"}
    return _llm_json(system, user)


def _invoke_repair(system: str, user: str, opts: dict):
    hook = opts.get("repair")
    if hook is not None:
        return hook({"system": system, "user": user, "evidence": True}), {"provider": "mock", "model": "mock"}
    return _llm_json(system, user)


def _llm_json(system: str, user: str):
    cfg = llm.current_config()
    raw = llm.chat_json(system, user, max_tokens=2500, thinking=False)
    return raw, {"provider": cfg.get("base_url") or "", "model": cfg.get("model") or ""}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _public(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "spec"}


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _violation_pos(viol: dict):
    for key in ("pos", "position"):
        if key in viol:
            return viol[key]
    items = viol.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and "pos" in item:
                return item["pos"]
    return None


def _resize_standard_zones(spec: dict, old: tuple[float, float], new) -> None:
    ow, oh = float(old[0]), float(old[1])
    nw, nh = float(new[0]), float(new[1])
    standard = [[4.0, 4.0], [round(ow - 4, 2), 4.0], [round(ow - 4, 2), round(oh - 4, 2)],
                [4.0, round(oh - 4, 2)]]
    updated = [[4, 4], [round(nw - 4, 2), 4], [round(nw - 4, 2), round(nh - 4, 2)],
               [4, round(nh - 4, 2)]]
    for zone in spec.get("zones") or []:
        outline = zone.get("outline_mm") or []
        if _same_outline(outline, standard) or _same_outline(outline, _raw_standard(ow, oh)):
            zone["outline_mm"] = updated


def _raw_standard(w: float, h: float) -> list:
    return [[4, 4], [w - 4, 4], [w - 4, h - 4], [4, h - 4]]


def _same_outline(a, b) -> bool:
    if not isinstance(a, list) or len(a) != len(b):
        return False
    for p, q in zip(a, b):
        if len(p) != 2 or abs(float(p[0]) - float(q[0])) > 0.2 or abs(float(p[1]) - float(q[1])) > 0.2:
            return False
    return True


def cap_repairs(value, enabled: bool) -> int:
    if not enabled:
        return 0
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 1
    return max(0, min(_MAX_REPAIRS, n))
