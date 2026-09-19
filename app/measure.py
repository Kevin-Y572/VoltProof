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
    # spyci 1.0.2 兼容垫片：其内部使用 np.complex_（NumPy 2.0 已移除）
    if not hasattr(np, "complex_"):
        np.complex_ = np.complex128
    # spyci 1.0.2：函数在 spyci.spyci 子模块，返回结构化数组 + vars 元数据
    from spyci.spyci import load_raw

    path = Path(raw_path)
    try:
        data = load_raw(str(path))
        vars_meta, values = data["vars"], data["values"]

        time_axis: np.ndarray | None = None
        signals: dict[str, np.ndarray] = {}
        for var in vars_meta:
            name = var["name"]
            arr = np.abs(np.asarray(values[name], dtype=complex))  # AC 复数取模，tran 虚部为 0
            if var["type"] in ("time", "frequency") or name.lower() in ("time", "freq", "frequency"):
                time_axis = arr
            else:
                signals[name] = arr
        return Traces(time=time_axis, signals=signals)
    except Exception:
        # spyci 解析不了的边界：op 分析后 write 会产出重复变量名（v(in) 出现两次），
        # spyci 构造结构化数组时直接抛错——用内置简易解析器兜底（重名列去重）
        return _fallback_parse(path)


def _fallback_parse(path: Path) -> Traces:
    """极简 ASCII raw 解析：Variables 段收集变量名（重名后列覆盖），Values 段
    按行号切点。只覆盖 ngspice write 产生的标准格式。"""
    var_names: list[str] = []
    var_types: dict[str, str] = {}
    section: str | None = None
    nums: list[complex] = []
    point_count = 0
    for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = ln.strip()
        if s == "Variables:":
            section = "vars"
            continue
        if s == "Values:":
            section = "vals"
            continue
        if section == "vars":
            parts = s.split()
            if len(parts) >= 3 and parts[0].isdigit():
                var_names.append(parts[1])
                var_types[parts[1]] = parts[2]
        elif section == "vals" and s:
            parts = s.split()
            if parts[0] == str(point_count):  # 行首的点索引，跳过
                parts = parts[1:]
                point_count += 1
            for tok in parts:
                if "," in tok:  # complex "re,im"
                    re_s, im_s = tok.split(",", 1)
                    nums.append(complex(float(re_s), float(im_s)))
                else:
                    try:
                        nums.append(complex(float(tok), 0.0))
                    except ValueError:
                        pass
    nvars = max(len(var_names), 1)
    npoints = len(nums) // nvars
    cols: dict[str, np.ndarray] = {}
    for j, name in enumerate(var_names):
        cols[name] = np.asarray([nums[i * nvars + j] for i in range(npoints)], dtype=complex)

    time_axis = None
    signals: dict[str, np.ndarray] = {}
    for name, arr in cols.items():
        mag = np.abs(arr)
        if var_types.get(name) in ("time", "frequency") or name.lower() in ("time", "freq", "frequency"):
            time_axis = mag
        else:
            signals[name] = mag
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
