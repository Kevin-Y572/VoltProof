"""bench 任务的指标级验收器——让"10/10 通过"变成真正的指标达标。

此前 bench 只判"仿真跑通"（ok=True），从未验证过 -3dB 是否真在 1kHz、
分压是否真出 5V。本模块按 exp_validators 同一模式为 10 个任务配
"从仿真波形实测判分"的检查项（判分靠仿真器数据，不是 LLM 自评）。

每个验收器：validator(evidence, traces) -> [{name, ok, detail}]
traces 为 None 时全部判不通过（无波形无从判分）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.measure import Traces  # noqa: E402


def _ck(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def _sig(tr: Traces | None, frag: str):
    """按名字片段找信号（v(out) 等）；返回 (time|None, arr) 或 None。
    op 分析没有扫描轴（time=None），单点数据同样可判分。"""
    if tr is None:
        return None
    for name, arr in tr.signals.items():
        if frag.lower() in name.lower():
            if tr.time is None or len(tr.time) == len(arr):
                return tr.time, arr
    return None


def _near(v, target, tol):
    return abs(v - target) <= tol * target


# ---------------------------------------------------------------------------
# 各任务验收器
# ---------------------------------------------------------------------------

def v_divider(ev, tr):
    s = _sig(tr, "out") or _sig(tr, "v(")
    if s is None:
        return [_ck("输出电压≈5V", False, "无输出信号")]
    _t, y = s
    v = float(np.mean(y))
    return [_ck("输出电压≈5V(±10%)", _near(v, 5, 0.10), f"实测 {v:.3f}V")]


def v_rc_charging(ev, tr):
    s = _sig(tr, "out") or _sig(tr, "v(")
    if s is None:
        return [_ck("充电特性", False, "无输出信号")]
    t, y = s
    final = float(np.mean(y[-max(len(y) // 10, 3):]))  # 末段均值≈终值
    i63 = int(np.argmax(y >= 0.632 * final)) if final > 0 else -1
    tau = float(t[i63]) if i63 > 0 else -1.0
    return [
        _ck("终值≈5V(±10%)", _near(final, 5, 0.10), f"实测 {final:.3f}V"),
        _ck("时间常数≈1ms(±25%)", 0 < tau and _near(tau, 1e-3, 0.25),
            f"实测 {tau * 1e3:.2f}ms"),
    ]


def v_rc_lowpass(ev, tr):
    s = _sig(tr, "out") or _sig(tr, "v(")
    if s is None:
        return [_ck("-3dB 截止频率", False, "无输出信号")]
    f, mag = s  # ac 分析时 time 轴即频率轴
    peak = float(np.max(mag))
    below = np.where(mag < 0.707 * peak)[0]
    fc = float(f[below[0]]) if len(below) else float("inf")
    return [_ck("-3dB 截止≈1kHz(±30%)", _near(fc, 1000, 0.30), f"实测 {fc:.0f}Hz")]


def _gain_check(ev, tr, target, tol, name, frac_out="out", frac_in="in"):
    so = _sig(tr, frac_out)
    si = _sig(tr, frac_in)
    if so is None or si is None:
        return [_ck(name, False, "缺输入或输出信号")]
    _to, yo = so
    _ti, yi = si
    gain = float(np.ptp(yo)) / max(float(np.ptp(yi)), 1e-12)
    return [_ck(name, _near(gain, target, tol), f"实测幅度增益 {gain:.2f}（目标 {target}）")]


def v_inverting(ev, tr):
    return _gain_check(ev, tr, 10, 0.25, "增益幅度≈10(±25%)",
                       frac_out="out", frac_in="in")


def v_noninverting(ev, tr):
    return _gain_check(ev, tr, 11, 0.25, "增益≈11(±25%)",
                       frac_out="out", frac_in="in")


def v_ce_amplifier(ev, tr):
    return _gain_check(ev, tr, 60, 0.60, "增益>50（目标区间 24~96）",
                       frac_out="out", frac_in="in")


def v_rectifier(ev, tr):
    s = _sig(tr, "out") or _sig(tr, "v(")
    if s is None:
        return [_ck("整流输出", False, "无输出信号")]
    t, y = s
    i0 = int(len(y) * 0.6)  # 去起始暂态
    yy = y[i0:]
    dc = float(np.mean(yy))
    ripple = float(np.ptp(yy))
    return [
        _ck("直流输出>10V", dc > 10, f"实测 {dc:.2f}V"),
        _ck("纹波<1.5V", ripple < 1.5, f"实测峰峰 {ripple:.2f}V"),
    ]


def v_zener(ev, tr):
    s = _sig(tr, "out") or _sig(tr, "v(")
    if s is None:
        return [_ck("稳压输出", False, "无输出信号")]
    _t, y = s
    v = float(np.mean(y))
    return [_ck("输出≈5.1V(±10%)", _near(v, 5.1, 0.10), f"实测 {v:.3f}V")]


def v_square_osc(ev, tr):
    s = _sig(tr, "out") or _sig(tr, "v(")
    if s is None:
        return [_ck("方波频率", False, "无输出信号")]
    from app.measure import dominant_freq
    t, y = s
    dom = dominant_freq(t, y)
    if dom is None:
        return [_ck("方波频率≈1kHz", False, "信号非周期（未起振？）")]
    f0 = dom[0]
    vpp = float(np.ptp(y))
    return [
        _ck("频率≈1kHz(±15%)", _near(f0, 1000, 0.15), f"实测 {f0:.0f}Hz"),
        _ck("为周期信号（已起振）", vpp > 0.5, f"vpp={vpp:.2f}V"),
    ]


def v_sensor(ev, tr):
    return _gain_check(ev, tr, 500, 0.35, "总增益≈500(±35%)",
                       frac_out="out", frac_in="in")


VALIDATORS = {
    "voltage-divider": v_divider,
    "rc-charging": v_rc_charging,
    "rc-lowpass": v_rc_lowpass,
    "inverting-amp": v_inverting,
    "noninverting-amp": v_noninverting,
    "ce-amplifier": v_ce_amplifier,
    "rectifier-filter": v_rectifier,
    "zener-regulator": v_zener,
    "square-osc": v_square_osc,
    "sensor-conditioning": v_sensor,
}


def get_validators(task_id: str) -> list:
    v = VALIDATORS.get(task_id)
    return [v] if v else []
