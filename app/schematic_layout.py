"""确定性原理图布局：网表 → 图结构 → graphviz dot 布局 → schemdraw 渲染。

2026-09 升级背景：原路线让 LLM 生成 schemdraw 代码、布局坐标靠语言模型
想象，复杂电路（振荡+分频+多级滤波）元件重叠连线混乱。本模块把画图从
概率问题变成确定性问题：LLM 彻底退出画图环节。

架构（调研结论：LLM 做策略，经典算法做几何）：
  1. 网表 → 图结构：复用 checks 的元件行/节点解析；网络与元件构成二部图
  2. 布局：graphviz dot（EPL-1.0，子进程调用与 ngspice 同哲学）——
     rankdir=LR 匹配信号流，地网络 rank=sink 沉底、电源网络 rank=source
     置顶，二部图中网络节点坐标即电气汇接点
  3. 渲染：两端元件 .at(netA).to(netB) 精确落位；多端元件（Q/M/X）画
     IC 方框、引脚到网络汇接点连线——拓扑 100% 正确
  dot 不可用时回退纯 Python 网格布局。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import checks as _checks

_DOT = os.environ.get("VOLTPROOF_DOT",
                      r"C:\Program Files\Graphviz\bin\dot.exe")

# 元件引脚数（用于布局/渲染分类；不在表内的按 2 端处理）
_PINS = {"Q": 3, "M": 4, "E": 4, "G": 4, "X": 99, "K": 99, "F": 2, "H": 2, "B": 2}

_SUPPLY_RE = re.compile(r"^(vcc|vee|vdd|vss|v\+|v-|\+\d+v|\-\d+v)", re.I)


def _is_gnd(n: str) -> bool:
    return n.lower() in ("0", "gnd")


def _is_supply(n: str) -> bool:
    return bool(_SUPPLY_RE.match(n)) and not _is_gnd(n)


@dataclass
class Elem:
    name: str
    kind: str
    nets: list[str]
    value: str = ""


def parse_to_graph(netlist: str) -> list[Elem]:
    """网表 → 元件列表（名称/类型/引脚网络）。"""
    elems: list[Elem] = []
    for _lineno, line in _checks.iter_element_lines(netlist.splitlines()):
        toks = line.split()
        name = toks[0]
        nets = _checks.parse_nodes(line)
        value = ""
        kind = name[0].upper()
        if kind in "RCL" and len(toks) >= 4:
            value = toks[3]
        elif kind in "VD" and len(toks) >= 4:
            value = toks[3]
        elems.append(Elem(name=name, kind=kind, nets=[n for n in nets if n], value=value))
    return elems


# ---------------------------------------------------------------------------
# 布局：graphviz dot（首选）/ 纯 Python 网格（回退）
# ---------------------------------------------------------------------------

def _layout_dot(elems: list[Elem]) -> dict[str, tuple[float, float]] | None:
    """二部图（元件→网络）交给 dot 布局，返回 {节点名: (x,y)}。"""
    if not (Path(_DOT).exists() or shutil.which(_DOT)):
        return None
    nets: dict[str, None] = {}
    for e in elems:
        for n in e.nets:
            nets.setdefault(n, None)

    lines = ["digraph g {", '  rankdir=LR;', "  nodesep=0.75;", "  ranksep=1.8;",
             '  node [shape=box height=0.5 width=0.9];']
    gnd = [n for n in nets if _is_gnd(n)]
    sup = [n for n in nets if _is_supply(n)]
    for n in nets:
        node = f'  "net_{n}" [shape=point width=0.02];'
        lines.append(node)
    for n in gnd:
        lines.append(f'  {{rank=sink; "net_{n}";}}')
    for n in sup:
        lines.append(f'  {{rank=source; "net_{n}";}}')
    for e in elems:
        for n in e.nets:
            lines.append(f'  "{e.name}" -> "net_{n}";')
    lines.append("}")
    src = "\n".join(lines)

    with tempfile.TemporaryDirectory(prefix="cp_dot_") as td:
        gv = Path(td) / "g.gv"
        gv.write_text(src, encoding="utf-8")
        try:
            proc = subprocess.run([_DOT, "-T", "json", str(gv)],
                                  capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        pos: dict[str, tuple[float, float]] = {}
        for obj in data.get("objects", []):
            name = obj.get("name", "")
            p = obj.get("pos")
            if name and isinstance(p, str) and "," in p:
                x, y = p.split(",")[:2]
                name = name.removeprefix("net_")
                pos[name] = (float(x), float(y))
        return pos or None


def _layout_fallback(elems: list[Elem]) -> dict[str, tuple[float, float]]:
    """无 graphviz 时的网格布局：元件按序铺网格，网络取其引脚平均坐标。"""
    pos: dict[str, tuple[float, float]] = {}
    cols = max(1, int(len(elems) ** 0.5))
    for i, e in enumerate(elems):
        pos[e.name] = ((i % cols) * 5.0, -(i // cols) * 4.0)
    nets: dict[str, list[tuple[float, float]]] = {}
    for e in elems:
        for n in e.nets:
            nets.setdefault(n, []).append(pos[e.name])
    for n, pts in nets.items():
        pos[n] = (sum(p[0] for p in pts) / len(pts) + 0.8,
                  sum(p[1] for p in pts) / len(pts) - 0.8)
    return pos


# ---------------------------------------------------------------------------
# 渲染（schemdraw，按坐标落位；拓扑正确优先）
# ---------------------------------------------------------------------------

def render_netlist_schematic(netlist: str, out_png: str | Path,
                             title: str = "") -> Path | None:
    """确定性渲染网表为原理图 PNG。失败返回 None（调用方回退 LLM 老路）。"""
    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei",
                                              "Noto Sans CJK SC", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    import schemdraw
    import schemdraw.elements as elm

    elems = parse_to_graph(netlist)
    if not elems:
        return None
    pos = _layout_dot(elems) or _layout_fallback(elems)
    if not pos:
        return None

    # dot 坐标（点）→ schemdraw 单位：y 轴翻转（dot 的 y 向上，schemdraw 向下）
    K = 2.4 / 72.0

    def xy(name: str) -> tuple[float, float]:
        x, y = pos[name]
        return (x * K, -y * K)

    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    d = schemdraw.Drawing(show=False)
    d.config(unit=2.4, fontsize=11)

    # 阶段 1：两端元件直接 .at(netA).to(netB)；多端元件画 IC 方框
    multi: list[Elem] = []
    two_end_idx = 0
    for e in elems:
        if e.kind in _PINS and _PINS[e.kind] > 2:
            multi.append(e)
            continue
        if len(e.nets) < 2 or any(n not in pos for n in e.nets[:2]):
            continue
        a, b = xy(e.nets[0]), xy(e.nets[1])
        sym = {
            "R": elm.Resistor, "C": elm.Capacitor, "L": elm.Inductor2,
            "V": elm.Source, "I": elm.SourceI, "D": elm.Diode,
            "F": elm.SourceControlled, "H": elm.SourceControlled,
            "B": elm.Source,
        }.get(e.kind, elm.Resistor)
        label = e.name if not e.value else f"{e.name}\n{e.value}"
        # 标注上下交替错开，避免相邻元件文字堆叠
        loc = "top" if two_end_idx % 2 == 0 else "bottom"
        two_end_idx += 1
        d += sym().at(a).to(b).label(label, fontsize=9, loc=loc)

    for e in multi:
        c = xy(e.name)
        pins = []
        for j, n in enumerate(e.nets):
            side = "left" if j % 2 == 0 else "right"
            pins.append(elm.IcPin(name=f"p{j}", side=side, anchorname=f"p{j}"))
        ic = elm.Ic(pins=pins, edgepadW=0.4).at(c).label(
            f"{e.name}\n{e.value or _kind_name(e.kind)}", fontsize=9)
        d += ic
        e_pos = {k: v for k, v in ic.absanchors.items()}  # 引脚实际坐标
        for j, n in enumerate(e.nets):
            if n not in pos or f"p{j}" not in e_pos:
                continue
            d += elm.Line().at(e_pos[f"p{j}"]).to(xy(n)).color("#666")

    # 阶段 2：网络汇接点 + 引脚连线（两端元件的引脚就在 net 点上，无需重复）
    for e in multi:
        pass  # 连线已在上面画
    drawn_nets = set()
    for e in elems:
        if e in multi:
            continue
        for n in e.nets[:2]:
            drawn_nets.add(n)
    for n in sorted(set(pos) - {e.name for e in elems}):
        if n not in pos:
            continue
        p = xy(n)
        if _is_gnd(n):
            d += elm.Ground().at(p)
        else:
            d += elm.Dot().at(p)
            if _is_supply(n):
                d += elm.Label().at(p).label(n, fontsize=8, loc="top")

    if title:
        d += elm.Label().at((0, 0)).label(title, fontsize=10, loc="bottom").color("#999")
    try:
        d.save(str(out_png), dpi=150, transparent=False)
    except Exception:
        return None
    if not out_png.exists() or out_png.stat().st_size == 0:
        return None
    from .render_schematic import _has_content
    return out_png if _has_content(out_png) else None


def _kind_name(kind: str) -> str:
    return {"Q": "BJT", "M": "MOSFET", "E": "VCVS", "G": "VCCS",
            "X": "子电路", "K": "耦合"}.get(kind, kind)
