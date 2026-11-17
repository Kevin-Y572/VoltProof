"""what-if 参数扫描：改一个元件值 → 逐值真实仿真 → 曲线族 + 指标表。

纯确定性、零 LLM：网表数值改写与判分全部来自既有解析层（tuner 的数值
语法、measure 的指标计算）。这是"仿真在环"架构的自然延伸——趋势不是
模型想象的，是 ngspice 逐点算出来的。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import numpy as np

from . import measure
from .ngspice_runner import run_netlist
from .tuner import _fmt

# 可扫元件：R/C/L 的值字段是 tokens[3]；V/I 只在 tokens[3] 为纯数值
# （直流值）时可扫——SIN/PULSE 等波形参数在别的字段，改了会破坏语法
_SCAN_KINDS = ("R", "C", "L", "V", "I")

# 纯数值（可作直流值/电阻电容值）："12"、"2.5k"、"1e-6"；"SIN(0" 不匹配
_PLAIN_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
                           r"(?:meg|[tTgGkKmuUnpPfF])?")

METRIC_NAMES = {"vpp": "输出峰峰值", "mean": "输出均值", "freq": "主频",
                "gain": "增益（out/in）", "fc": "-3dB 截止频率"}


def canvas_with_component_value(canvas: dict | None, component: str, value: str) -> dict | None:
    """只改被扫描元件的数值，保留手动画布的位置、接地和标签。"""
    if not isinstance(canvas, dict) or not isinstance(canvas.get("comps"), list):
        return None
    import copy
    out = copy.deepcopy(canvas)
    found = False
    for comp in out["comps"]:
        if isinstance(comp, dict) and str(comp.get("name", "")).lower() == str(component).lower():
            comp["value"] = value
            found = True
            break
    return out if found else None


def scan_components(netlist: str) -> list[dict]:
    """列出网表里可扫描的元件（名称/类型/当前值/节点），供前端下拉。"""
    out: list[dict] = []
    for lineno, raw in enumerate(netlist.splitlines()):
        s = raw.strip()
        if not s or s.startswith(("*", ".", "+")):
            continue
        kind = s[0].upper()
        if kind not in _SCAN_KINDS:
            continue
        toks = s.split()
        if kind in "RCL" and len(toks) >= 4:
            out.append({"name": toks[0], "kind": kind, "value": toks[3],
                        "nodes": f"{toks[1]}–{toks[2]}", "line": lineno})
        elif kind in "VI" and len(toks) >= 4:
            idx = 4 if toks[3].lower() == "dc" else 3
            if len(toks) > idx and _is_plain_number(toks[idx]):
                out.append({"name": toks[0], "kind": kind, "value": toks[idx], "value_index": idx,
                            "nodes": f"{toks[1]}–{toks[2]}", "line": lineno})
    return out


def _is_plain_number(tok: str) -> bool:
    return bool(_PLAIN_NUM_RE.fullmatch(tok))


def _set_component(lines: list[str], lineno: int, new_value: str, value_index=3) -> str:
    toks = lines[lineno].split()
    toks[value_index] = new_value
    lines = list(lines)
    lines[lineno] = " ".join(toks)
    return "\n".join(lines)


def _sig(tr, frag: str):
    if tr is None:
        return None
    for name, arr in tr.signals.items():
        if frag.lower() in name.lower():
            if tr.time is None or len(tr.time) == len(arr):
                return tr.time, arr
    return None


def compute_metric(tr, metric: str, out_frag: str, in_frag: str):
    return measure.compute_metric(tr, metric, out_frag, in_frag)


def run_scan(netlist: str, component: str, values: list[str], metric: str,
             out_frag: str = "out", in_frag: str = "in",
             workdir: Path | None = None, max_points: int = 12, requirements=None) -> dict:
    """对 component 逐个取 values 里的值真实仿真，返回逐点结果 + 曲线族 PNG。"""
    comp = next((c for c in scan_components(netlist) if c["name"] == component), None)
    if comp is None:
        return {"error": f"网表里没有可扫描的元件 '{component}'（只支持 R/C/L 与直流源）"}
    if metric not in METRIC_NAMES:
        return {"error": f"未知指标 '{metric}'"}
    values = [str(v).strip() for v in values if str(v).strip()][:max_points]
    if not values:
        return {"error": "扫描值列表为空"}
    if any(not _is_plain_number(v) for v in values):
        return {"error": "扫描值必须是单个 SPICE 数值，如 1k、47n；不能包含表达式或命令"}
    workdir = workdir or Path(".voltproof_whatif")
    workdir.mkdir(parents=True, exist_ok=True)

    lines = netlist.splitlines()
    points: list[dict] = []
    curves: list[tuple[str, object, np.ndarray]] = []
    for index, v in enumerate(values):
        nl = _set_component(lines, comp["line"], v, comp.get("value_index", 3))
        sim = run_netlist(nl, workdir=workdir / f"point-{index + 1}")
        if not sim.ok or sim.raw_path is None:
            points.append({"value": v, "ok": False,
                           "error": (sim.error_snippet or "仿真失败")[:160]})
            continue
        try:
            tr = measure.load_traces(sim.raw_path)
            measure.validate_traces(tr)
        except Exception as e:
            points.append({"value": v, "ok": False, "error": f"解析失败: {e}"})
            continue
        val, err = measure.compute_metric(tr, metric, out_frag, in_frag, netlist=nl)
        if val is None:
            points.append({"value": v, "ok": False, "error": err})
            continue
        from . import specs
        checks = specs.evaluate(tr, requirements or [], nl)
        points.append({"value": v, "ok": True, "metric": round(val, 6),
                       "checks": checks, "passed": all(c.get("ok") for c in checks) if checks else None,
                       "metrics": measure.extract_metrics(tr)})
        so = _sig(tr, out_frag)
        if so:
            curves.append((v, so[0], so[1]))

    png = _plot_family(curves, metric, out_frag, component, workdir,
                       analysis=tr.analysis if curves else "unknown")
    out = {"component": component, "metric": metric,
           "metric_name": METRIC_NAMES[metric], "points": points,
           "note": "瞬态去除前20%时间暂态；扫描基于固定电路版本。候选方案应用前需确认。"}
    if png:
        out["png_b64"] = base64.b64encode(png.read_bytes()).decode()
    return out


def _plot_family(curves, metric: str, out_frag: str, comp: str, workdir: Path, analysis="unknown") -> Path | None:
    """曲线族叠加图：颜色按参数值渐变（viridis），AC 用对数横轴。"""
    if not curves:
        return None
    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei",
                                              "Noto Sans CJK SC", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    import matplotlib.pyplot as plt

    is_ac = analysis == "ac"
    fig, ax = plt.subplots(figsize=(7, 3.6), dpi=130)
    colors = plt.cm.viridis(np.linspace(0, 0.9, len(curves)))
    for (v, t, y), c in zip(curves, colors):
        x = t if t is not None else np.arange(len(y))
        ax.plot(x[: len(y)], y, linewidth=1.1, color=c, label=str(v))
    if is_ac:
        ax.set_xscale("log")
        ax.set_xlabel("frequency (Hz)")
    else:
        ax.set_xlabel("time (s)" if curves[0][1] is not None else "sample")
    ax.set_ylabel(out_frag)
    ax.grid(True, alpha=0.3, which="both")
    ax.set_title(f"what-if：{comp} 扫描（{METRIC_NAMES.get(metric, metric)}）", fontsize=10)
    ax.legend(title=comp, loc="best", fontsize=7.5, title_fontsize=8)
    fig.tight_layout()
    out_png = workdir / "whatif.png"
    fig.savefig(out_png)
    plt.close(fig)
    return out_png
