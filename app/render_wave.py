"""波形渲染为 PNG（matplotlib，PSF 协议）。  [W1]"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无头渲染，服务器环境必须
# 中文标题/图例不变成豆腐块：Windows 优先微软雅黑/黑体
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
matplotlib.rcParams["axes.unicode_minus"] = False
import matplotlib.pyplot as plt

from .measure import Traces


def render_wave(traces: Traces, out_png: str | Path, title: str = "") -> Path:
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)

    fig, ax1 = plt.subplots(figsize=(7, 3.2), dpi=130)
    x = traces.time if traces.time is not None else range(len(next(iter(traces.signals.values()))))

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    for i, (name, arr) in enumerate(traces.signals.items()):
        ax1.plot(x[: len(arr)], arr, label=name, linewidth=1.2, color=colors[i % len(colors)])

    xlabel = "time (s)" if traces.time is not None else "sample"
    ax1.set_xlabel(xlabel)
    ax1.set_ylabel("voltage / current")
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc="best", fontsize=8)
    if title:
        ax1.set_title(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    return out_png


# TODO(W1): 双 y 轴（电压/电流混绘）；.ac 结果改用对数横轴。
