"""确定性布局合法化：重叠、出板、间距、靠边、锁定、近距与禁布。

间距的基线是信号流布局已经实测过的庭院安全节距（横向 16mm、纵向 13mm，
见 placement.X_PITCH / Y_PITCH）。具体封装能收得更紧时用下面的庭院盒子，
两盒沿某一轴分开就不算重叠。

合法化只改坐标和角度，不放宽线宽、净距或过孔。修不动的硬约束返回
结构化错误，调用方不得当成已经布好的板。
"""
from __future__ import annotations

import copy
import math

from . import placement

# (宽, 高) 毫米，旋转 90/270 时对调。默认盒子等于实测安全节距，
# 所以信号流网格（列距 16、行距 13）刚好贴边、不会被推开。
_DEFAULT_BOX = (placement.X_PITCH, placement.Y_PITCH)
_EDGE_TARGET = placement.X_MARGIN  # 端子列中心，信号流用的就是这个值


def courtyard(comp: dict) -> tuple[float, float]:
    """封装庭院（宽, 高），毫米。未知封装退回实测安全节距。"""
    fp = str(comp.get("footprint") or "")
    part = str(comp.get("part") or "")
    if "DIP-8" in fp or part.startswith("LM358_DIP"):
        box = (11.0, 18.0)
    elif "SOIC-8" in fp or part.startswith("LM358_SOIC"):
        box = (8.0, 11.0)
    elif "PinHeader" in fp or part.startswith("CONN_"):
        box = (5.5, 8.0)
    elif "MountingHole" in fp or part == "HOLE_M3":
        box = (6.0, 6.0)
    elif "R_Axial" in fp or part.startswith("R_THT"):
        box = (12.0, 5.0)
    elif "C_Disc" in fp or "C_Radial" in fp or part.startswith("C_THT"):
        box = (8.0, 8.0)
    elif "0805" in fp or part.endswith("_0805") or "_SMD_" in part:
        box = (5.0, 4.0)
    elif "LED" in fp or "D_DO-35" in fp or part.startswith("D_") or part.startswith("LED"):
        box = (10.0, 5.0)
    elif "TO-92" in fp or "SOT-23" in fp or part.startswith("Q_"):
        box = (8.0, 8.0)
    else:
        box = _DEFAULT_BOX
    rot = int(round(float(comp.get("rot") or 0))) % 360
    if rot in (90, 270):
        return box[1], box[0]
    return box


def _issue(code: str, reason: str, **extra) -> dict:
    item = {"code": code, "reason": reason}
    item.update(extra)
    return item


def _locked_map(rules: dict) -> dict[str, dict]:
    out = {}
    for item in rules.get("locked") or []:
        if isinstance(item, dict) and item.get("ref"):
            out[str(item["ref"])] = item
    return out


def _overlap_amount(a: dict, b: dict) -> tuple[float, float] | None:
    dx = abs(float(a["x"]) - float(b["x"]))
    dy = abs(float(a["y"]) - float(b["y"]))
    wa, ha = courtyard(a)
    wb, hb = courtyard(b)
    ox = (wa + wb) / 2 - dx
    oy = (ha + hb) / 2 - dy
    if ox > 0.05 and oy > 0.05:
        return ox, oy
    return None


def _inside(comp: dict, width: float, height: float) -> bool:
    w, h = courtyard(comp)
    x, y = float(comp["x"]), float(comp["y"])
    return (w / 2 - 0.05) <= x <= width - w / 2 + 0.05 and (h / 2 - 0.05) <= y <= height - h / 2 + 0.05


def _edge_distance(comp: dict, width: float, height: float) -> float:
    x, y = float(comp["x"]), float(comp["y"])
    return min(x, width - x, y, height - y)


def legalize(spec: dict, rules: dict) -> dict:
    """返回 {ok, spec, errors, repairs}。不修改调用方传入的 spec。

    成功时 rules["keepouts"] 会补上能够落地的推断禁布区（与 spec 一致）。
    失败时 rules 保持调用前的禁布列表。
    """
    spec = copy.deepcopy(spec)
    rules_keepouts_before = copy.deepcopy(rules.get("keepouts") or [])
    comps: list[dict] = list(spec.get("components") or [])
    by_ref = {c["ref"]: c for c in comps}
    locked = _locked_map(rules)
    allowed = rules.get("rotations") or {"*": [0, 90, 180, 270]}
    repairs: list[dict] = []
    errors: list[dict] = []

    for comp in comps:
        opts = allowed.get(comp["ref"], allowed.get("*", [0, 90, 180, 270]))
        opts = [int(a) % 360 for a in opts]
        rot = int(round(float(comp.get("rot") or 0))) % 360
        if rot not in opts:
            if comp["ref"] in locked:
                errors.append(_issue(
                    "rotation",
                    f"{comp['ref']} 被锁定，不能把角度 {rot} 收到允许集合 {opts}",
                    ref=comp["ref"]))
            else:
                nearest = min(opts, key=lambda a: min(abs(a - rot), 360 - abs(a - rot)))
                repairs.append({"ref": comp["ref"], "why": "rotation",
                                "from": rot, "to": nearest})
                rot = nearest
        comp["rot"] = rot

    for ref, lock in locked.items():
        comp = by_ref.get(ref)
        if comp is None:
            errors.append(_issue("locked_missing", f"锁定位号 {ref} 不在板上", ref=ref))
            continue
        lx, ly = float(lock["x"]), float(lock["y"])
        if abs(float(comp["x"]) - lx) > 0.05 or abs(float(comp["y"]) - ly) > 0.05:
            errors.append(_issue(
                "locked_moved",
                f"{ref} 锁定在 ({lx}, {ly})，当前在 ({comp['x']}, {comp['y']})",
                ref=ref))
        comp["x"], comp["y"] = lx, ly
        if "rot" in lock:
            comp["rot"] = int(lock["rot"]) % 360

    if errors:
        return {"ok": False, "spec": None, "errors": errors, "repairs": repairs}

    def _remember(comp: dict, why: str, x: float, y: float) -> None:
        if abs(float(comp["x"]) - x) < 0.05 and abs(float(comp["y"]) - y) < 0.05:
            return
        repairs.append({"ref": comp["ref"], "why": why,
                        "from": [round(float(comp["x"]), 2), round(float(comp["y"]), 2)],
                        "to": [round(x, 2), round(y, 2)]})
        comp["x"], comp["y"] = round(x, 2), round(y, 2)

    for _round in range(48):
        width, height = (float(spec["size_mm"][0]), float(spec["size_mm"][1]))
        changed = False
        changed |= _clamp_all(comps, locked, width, height, _remember)
        changed |= _separate(comps, locked, _remember)
        changed |= _pull_proximity(comps, by_ref, locked, rules, spec, _remember)
        changed |= _pull_groups(comps, by_ref, locked, rules, _remember)
        width, height = (float(spec["size_mm"][0]), float(spec["size_mm"][1]))
        changed |= _pull_isolation(comps, by_ref, locked, rules, width, height, _remember)
        changed |= _project_edges(comps, by_ref, locked, rules, width, height, _remember)
        changed |= _push_keepouts(comps, locked, spec.get("keepouts") or [], width, height, _remember)
        if not changed:
            break
    else:
        errors.append(_issue("unresolved", "推开 48 轮后布局仍在变化，拒绝这次提案"))

    errors.extend(_final_geometry(comps, by_ref, locked, rules, width, height))
    if errors:
        rules["keepouts"] = rules_keepouts_before
        return {"ok": False, "spec": None, "errors": errors, "repairs": repairs}

    slot_errors = _materialize_keepouts(spec, rules, by_ref, width, height)
    if slot_errors:
        rules["keepouts"] = rules_keepouts_before
        return {"ok": False, "spec": None, "errors": slot_errors, "repairs": repairs}

    # 新禁布区落定后再确认没有压到封装。压到就失败，不删禁布区蒙混。
    hit = _final_keepouts(comps, spec.get("keepouts") or [])
    if hit:
        rules["keepouts"] = rules_keepouts_before
        return {"ok": False, "spec": None, "errors": hit, "repairs": repairs}

    spec["components"] = comps
    return {"ok": True, "spec": spec, "errors": [], "repairs": repairs}


def _clamp_all(comps, locked, width, height, remember) -> bool:
    changed = False
    for comp in comps:
        if comp["ref"] in locked:
            continue
        w, h = courtyard(comp)
        nx = min(max(float(comp["x"]), w / 2), width - w / 2)
        ny = min(max(float(comp["y"]), h / 2), height - h / 2)
        if abs(nx - float(comp["x"])) > 0.05 or abs(ny - float(comp["y"])) > 0.05:
            remember(comp, "in_board", nx, ny)
            changed = True
    return changed


def _movable(a, b, locked) -> dict | None:
    """重叠时移动位号较后、且未锁定的那一个。两个都锁则返回 None。"""
    order = sorted((a, b), key=lambda c: c["ref"])
    for comp in reversed(order):
        if comp["ref"] not in locked:
            return comp
    return None


def _separate(comps, locked, remember) -> bool:
    changed = False
    for i, a in enumerate(comps):
        for b in comps[i + 1:]:
            hit = _overlap_amount(a, b)
            if hit is None:
                continue
            mover = _movable(a, b, locked)
            if mover is None:
                continue
            other = b if mover is a else a
            ox, oy = hit
            if ox <= oy:
                sign = 1 if float(mover["x"]) >= float(other["x"]) else -1
                if abs(float(mover["x"]) - float(other["x"])) < 0.05:
                    sign = 1 if mover["ref"] > other["ref"] else -1
                remember(mover, "overlap", float(mover["x"]) + sign * (ox + 0.2), float(mover["y"]))
            else:
                sign = 1 if float(mover["y"]) >= float(other["y"]) else -1
                if abs(float(mover["y"]) - float(other["y"])) < 0.05:
                    sign = 1 if mover["ref"] > other["ref"] else -1
                remember(mover, "overlap", float(mover["x"]), float(mover["y"]) + sign * (oy + 0.2))
            changed = True
    return changed


def _pull_proximity(comps, by_ref, locked, rules, spec, remember) -> bool:
    """把去耦等近距器件拉到父器件旁。当前板放不下时只放大板框，不放宽间距。"""
    changed = False
    for item in rules.get("proximity") or []:
        child = by_ref.get(item["ref"])
        parent = by_ref.get(item["parent"])
        if child is None or parent is None or child["ref"] in locked:
            continue
        dx = float(child["x"]) - float(parent["x"])
        dy = float(child["y"]) - float(parent["y"])
        dist = math.hypot(dx, dy)
        limit = float(item["max_mm"])
        if dist <= limit + 0.05:
            continue
        wa, ha = courtyard(child)
        wb, hb = courtyard(parent)
        min_center = min((wa + wb) / 2, (ha + hb) / 2) + 0.6
        target = max(min_center, min(limit * 0.85, max(min_center, limit * 0.85)))
        base = math.atan2(dy, dx) if dist > 0.1 else 0.0
        candidates = []
        for ang in [base] + [math.radians(a) for a in (0, 90, 180, 270, 45, 135, 225, 315)]:
            candidates.append((float(parent["x"]) + math.cos(ang) * target,
                               float(parent["y"]) + math.sin(ang) * target))
        for slot in range(4):
            for sign in (1, -1):
                candidates.append((float(parent["x"]) + sign * target,
                                   float(parent["y"]) + sign * slot * (ha + 1.5)))
                candidates.append((float(parent["x"]) + sign * slot * (wa + 1.5),
                                   float(parent["y"]) + sign * target))
        placed = False
        for nx, ny in candidates:
            if math.hypot(nx - float(parent["x"]), ny - float(parent["y"])) > limit + 0.05:
                continue
            _make_room(spec, child, nx, ny)
            width, height = float(spec["size_mm"][0]), float(spec["size_mm"][1])
            trial = dict(child)
            trial["x"], trial["y"] = nx, ny
            if not _inside(trial, width, height):
                continue
            if any(other is not child and _overlap_amount(trial, other) for other in comps):
                continue
            remember(child, "proximity", nx, ny)
            changed = True
            placed = True
            break
        if not placed:
            continue
    return changed


def _make_room(spec: dict, comp: dict, x: float, y: float) -> bool:
    """把板框放大到能放下 (x, y) 上的庭院。只放大，不缩小。"""
    w, h = courtyard(comp)
    need_w = max(float(spec["size_mm"][0]), x + w / 2 + 1.5, w + 3)
    need_h = max(float(spec["size_mm"][1]), y + h / 2 + 1.5, h + 3)
    if x - w / 2 < 1 or y - h / 2 < 1:
        return False
    if need_w > 500 or need_h > 500:
        return False
    old = (float(spec["size_mm"][0]), float(spec["size_mm"][1]))
    if need_w <= old[0] + 0.05 and need_h <= old[1] + 0.05:
        return False
    spec["size_mm"] = [round(need_w, 2), round(need_h, 2)]
    _resize_zones(spec, old)
    return True


def _resize_zones(spec: dict, old: tuple[float, float]) -> None:
    ow, oh = old
    nw, nh = float(spec["size_mm"][0]), float(spec["size_mm"][1])
    standard = [[4.0, 4.0], [ow - 4.0, 4.0], [ow - 4.0, oh - 4.0], [4.0, oh - 4.0]]
    updated = [[4, 4], [round(nw - 4, 2), 4], [round(nw - 4, 2), round(nh - 4, 2)],
               [4, round(nh - 4, 2)]]
    for zone in spec.get("zones") or []:
        outline = zone.get("outline_mm") or []
        if len(outline) == 4 and all(
                abs(float(p[0]) - float(q[0])) < 0.3 and abs(float(p[1]) - float(q[1])) < 0.3
                for p, q in zip(outline, standard)):
            zone["outline_mm"] = updated


def _pull_groups(comps, by_ref, locked, rules, remember) -> bool:
    changed = False
    for group in rules.get("groups") or []:
        members = [by_ref[r] for r in group.get("refs") or [] if r in by_ref]
        if len(members) < 2:
            continue
        xs = [float(c["x"]) for c in members]
        ys = [float(c["y"]) for c in members]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if span <= float(group["max_span_mm"]) + 0.05:
            continue
        cx = sum(xs) / len(xs)
        cy = sum(ys) / len(ys)
        for comp in members:
            if comp["ref"] in locked:
                continue
            nx = float(comp["x"]) + (cx - float(comp["x"])) * 0.45
            ny = float(comp["y"]) + (cy - float(comp["y"])) * 0.45
            remember(comp, "group", nx, ny)
            changed = True
    return changed


def _isolated(a: dict, b: dict, gap: float) -> bool:
    dx = abs(float(a["x"]) - float(b["x"]))
    dy = abs(float(a["y"]) - float(b["y"]))
    wa, ha = courtyard(a)
    wb, hb = courtyard(b)
    return (dx + 0.05 >= (wa + wb) / 2 + gap) or (dy + 0.05 >= (ha + hb) / 2 + gap)


def _pull_isolation(comps, by_ref, locked, rules, width, height, remember) -> bool:
    changed = False
    for item in rules.get("isolation") or []:
        gap = float(item.get("min_mm") or 0)
        left = [by_ref[r] for r in item.get("refs") or [] if r in by_ref]
        right = [by_ref[r] for r in item.get("away_from") or [] if r in by_ref]
        for a in left:
            for b in right:
                if a["ref"] == b["ref"] or _isolated(a, b, gap):
                    continue
                mover = a if a["ref"] not in locked else b
                other = b if mover is a else a
                if mover["ref"] in locked:
                    continue
                dx = float(mover["x"]) - float(other["x"])
                dy = float(mover["y"]) - float(other["y"])
                if abs(dx) >= abs(dy):
                    sign = 1 if dx >= 0 else -1
                    wa, _ha = courtyard(mover)
                    wb, _hb = courtyard(other)
                    need = (wa + wb) / 2 + gap + 0.3
                    nx = float(other["x"]) + sign * need
                    ny = float(mover["y"])
                else:
                    sign = 1 if dy >= 0 else -1
                    _wa, ha = courtyard(mover)
                    _wb, hb = courtyard(other)
                    need = (ha + hb) / 2 + gap + 0.3
                    nx = float(mover["x"])
                    ny = float(other["y"]) + sign * need
                trial = dict(mover)
                trial["x"], trial["y"] = nx, ny
                if _inside(trial, width, height):
                    remember(mover, "isolation", nx, ny)
                    changed = True
    return changed


def _project_edges(comps, by_ref, locked, rules, width, height, remember) -> bool:
    changed = False
    for item in rules.get("edge") or []:
        comp = by_ref.get(item["ref"])
        if comp is None or comp["ref"] in locked:
            continue
        band = float(item.get("band_mm") or 12)
        if _edge_distance(comp, width, height) <= band + 0.05:
            continue
        w, h = courtyard(comp)
        distances = {
            "left": float(comp["x"]),
            "right": width - float(comp["x"]),
            "bottom": float(comp["y"]),
            "top": height - float(comp["y"]),
        }
        edge = min(distances, key=lambda name: (distances[name], name))
        inset_x = min(_EDGE_TARGET, max(w / 2 + 0.8, 0))
        inset_y = min(_EDGE_TARGET, max(h / 2 + 0.8, 0))
        nx, ny = float(comp["x"]), float(comp["y"])
        if edge == "left":
            nx = inset_x
        elif edge == "right":
            nx = width - inset_x
        elif edge == "bottom":
            ny = inset_y
        else:
            ny = height - inset_y
        nx = min(max(nx, w / 2), width - w / 2)
        ny = min(max(ny, h / 2), height - h / 2)
        remember(comp, "edge", nx, ny)
        changed = True
    return changed


def _rect_hits_box(comp: dict, ko: dict) -> bool:
    w, h = courtyard(comp)
    cx, cy = float(comp["x"]), float(comp["y"])
    x0, y0 = cx - w / 2, cy - h / 2
    x1, y1 = cx + w / 2, cy + h / 2
    kx, ky = float(ko["x"]), float(ko["y"])
    kw, kh = float(ko["w"]), float(ko["h"])
    return x0 < kx + kw - 0.05 and x1 > kx + 0.05 and y0 < ky + kh - 0.05 and y1 > ky + 0.05


def _push_keepouts(comps, locked, keepouts, width, height, remember) -> bool:
    changed = False
    for ko in keepouts:
        for comp in comps:
            if comp["ref"] in locked or not _rect_hits_box(comp, ko):
                continue
            w, h = courtyard(comp)
            cx, cy = float(comp["x"]), float(comp["y"])
            options = [
                (float(ko["x"]) - w / 2 - 0.4, cy),
                (float(ko["x"]) + float(ko["w"]) + w / 2 + 0.4, cy),
                (cx, float(ko["y"]) - h / 2 - 0.4),
                (cx, float(ko["y"]) + float(ko["h"]) + h / 2 + 0.4),
            ]
            for nx, ny in options:
                trial = dict(comp)
                trial["x"], trial["y"] = nx, ny
                if _inside(trial, width, height) and not _rect_hits_box(trial, ko):
                    remember(comp, "keepout", nx, ny)
                    changed = True
                    break
    return changed


def _final_geometry(comps, by_ref, locked, rules, width, height) -> list[dict]:
    errors = []
    for comp in comps:
        if not _inside(comp, width, height):
            errors.append(_issue(
                "out_of_board",
                f"{comp['ref']} 的庭院超出板框 {width}×{height}mm",
                ref=comp["ref"]))
    for i, a in enumerate(comps):
        for b in comps[i + 1:]:
            if _overlap_amount(a, b):
                errors.append(_issue(
                    "overlap",
                    f"{a['ref']} 与 {b['ref']} 庭院重叠",
                    refs=[a["ref"], b["ref"]]))
    for item in rules.get("proximity") or []:
        child = by_ref.get(item["ref"])
        parent = by_ref.get(item["parent"])
        if child is None or parent is None:
            errors.append(_issue(
                "proximity_missing",
                f"近距约束引用了不在板上的位号 {item.get('ref')}/{item.get('parent')}"))
            continue
        dist = math.hypot(float(child["x"]) - float(parent["x"]),
                          float(child["y"]) - float(parent["y"]))
        if dist > float(item["max_mm"]) + 0.05:
            errors.append(_issue(
                "proximity",
                f"{child['ref']} 距离 {parent['ref']} {dist:.1f}mm，超过 {item['max_mm']}mm",
                ref=child["ref"]))
    for group in rules.get("groups") or []:
        members = [by_ref[r] for r in group.get("refs") or [] if r in by_ref]
        missing = [r for r in group.get("refs") or [] if r not in by_ref]
        if missing:
            errors.append(_issue("group_missing", f"功能组 {group.get('id')} 缺少位号 {missing}"))
            continue
        if len(members) < 2:
            continue
        xs = [float(c["x"]) for c in members]
        ys = [float(c["y"]) for c in members]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if span > float(group["max_span_mm"]) + 0.05:
            errors.append(_issue(
                "group",
                f"功能组 {group.get('id')} 跨度 {span:.1f}mm，超过 {group['max_span_mm']}mm"))
    for item in rules.get("edge") or []:
        comp = by_ref.get(item["ref"])
        if comp is None:
            errors.append(_issue("edge_missing", f"靠边约束的位号 {item.get('ref')} 不在板上"))
            continue
        band = float(item.get("band_mm") or 12)
        dist = _edge_distance(comp, width, height)
        if dist > band + 0.05:
            errors.append(_issue(
                "edge",
                f"{comp['ref']} 离板边 {dist:.1f}mm，要求 ≤ {band}mm",
                ref=comp["ref"]))
    for item in rules.get("isolation") or []:
        gap = float(item.get("min_mm") or 0)
        for ra in item.get("refs") or []:
            for rb in item.get("away_from") or []:
                if ra == rb or ra not in by_ref or rb not in by_ref:
                    continue
                if not _isolated(by_ref[ra], by_ref[rb], gap):
                    errors.append(_issue(
                        "isolation",
                        f"{ra} 与 {rb} 的间隔小于 {gap}mm（敏感网络）",
                        refs=[ra, rb]))
    return errors


def _final_keepouts(comps, keepouts) -> list[dict]:
    errors = []
    for ko in keepouts:
        for comp in comps:
            if _rect_hits_box(comp, ko):
                errors.append(_issue(
                    "keepout_conflict",
                    f"禁布区压住 {comp['ref']}，不能删掉这块禁布区",
                    ref=comp["ref"]))
    return errors


def _materialize_keepouts(spec, rules, by_ref, width, height) -> list[dict]:
    """把可推断的禁布意图落成矩形。落不下去就报错，不悄悄丢掉。"""
    errors = []
    existing = [dict(k) for k in (spec.get("keepouts") or [])]
    generated = []
    for intent in rules.get("keepout_intents") or []:
        anchors = [by_ref[r] for r in intent.get("anchor_refs") or [] if r in by_ref]
        if not anchors:
            errors.append(_issue(
                "keepout_unplaced",
                f"网络 {intent.get('net')} 的禁布区没有可锚定的封装"))
            continue
        ax = sum(float(c["x"]) for c in anchors) / len(anchors)
        ay = sum(float(c["y"]) for c in anchors) / len(anchors)
        size = float(intent.get("size_mm") or 4)
        rect = _nearest_free_rect(ax, ay, size, width, height, by_ref.values(), existing + generated)
        if rect is None:
            errors.append(_issue(
                "keepout_unplaced",
                f"板上没有不压封装的位置可放网络 {intent.get('net')} 的禁布区",
                net=intent.get("net")))
            continue
        rect["net"] = intent.get("net")
        rect["reason"] = intent.get("reason") or intent.get("kind") or ""
        rect["generated"] = True
        generated.append(rect)
    if errors:
        return errors
    merged = existing + generated
    spec["keepouts"] = merged
    rules["keepouts"] = merged
    return []


def _nearest_free_rect(ax, ay, size, width, height, comps, keepouts) -> dict | None:
    for dist in (6, 8, 10, 12, 16, 20, 24, 30, 40):
        for deg in (0, 45, 90, 135, 180, 225, 270, 315):
            ang = math.radians(deg)
            cx = ax + math.cos(ang) * dist
            cy = ay + math.sin(ang) * dist
            rect = {"x": round(cx - size / 2, 2), "y": round(cy - size / 2, 2),
                    "w": size, "h": size}
            if rect["x"] < 1 or rect["y"] < 1:
                continue
            if rect["x"] + size > width - 1 or rect["y"] + size > height - 1:
                continue
            probe = {"x": cx, "y": cy, "_box": (size, size)}
            if any(_probe_hits(probe, comp) for comp in comps):
                continue
            if any(_keepout_hits(rect, ko) for ko in keepouts):
                continue
            return rect
    return None


def _probe_hits(probe: dict, comp: dict) -> bool:
    pw, ph = probe["_box"]
    cw, ch = courtyard(comp)
    dx = abs(probe["x"] - float(comp["x"]))
    dy = abs(probe["y"] - float(comp["y"]))
    return dx < (pw + cw) / 2 - 0.05 and dy < (ph + ch) / 2 - 0.05


def _keepout_hits(a: dict, b: dict) -> bool:
    return (a["x"] < b["x"] + b["w"] and a["x"] + a["w"] > b["x"]
            and a["y"] < b["y"] + b["h"] and a["y"] + a["h"] > b["y"])
