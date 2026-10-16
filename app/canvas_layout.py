"""网表 → 画布（可编辑电路图）的确定性转换。

会话工坊的核心衔接件：AI 轮产出的是【验证通过的网表】，但画布才是
会话里电路的唯一权威表示。本模块把网表"铺"回画布 schema：

  LLM 只做策略（出网表，pass@1 92.6% 的强项）；
  几何由确定性算法负责——LLM 永远不直接操纵画布坐标。

布局策略（多轮实测教训的沉淀）：
  1. 不复用 dot 的坐标做落位——dot 的二部图把元件/网络各挤成一列，
     缩到 50×35 画布后走廊全无，电源干线横穿元件区把局部围死；
  2. 元件按 dot 的信号流次序（rank 优先、列内按 y）铺到稀疏网格，
     间距 ≥5 格，绕线空间由布局结构性保证；
  3. 无中央汇接列：网络按"引脚→引脚链式"布线（最近邻生成树），
     迷宫算法（Dijkstra + 转角惩罚）在【格点 → 网络】占用图上找
     不踏入其他网络格点的路径；元件本体格点视为障碍（线不穿身）；
  4. 地网络在链上补画接地符号，write 列出的输出网络/电源网络贴标签；
  5. 最后用与前端完全一致的并查集做拓扑终检——不过则整体返回 None，
     调用方降级为只展示证据卡与网表，绝不给用户一张"看着对、实际
     短路"的图。

映射规则与前端 draw.html 的数据契约一一对应：
  comps: [{type,x,y,rot,name,value}]
  wires: [{pts:[[gx,gy],...]}]
  labels:[{x,y,text}]
  analysis: {type,tstop|fstart,fstop}  从 .control 解析，回填分析选择器

已知取舍：半导体/运放模型标准化为画板教学模型（QNPN/MNMOS/DMOD/OPAMP），
不保留 AI 网表的自定义 .model 参数——应用到画布后可一键重仿真验证。
"""

from __future__ import annotations

import heapq
import re

from . import checks as _checks
from .schematic_layout import Elem, _layout_dot, _layout_fallback

# 画布常量（与 draw.html 的 W/H 对应：50×35 格）
_W, _H = 50, 35

# 元件类型映射：网表首字母 → 画板类型。E/G/F/H/B/K 不进画布（画板无此符号）
# V 先映到 VDC，值以 SIN/PULSE/EXP 开头时再升级成 VSIN
_KIND2TYPE = {"R": "R", "C": "C", "L": "L", "D": "D", "V": "VDC",
              "Q": "QN", "M": "MN", "X": "OP", "I": "ISRC"}

# 引脚偏移表，顺序与网表节点序一一对应：
#   QN/MN 卡片是 "Q1 C B E" / "M1 D G S" → [上(=C/D), 左(=B/G), 下(=E/S)]
#   OP 卡片 "X1 n1 n2 n3" → [上(in−), 下(in+), 右(out)]
_PIN_OFFS = {
    "R": [(-2, 0), (2, 0)], "C": [(-2, 0), (2, 0)], "L": [(-2, 0), (2, 0)],
    "D": [(-2, 0), (2, 0)], "VDC": [(-2, 0), (2, 0)], "VSIN": [(-2, 0), (2, 0)],
    "ISRC": [(-2, 0), (2, 0)],
    "QN": [(0, -2), (-2, 0), (0, 2)],
    "MN": [(0, -2), (-2, 0), (0, 2)],
    "OP": [(-2, -1), (-2, 1), (2, 0)],
    "GND": [(0, 0)],
}
_2PIN = {"R", "C", "L", "D", "VDC", "VSIN", "ISRC"}

_SUPPLY_RE = re.compile(r"^(vcc|vee|vdd|vss|v\+|v-|\+\d+v|\-\d+v)", re.I)


def _rot(off: tuple[int, int], r: int) -> tuple[int, int]:
    dx, dy = off
    for _ in range(r & 3):
        dx, dy = -dy, dx
    return dx, dy


def _parse_elems(netlist: str) -> list[dict]:
    """元件行解析（值取全：源的 SIN(...)/AC 1 是多 token）。"""
    out = []
    for _lineno, line in _checks.iter_element_lines(netlist.splitlines()):
        toks = line.split()
        name = toks[0]
        kind = name[0].upper()
        nets = [n for n in _checks.parse_nodes(line) if n]
        if kind in "RCL" and len(toks) >= 4:
            value = toks[3]
        elif kind in "VI":
            value = " ".join(toks[3:])
        else:
            value = ""
        out.append({"name": name, "kind": kind, "nets": nets, "value": value})
    return out


def _parse_analysis(netlist: str) -> dict | None:
    """.control 内（优先）或卡片区 的分析指令 → 画板分析选择器回填值。"""
    in_ctrl, ctrl_lines, deck_lines = False, [], []
    for raw in netlist.splitlines():
        s = raw.strip().lower()
        if s == ".control":
            in_ctrl = True
            continue
        if s == ".endc":
            in_ctrl = False
            continue
        if in_ctrl:
            ctrl_lines.append(s)
        elif s.startswith((".tran", ".ac ", ".op")):
            deck_lines.append(s.lstrip("."))
    lines = [l for l in ctrl_lines if l.startswith(("tran", "ac ", "op"))] or deck_lines
    for l in lines:
        toks = l.split()
        if toks[0] == "tran" and len(toks) >= 3:
            return {"type": "tran", "tstop": toks[2]}
        if toks[0] == "ac" and len(toks) >= 4:
            return {"type": "ac", "fstart": toks[-2], "fstop": toks[-1]}
        if toks[0] == "op":
            return {"type": "op"}
    return None


def _write_nodes(netlist: str) -> set[str]:
    """write 命令列出的输出网络名（v(x) → x）——这些网络在画布上值得命名。"""
    nodes = set()
    for m in re.finditer(r"write\s+\S+((?:\s+v\([^)]+\))+)", netlist, re.I):
        for vm in re.finditer(r"v\(([^)]+)\)", m.group(1)):
            nodes.add(vm.group(1).strip().lower())
    return nodes


# ---------------------------------------------------------------------------
# 拓扑终检（与前端 draw.html 完全一致的并查集语义）
# ---------------------------------------------------------------------------

def _wire_pts(pts: list) -> set[tuple[int, int]]:
    cover = set()
    for i in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        dx, dy = (x2 > x1) - (x2 < x1), (y2 > y1) - (y2 < y1)
        if dx and dy:
            raise ValueError(f"非正交线段: {pts[i]} -> {pts[i+1]}")
        x, y = x1, y1
        cover.add((x, y))
        while (x, y) != (x2, y2):
            x, y = x + dx, y + dy
            cover.add((x, y))
    return cover


def _pins_of(canvas: dict) -> list[tuple[dict, int, tuple[int, int]]]:
    out = []
    for c in canvas["comps"]:
        offs = _PIN_OFFS.get(c["type"])
        if not offs:
            continue
        for i, off in enumerate(offs):
            dx, dy = _rot(off, c.get("rot", 0))
            out.append((c, i, (c["x"] + dx, c["y"] + dy)))
    return out


def _partition(canvas: dict) -> dict:
    """并查集求引脚连通分区：{(comp_name, pin_idx): 分区根}。"""
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for _c, _i, p in _pins_of(canvas):
        parent.setdefault(p, p)
    for w in canvas["wires"]:
        pts = [tuple(p) for p in w["pts"]]
        for p in _wire_pts(pts):
            parent.setdefault(p, p)
        root = pts[0]
        for p in _wire_pts(pts):
            union(root, p)
    return {(_c["name"], _i): find(p) for _c, _i, p in _pins_of(canvas)}


def _topology_ok(canvas: dict, elems: list[dict]) -> bool:
    """画布分区与网表分区在引脚对层面完全一致（0/gnd 视为同一地）。"""
    gnd = {"0", "gnd"}

    def norm_net(n: str) -> str:
        return "0" if n.lower() in gnd else n

    want: dict[tuple, str] = {}
    for e in elems:
        ctype = _KIND2TYPE.get(e["kind"])
        if not ctype:
            continue
        for i, n in enumerate(e["nets"][:len(_PIN_OFFS[ctype])]):
            want[(e["name"], i)] = norm_net(n)
    got = _partition(canvas)
    # GND 等画板自增元件不在 want 里（钉在自家网络上，不引入新连接）
    got = {k: v for k, v in got.items() if k in want}
    keys = list(want)
    if set(got) != set(keys):
        return False
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            if (want[keys[a]] == want[keys[b]]) != (got[keys[a]] == got[keys[b]]):
                return False
    return True


# ---------------------------------------------------------------------------
# 迷宫布线：占用图上找不踏入异网络格点、连到目标集合任一格的折线
# ---------------------------------------------------------------------------

_DIRS = [(1, 0, 0), (-1, 0, 1), (0, 1, 2), (0, -1, 3)]  # dx, dy, dir_id


def _maze_route(a, goals: set, net: str, occupied: dict,
                pin_guard: dict | None = None) -> list | None:
    """Dijkstra（步长 1 + 转角惩罚 2）。goals 是本网络已有结构的格点集合，
    走到任一目标即成功；返回 a → 目标格 的折线点列。

    pin_guard：格点 → 异网络引脚的邻格表。踏入即 +3 代价（软惩罚）——
    干线不再贴着元件行横穿，否则会把别的元件引脚封进"口袋"（实测：
    共射放大的 base 干线沿行 25 横穿，把 Vcc 的 vcc 引脚整个围死）。"""
    if a in goals:
        return None
    best: dict[tuple, int] = {}
    parent: dict[tuple, tuple | None] = {}
    h = []
    for _dx, _dy, d in _DIRS:
        s = (a[0], a[1], d)
        best[s] = 0
        parent[s] = None
        heapq.heappush(h, (0, a[0], a[1], d))

    def passable(x, y):
        if not (0 <= x <= _W and 0 <= y <= _H):
            return False
        o = occupied.get((x, y))
        return o is None or o == net or (x, y) in goals

    goal_state = None
    while h:
        cost, x, y, d = heapq.heappop(h)
        if best.get((x, y, d), -1) != cost:
            continue
        if (x, y) in goals:
            goal_state = (x, y, d)
            break
        for dx, dy, nd in _DIRS:
            nx, ny = x + dx, y + dy
            if not passable(nx, ny):
                continue
            ncost = cost + 1 + (2 if nd != d else 0)
            if pin_guard is not None:
                g = pin_guard.get((nx, ny))
                if g is not None and g != net:
                    ncost += 3
            st = (nx, ny, nd)
            if ncost < best.get(st, 1 << 30):
                best[st] = ncost
                parent[st] = (x, y, d)
                heapq.heappush(h, (ncost, nx, ny, nd))
    if goal_state is None:
        return None
    chain = []
    st = goal_state
    while st is not None:
        chain.append((st[0], st[1]))
        st = parent[st]
    chain.reverse()
    # 合并共线段成折线
    pts = [chain[0]]
    for i in range(1, len(chain) - 1):
        d1 = (chain[i][0] - chain[i - 1][0], chain[i][1] - chain[i - 1][1])
        d2 = (chain[i + 1][0] - chain[i][0], chain[i + 1][1] - chain[i][1])
        if d1 != d2:
            pts.append(chain[i])
    pts.append(chain[-1])
    return pts


# ---------------------------------------------------------------------------
# 主转换
# ---------------------------------------------------------------------------

def netlist_to_canvas(netlist: str) -> dict | None:
    """验证通过的网表 → 画布 schema；拓扑终检不过返回 None。"""
    elems = _parse_elems(netlist)
    if not elems:
        return None

    # ---- 元件排序：dot 只用来定信号流次序（rank 优先、列内按 y）----
    selems = [Elem(name=e["name"], kind=e["kind"], nets=e["nets"], value=e["value"])
              for e in elems]
    order_pos = _layout_dot(selems) or _layout_fallback(selems)
    xs = sorted({round(order_pos[e["name"]][0]) for e in elems if e["name"] in order_pos})
    rank = {x: i for i, x in enumerate(xs)}
    ordered = sorted(elems, key=lambda e: (
        rank.get(round(order_pos[e["name"]][0]), 0) if e["name"] in order_pos else 0,
        order_pos[e["name"]][1] if e["name"] in order_pos else 0))

    # ---- 网格落位：间距尽量大（用满画布，≥5 格），元件多时自动收紧 ----
    n = len(ordered)
    cols = max(1, min(5, n))
    rows = -(-n // cols)
    pitch_x = max(5, (_W - 6) // cols)
    pitch_y = max(5, (_H - 6) // rows)
    ox = max(3, (_W - (cols - 1) * pitch_x) // 2)
    oy = max(3, (_H - (rows - 1) * pitch_y) // 2)
    slot = {}
    for i, e in enumerate(ordered):
        col, row = i % cols, i // cols
        slot[e["name"]] = (ox + col * pitch_x, oy + row * pitch_y)

    def norm(n_: str) -> str:
        return "0" if n_.lower() in ("0", "gnd") else n_

    # 占用图：格点 → 网络（同网络共点无害，异网络即短路）
    occupied: dict[tuple[int, int], str] = {}

    def pin_points(cx, cy, rot, ctype):
        return [(cx + dx, cy + dy) for dx, dy in
                (_rot(off, rot) for off in _PIN_OFFS[ctype])]

    # 引脚→网络 的全局索引（布线用）
    pin_net: dict[tuple[int, int], str] = {}
    comps: list[dict] = []
    placed_pins: dict[str, list[tuple[int, int]]] = {}  # elem_name -> 引脚格点
    for e in elems:
        ctype = _KIND2TYPE.get(e["kind"])
        if ctype is None:
            continue
        cx, cy = slot[e["name"]]
        rot = 0
        value = e["value"]
        if ctype == "VDC":
            if re.match(r"(?i)^(sin|pulse|exp)\s*\(", value):
                ctype = "VSIN"
            elif not value.strip():
                value = "DC 12"
        comps.append({"type": ctype, "x": cx, "y": cy, "rot": rot,
                      "name": e["name"], "value": value})
        pins = pin_points(cx, cy, rot, ctype)
        placed_pins[e["name"]] = pins
        for p, n_ in zip(pins, e["nets"]):
            if n_:
                pin_net[p] = norm(n_)
                occupied.setdefault(p, norm(n_))
        # 本体占位（对所有网络都是障碍）
        occupied.setdefault((cx, cy), f"#{e['name']}")
        if ctype in _2PIN:
            for t in (-1, 1):
                dx, dy = _rot((t, 0), rot)
                occupied.setdefault((cx + dx, cy + dy), f"#{e['name']}")

    # 两端元件的 rot：朝向同网络最近的另一引脚（连线短且不折返穿身）
    for e in elems:
        ctype = _KIND2TYPE.get(e["kind"])
        if ctype not in _2PIN:
            continue
        c = next(cc for cc in comps if cc["name"] == e["name"])
        my_nets = {norm(x) for x in e["nets"] if x}
        old_pins = placed_pins[e["name"]]
        peers = [p for p, nn in pin_net.items()
                 if nn in my_nets and p not in old_pins]
        if not peers:
            continue
        nearest = min(peers, key=lambda q: min(abs(q[0] - p[0]) + abs(q[1] - p[1])
                                               for p in old_pins))
        if abs(nearest[0] - c["x"]) >= abs(nearest[1] - c["y"]):
            rot = 0 if nearest[0] <= c["x"] else 2
        else:
            rot = 3 if nearest[1] >= c["y"] else 1
        if rot == c["rot"]:
            continue
        # 撤旧引脚占位，转 rot 后重设（本体旧格残留无碍，多一道障碍而已）
        for p in old_pins:
            if occupied.get(p) in my_nets:
                del occupied[p]
            pin_net.pop(p, None)
        c["rot"] = rot
        new_pins = pin_points(c["x"], c["y"], rot, ctype)
        placed_pins[e["name"]] = new_pins
        for p, n_ in zip(new_pins, e["nets"]):
            if n_:
                pin_net[p] = norm(n_)
                occupied.setdefault(p, norm(n_))
        for t in (-1, 1):
            dx, dy = _rot((t, 0), rot)
            occupied.setdefault((c["x"] + dx, c["y"] + dy), f"#{e['name']}")

    # ---- 链式布线：每个网络生成树，逐引脚连到已有结构 ----
    nets_pins: dict[str, list[tuple[int, int]]] = {}
    for e in elems:
        ctype = _KIND2TYPE.get(e["kind"])
        if not ctype:
            continue
        for p, n_ in zip(placed_pins[e["name"]], e["nets"]):
            if n_:
                nets_pins.setdefault(norm(n_), []).append(p)

    def is_rail(name: str) -> bool:
        return name == "0" or bool(_SUPPLY_RE.match(name))

    # 引脚邻格软惩罚表：格点 → 引脚所属网络（踏入异网络引脚邻格 +3 代价，
    # 避免干线贴着元件行横穿、把别的引脚封进"口袋"）
    pin_guard: dict[tuple[int, int], str] = {}
    for p, n_ in pin_net.items():
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            q = (p[0] + dx, p[1] + dy)
            pin_guard.setdefault(q, n_)

    base_occupied = dict(occupied)  # 每次重试的干净起点（元件引脚+本体）

    def route_all(net_order):
        """按给定网络顺序链式布线，返回 (wires, occupied)。"""
        occ = dict(base_occupied)
        ws = []
        for net in net_order:
            pins = nets_pins[net]
            if len(pins) < 2:
                continue
            structure = {pins[0]}
            rest = list(pins[1:])
            while rest:
                pin = min(rest, key=lambda q: min(abs(q[0] - s[0]) + abs(q[1] - s[1])
                                                  for s in structure))
                rest.remove(pin)
                path = _maze_route(pin, structure, net, occ, pin_guard)
                if path is None:
                    continue  # 连不上：留给拓扑终检裁决
                for p in _wire_pts(path):
                    occ.setdefault(p, net)
                    structure.add(p)
                ws.append({"pts": [list(p) for p in path]})
        return ws, occ

    # 多顺序重试：不同布线顺序拥堵情况截然不同（干线先布会切出横墙把
    # 后面的网络封死，小网络先布则干线绕远）——固定启发式 + 多种子随机
    # 排列，全败才放弃落布
    import random as _random

    names = list(nets_pins)
    attempts = [
        sorted(names, key=lambda k: -len(nets_pins[k])),                    # 大网络先
        sorted(names, key=lambda k: len(nets_pins[k])),                     # 小网络先
        sorted(names, key=lambda k: (not is_rail(k), len(nets_pins[k]))),   # 干线先
        sorted(names, key=lambda k: (is_rail(k), len(nets_pins[k]))),       # 干线后
    ]
    for seed in range(20):
        perm = list(names)
        _random.Random(seed).shuffle(perm)
        attempts.append(perm)
    write_nodes = _write_nodes(netlist)
    for net_order in attempts:
        wires, occupied = route_all(net_order)
        canvas_try = {"comps": comps, "wires": wires, "labels": [],
                      "analysis": _parse_analysis(netlist)}
        if not _topology_ok(canvas_try, elems):
            continue
        # ---- 接地符号：贴在地网络结构旁的空格（并补一格连线接入网络——
        # 前端按"共格点即连通"算网络，GND 引脚必须落在导线格点上才真正接地）----
        gnd_cells = nets_pins.get("0", [])
        gnd_added = False
        for s in sorted(gnd_cells, key=lambda q: (q[1], q[0])):
            for f in ((s[0] + 1, s[1]), (s[0] - 1, s[1]), (s[0], s[1] + 1)):
                if (f not in occupied and 1 <= f[0] <= _W - 1 and 2 <= f[1] <= _H - 3
                        and (f[0], f[1] + 1) not in occupied
                        and (f[0], f[1] + 2) not in occupied):
                    comps.append({"type": "GND", "x": f[0], "y": f[1], "rot": 0,
                                  "name": "", "value": ""})
                    wires.append({"pts": [[f[0], f[1]], [s[0], s[1]]]})
                    gnd_added = True
                    break
            if gnd_added:
                break
        # ---- 网络标签：write 列出的输出网络 + 电源网络 ----
        for net, cells in nets_pins.items():
            if net == "0" or not (net.lower() in write_nodes or _SUPPLY_RE.match(net)):
                continue
            s = min(cells, key=lambda q: (q[1], q[0]))
            canvas_try["labels"].append({"x": s[0], "y": s[1], "text": net})
        return canvas_try
    return None
