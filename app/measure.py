"""仿真输出解析与指标计算。spyci(MIT) 解析 ngspice raw 文件，numpy 算指标。  [W1]

所有出现在"证据卡片"上的数字必须来自这里，不许由 LLM 报数。

符号纪律（2026-09-19 综合实验踩坑）：
  - 瞬态/工作点（flags=real）必须保留符号，取 .real——对 real 数据取 abs
    会把负半周全部翻正，波形和均值全是"全波整流"后的错误数据；
  - 交流扫描（flags=complex）才取模（幅频特性）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class Traces:
    time: np.ndarray | None          # .tran 的时间轴；.ac 时是频率轴
    signals: dict[str, np.ndarray]
    warning: str | None = None       # 非致命问题（如 spyci 降级原因），透出到证据
    # 数据来自哪种分析：ac（复数取模）/ tran / op（单点）。
    # 验收器必须区分——同一份"幅度"数组，AC 里是各频点增益、tran 里是瞬时
    # 电压，混用会把恒定 1.0 的 AC 源算成 ptp=0 再除出 1e14 的荒谬增益
    # （2026-09-22 bench 实测：sensor-conditioning 假失败即此因）
    analysis: str = "unknown"


def _infer_analysis(is_ac: bool, time_axis: np.ndarray | None) -> str:
    """is_ac 直接来自 raw 的 flags；无时间轴（单点）判 op，否则 tran。"""
    if is_ac:
        return "ac"
    if time_axis is None or len(time_axis) <= 1:
        return "op"
    return "tran"


def load_traces(raw_path: str | Path) -> Traces:
    # spyci 1.0.2 兼容垫片：其内部使用 np.complex_（NumPy 2.0 已移除）
    if not hasattr(np, "complex_"):
        np.complex_ = np.complex128
    # spyci 1.0.2：函数在 spyci.spyci 子模块，返回结构化数组 + vars 元数据
    from spyci.spyci import load_raw

    path = Path(raw_path)
    try:
        data = load_raw(str(path))
        is_ac = "complex" in str(data.get("flags", "")).lower()

        time_axis: np.ndarray | None = None
        signals: dict[str, np.ndarray] = {}
        for var in data["vars"]:
            name = var["name"]
            arr_c = np.asarray(data["values"][name], dtype=complex)
            arr = np.abs(arr_c) if is_ac else arr_c.real  # AC 取幅值，tran/op 保符号
            if var["type"] in ("time", "frequency") or name.lower() in ("time", "freq", "frequency"):
                time_axis = arr
            else:
                signals[name] = arr
        analysis = _infer_analysis(is_ac, time_axis)
        return Traces(time=time_axis, signals=signals, analysis=analysis)
    except Exception as e:
        # spyci 解析不了的边界：op 分析后 write 会产出重复变量名（v(in) 出现两次），
        # spyci 构造结构化数组时直接抛错——用内置简易解析器兜底（重名列去重）。
        # 不再静默：降级原因记入 warning 透出到证据卡。
        tr = _fallback_parse(path)
        tr.warning = f"spyci 解析失败，已用内置解析器兜底：{type(e).__name__}: {e}"
        return tr


def _fallback_parse(path: Path) -> Traces:
    """极简 ASCII raw 解析：Variables 段收集变量名（重名后列覆盖），Values 段
    按行号切点。只覆盖 ngspice write 产生的标准格式。"""
    var_names: list[str] = []
    var_types: dict[str, str] = {}
    section: str | None = None
    nums: list[complex] = []
    saw_complex = False  # 值里出现 "re,im" 逗号对 → AC 分析
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
                    saw_complex = True
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
        if saw_complex:
            mag = np.abs(arr)  # AC 幅值
        else:
            mag = arr.real     # 瞬态/工作点，保留符号
        if var_types.get(name) in ("time", "frequency") or name.lower() in ("time", "freq", "frequency"):
            time_axis = mag
        else:
            signals[name] = mag
    return Traces(time=time_axis, signals=signals,
                  analysis=_infer_analysis(saw_complex, time_axis))


# ---------------------------------------------------------------------------
# 谱分析（2026-09-19 新增）：ngspice 自适应步长的 raw 时间轴不均匀（dt 可差
# 两个数量级），直接 FFT 全是伪影——必须先重采样到均匀网格。
# ---------------------------------------------------------------------------

def resample_uniform(t: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """线性插值到等间隔网格（点数不变）。"""
    tu = np.linspace(float(t[0]), float(t[-1]), len(t))
    return tu, np.interp(tu, t, y)


def signal_spectrum(t: np.ndarray, y: np.ndarray, skip_fraction: float = 0.2):
    """去暂态 + 去均值 + rFFT 幅度谱。返回 (freqs, amps) 或 None（数据太短）。"""
    if t is None or len(t) < 32 or len(t) != len(y):
        return None
    tu, yu = resample_uniform(t, y)
    i0 = int(len(tu) * skip_fraction)
    yy = yu[i0:] - yu[i0:].mean()
    n = len(yy)
    dt = float(tu[1] - tu[0])
    freqs = np.fft.rfftfreq(n, dt)
    amps = np.abs(np.fft.rfft(yy)) * 2 / n
    return freqs, amps


def _peak_interp(freqs: np.ndarray, amps: np.ndarray, k: int) -> float:
    """抛物线插值细化峰值频率，达到亚 bin 精度（bin 很宽时必须）。"""
    df = float(freqs[1] - freqs[0])
    if 0 < k < len(amps) - 1:
        a, b, c = amps[k - 1], amps[k], amps[k + 1]
        den = a - 2 * b + c
        if abs(den) > 1e-15:
            return (k + 0.5 * (a - c) / den) * df
    return k * df


def dominant_freq(t: np.ndarray, y: np.ndarray) -> tuple[float, float] | None:
    """主频 (f0, 基波幅度)。判据：主峰幅度 > 峰峰值 5%，否则视为非周期信号。"""
    spec = signal_spectrum(t, y)
    if spec is None:
        return None
    freqs, amps = spec
    k = int(np.argmax(amps[1:])) + 1
    vpp = float(np.ptp(y))
    if vpp <= 0 or amps[k] < 0.05 * vpp / 2:
        return None
    return _peak_interp(freqs, amps, k), float(amps[k])


def thd_ratio(t: np.ndarray, y: np.ndarray, f0: float) -> float | None:
    """2..9 次谐波幅度平方和开方 / 基波幅度。"""
    spec = signal_spectrum(t, y)
    if spec is None or f0 <= 0:
        return None
    freqs, amps = spec
    df = float(freqs[1] - freqs[0])
    k1 = int(round(f0 / df))
    a1 = float(amps[k1]) if 0 < k1 < len(amps) else 0.0
    if a1 <= 0:
        return None
    harm = 0.0
    for k in range(2, 10):
        idx = int(round(k * k1))
        if idx < len(amps):
            harm += float(amps[idx]) ** 2
    return float(np.sqrt(harm)) / a1


def harmonic_amp(t: np.ndarray, y: np.ndarray, freq: float) -> float:
    """指定频率处的谱线幅度（最接近的 bin）。"""
    spec = signal_spectrum(t, y)
    if spec is None:
        return 0.0
    freqs, amps = spec
    idx = int(np.argmin(np.abs(freqs - freq)))
    return float(amps[idx])


def extract_metrics(traces: Traces) -> dict[str, float]:
    """从波形算实测指标。规则保持简单、可解释：
    - 每路信号：直流均值 / 极值 / 峰峰值 / RMS
    - 类周期信号：主频（FFT + 抛物线细化）与 THD
    - 两个及以上信号：前两路的幅值比
    """
    m: dict[str, float] = {}

    def clean(x: float) -> float:
        return round(float(x), 6)

    for name, arr in traces.signals.items():
        key = name.lower()
        m[f"{name}_mean"] = clean(np.mean(arr))
        m[f"{name}_max"] = clean(np.max(arr))
        m[f"{name}_min"] = clean(np.min(arr))
        m[f"{name}_vpp"] = clean(np.ptp(arr))
        m[f"{name}_rms"] = clean(np.sqrt(np.mean(arr ** 2)))
        if traces.time is not None and len(traces.time) == len(arr):
            dom = dominant_freq(traces.time, arr)
            if dom is not None:
                f0, _a0 = dom
                m[f"{name}_freq_Hz"] = clean(f0)
                thd = thd_ratio(traces.time, arr, f0)
                if thd is not None:
                    m[f"{name}_thd"] = clean(thd)

    if len(traces.signals) >= 2:
        names = list(traces.signals)[:2]
        a, b = traces.signals[names[0]], traces.signals[names[1]]
        a_rms, b_rms = np.sqrt(np.mean(a ** 2)), np.sqrt(np.mean(b ** 2))
        if a_rms > 0:
            m[f"ratio_{names[0]}_{names[1]}"] = clean(b_rms / a_rms)

    return m
