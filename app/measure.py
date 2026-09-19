"""仿真输出解析与指标计算。spyci(MIT) 解析 ngspice raw 文件，numpy 算指标。  [W1]

所有出现在"证据卡片"上的数字必须来自这里，不许由 LLM 报数。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class Traces:
    time: np.ndarray | None          # .tran 的时间轴；.ac 时是频率轴
    signals: dict[str, np.ndarray]   # 名字如 v(out), i(v1)


def load_traces(raw_path: str | Path) -> Traces:
    import spyci  # 延迟导入，W1 时验证 API 名称（load_raw 返回 dict）

    data = spyci.load_raw(str(raw_path))
    signals: dict[str, np.ndarray] = {}
    time_axis = None
    for name, values in data.items():
        arr = np.asarray(values, dtype=float)
        key = name.lower()
        if key in ("time", "frequency", "freq", "sweep"):
            time_axis = arr
        else:
            signals[name] = arr
    return Traces(time=time_axis, signals=signals)


def extract_metrics(traces: Traces) -> dict[str, float]:
    """从波形算实测指标。规则保持简单、可解释：
    - 单信号：直流均值 / 峰峰值 / 最大最小值
    - 两个信号：幅值比（增益）、相位差
    - TODO(W1/W2): 截止频率(-3dB)、纹波、THD 等，按基准任务需要逐步加。
    """
    m: dict[str, float] = {}
    if not traces.signals:
        return m

    def clean(x: float) -> float:
        return round(float(x), 6)

    if len(traces.signals) == 1:
        name, arr = next(iter(traces.signals.items()))
        m[f"{name}_mean"] = clean(np.mean(arr))
        m[f"{name}_max"] = clean(np.max(arr))
        m[f"{name}_min"] = clean(np.min(arr))
        m[f"{name}_vpp"] = clean(np.ptp(arr))
    else:
        names = list(traces.signals)[:2]
        a, b = traces.signals[names[0]], traces.signals[names[1]]
        a_rms, b_rms = np.sqrt(np.mean(a**2)), np.sqrt(np.mean(b**2))
        m["amplitude_ratio"] = clean(b_rms / a_rms) if a_rms > 0 else float("inf")
        # 相位差（互相关最大处的时间偏移），仅瞬态有意义
        if traces.time is not None and len(traces.time) == len(a) == len(b):
            lags = np.correlate(b - b.mean(), a - a.mean(), mode="full")
            lag = lags.argmax() - (len(a) - 1)
            dt = float(np.mean(np.diff(traces.time)))
            m["phase_diff_deg"] = clean(-lag * dt * 360.0 * 1e0) if dt else 0.0

    return m


# TODO(W1): 单元测试——用固定 RC 电路的 raw 文件验证增益/时间轴解析正确。
