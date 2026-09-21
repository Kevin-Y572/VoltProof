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
    if tr is not None and tr.analysis == "ac":
        # AC 数据是各频点幅值：增益 = 通带内 |out|/|in|（低频段均值）。
        # 不能用 ptp——AC 源幅值恒定（如 AC 1 时 in 全程=1.0，ptp=0），
        # 除以 ptp 会得出 1e14 级荒谬增益（2026-09-22 sensor 假失败实锤）
        n = max(len(yo) // 6, 1)  # 最低 ~1/6 频程视为通带
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.asarray(yo[:n], dtype=float) / np.asarray(yi[:n], dtype=float)
        ratio = ratio[np.isfinite(ratio) & (np.abs(np.asarray(yi[:n], dtype=float)) > 1e-12)]
        if len(ratio) == 0:
            return [_ck(name, False, "AC 通带内输入幅值≈0，增益无法计算")]
        gain = float(np.mean(ratio))
        return [_ck(name, _near(gain, target, tol),
                    f"AC 通带增益 {gain:.2f}（目标 {target}）")]
    if float(np.ptp(yi)) < 1e-9:
        # 输入是常数（如激励没接进来/直流偏置）——报真实原因，别报 1e14
        return [_ck(name, False,
                    f"输入信号峰峰值为 {float(np.ptp(yi)):.2g}（恒定），增益无从谈起")]
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
        # 未起振是结构性问题，不是数值差多少——回喂可执行的结构修复指导
        # （tuner 确定性插 .ic；LLM 路径也能按 message 里的清单自查）
        return [{
            "name": "为周期信号（已起振）", "ok": False,
            "detail": "信号非周期（未起振）：对称静态点问题。修复清单——"
                      "① .ic 给定时电容设初始电压打破对称；② 核对环路增益>1"
                      "（反馈衰减×放大倍数）；③ 两侧元件值轻微不对称；"
                      "④ .tran 时长≥10 个目标周期",
            "tune_hint": {
                "kind": "startup",
                "message": "输出恒定未起振：插入 .ic 打破定时电容的对称静态点",
            },
        }]
    f0 = dom[0]
    vpp = float(np.ptp(y))
    checks = []
    if vpp > 100.0:
        # 理想受控源没接电源轨——幅度上天（实测见过 2.8e10V），此时频率
        # 也被巨幅摆动拖偏，必须先钳幅再谈频率
        checks.append({
            "name": "输出幅度在电源轨内", "ok": False,
            "detail": f"vpp={vpp:.3g}V——理想运放/受控源未限幅。给运放子电路的"
                      " E 源输出加钳位（注意 ngspice 的 limit() 不起作用，"
                      "必须用 min/max）：E1 out 0 VALUE={{max(-11.5,"
                      " min(1e5*v(a,b), 11.5))}}",
            "tune_hint": {"kind": "clamp",
                          "message": "输出超出电源轨：E 源输出用 min/max 钳位"},
        })
    else:
        checks.append(_ck("输出幅度在电源轨内", True, f"vpp={vpp:.2f}V"))
    checks.append(_ck("频率≈1kHz(±15%)", _near(f0, 1000, 0.15), f"实测 {f0:.0f}Hz"))
    checks.append(_ck("为周期信号（已起振）", vpp > 0.5, f"vpp={vpp:.2f}V"))
    return checks


def v_sensor(ev, tr):
    checks = _gain_check(ev, tr, 500, 0.35, "总增益≈500(±35%)",
                         frac_out="out", frac_in="in")
    # 任务要求一阶低通 -3dB≈100Hz——只对 AC 数据判，缺数据给出补扫指导
    if tr is not None and tr.analysis == "ac":
        s = _sig(tr, "out") or _sig(tr, "v(")
        if s and s[0] is not None:
            f, mag = s
            peak = float(np.max(mag))
            below = np.where(mag < 0.707 * peak)[0]
            fc = float(f[below[0]]) if len(below) else float("inf")
            checks.append(_ck("-3dB≈100Hz(±50%)", _near(fc, 100, 0.50),
                              f"实测 {fc:.0f}Hz"))
    else:
        checks.append(_ck("-3dB≈100Hz(±50%)", False,
                          "无交流扫描数据：请在 .control 增加 ac 扫描"
                          "（如 ac dec 20 1 100k）并 write，放在最后"))
    return checks


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
