"""综合实验在环验收器：把《综合实验要求.docx》的指标判定接入 pipeline 重试环。

与 bench/verify_exp.py（独立验收仪器，自写 raw 解析）判定口径对齐，但这里
复用产品的 measure 模块解析（load_traces 已修符号 bug + 均匀重采样），
在仿真成功后立即验收，不达标 → 差距回喂 LLM 调参重跑。

每个验收器签名：validator(evidence, traces) -> [{name, ok, detail}]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.measure import Traces, dominant_freq, harmonic_amp  # noqa: E402

# 激励/中间节点不算输出（综合实验电路里的振荡源、分频器输出）
_SRC_PAT = ("osc", "div", "clk", "pulse")
# 合成输出节点的典型命名
_OUT_PAT = ("sum", "synth", "tri", "sqr", "out", "sq")


def _near(v: float, target: float, tol: float) -> bool:
    return abs(v - target) <= tol * target


class SignalStats:
    """一路信号的谱分析结果（去暂态 20% 后）。"""

    def __init__(self, name: str, t: np.ndarray, y: np.ndarray):
        self.name = name
        self.vpp = float(np.ptp(y))
        self.mean = float(np.mean(y))
        dom = dominant_freq(t, y)
        self.f0 = dom[0] if dom else None
        # 谐波线幅度在 dominant_freq 判定非周期时无意义
        self.amp_cache: dict[float, float] = {}

    def harmonic(self, t: np.ndarray, y: np.ndarray, freq: float) -> float:
        if freq not in self.amp_cache:
            self.amp_cache[freq] = harmonic_amp(t, y, freq)
        return self.amp_cache[freq]


def _collect_outputs(traces: Traces) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """挑选待验收的输出信号：(信号名, (time, y))。排除激励节点。"""
    if traces is None or traces.time is None:
        return {}
    t = traces.time
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for name, y in traces.signals.items():
        if len(y) != len(t):
            continue
        low = name.lower()
        if any(p in low for p in _SRC_PAT):
            continue
        out[name] = (t, y)
    return out


def _find_tone(outs: dict, f_target: float, tol: float = 0.06):
    """找基频最接近 f_target 的信号，返回 (name, stats, (t, y)) 或 None。"""
    cands = []
    for name, (t, y) in outs.items():
        st = SignalStats(name, t, y)
        if st.f0 is not None and _near(st.f0, f_target, tol):
            cands.append((name, st, (t, y)))
    if not cands:
        return None
    # 若多个信号同频，取命名更像输出端的
    def rank(item):
        low = item[0].lower()
        return (any(p in low for p in _OUT_PAT), item[1].vpp)
    return max(cands, key=rank)


def _ck(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


# ---------------------------------------------------------------------------
# 各实验任务的验收器（与 verify_exp.py 判定口径一致）
# ---------------------------------------------------------------------------

def validator_exp1(ev, tr) -> list[dict]:
    """1kHz/3kHz 双正弦：频率、峰峰值 6V/2V(±15%)、无明显失真。"""
    outs = _collect_outputs(tr)
    s1 = _find_tone(outs, 1000)
    s3 = _find_tone(outs, 3000)
    res = []
    if s1 is None or s3 is None:
        missing = "1kHz" if s1 is None else "3kHz"
        res.append(_not_found(f"存在{missing}正弦输出", tr))
        return res
    (n1, st1, (t1, y1)), (n3, st3, (t3, y3)) = s1, s3
    res.append({**_ck("1kHz 频率", _near(st1.f0, 1000, 0.02), f"实测 {st1.f0:.1f}Hz"),
                **({"tune_hint": {"kind": "freq", "measured": st1.f0, "target": 1000, "freq": st1.f0}}
                   if not _near(st1.f0, 1000, 0.02) else {})})
    res.append({**_ck("1kHz 峰峰值≈6V", _near(st1.vpp, 6, 0.15), f"实测 vpp={st1.vpp:.3f}V（目标 6V±15%）"),
                **({"tune_hint": {"kind": "vpp", "measured": st1.vpp, "target": 6, "freq": st1.f0}}
                   if not _near(st1.vpp, 6, 0.15) else {})})
    res.append({**_ck("3kHz 频率", _near(st3.f0, 3000, 0.02), f"实测 {st3.f0:.1f}Hz"),
                **({"tune_hint": {"kind": "freq", "measured": st3.f0, "target": 3000, "freq": st3.f0}}
                   if not _near(st3.f0, 3000, 0.02) else {})})
    res.append({**_ck("3kHz 峰峰值≈2V", _near(st3.vpp, 2, 0.15), f"实测 vpp={st3.vpp:.3f}V（目标 2V±15%）"),
                **({"tune_hint": {"kind": "vpp", "measured": st3.vpp, "target": 2, "freq": st3.f0}}
                   if not _near(st3.vpp, 2, 0.15) else {})})
    # THD 用产品指标（extract_metrics 已算好，名字带信号名）
    for n, st, tgt in ((n1, st1, "1kHz"), (n3, st3, "3kHz")):
        thd = ev.metrics.get(f"{n}_thd")
        if thd is not None:
            res.append(_ck(f"{tgt} 失真 THD<5%", thd < 0.05, f"实测 thd={thd}"))
    return res


def _synth_output(outs):
    """合成类实验（exp2-4）的输出信号：优先 vpp>0.2V 的 sum/synth/tri 命名信号
    （0V 左右的'求和虚短点'也叫 sum，不能选），否则取 vpp 最大的非激励信号。"""
    if not outs:
        return None
    big = [(n, (t, y)) for n, (t, y) in outs.items() if float(np.ptp(y)) > 0.2]
    named = [(n, (t, y)) for n, (t, y) in big
             if any(p in n.lower() for p in ("sum", "synth", "tri"))]
    # 合成输出的特征：1k 与 3k 谱线都显著；单一谱线只是某路支路
    # （曾把 3kHz 支路误当合成输出，a3/a1 倒挂 2.3）
    def dual(kv):
        t, y = kv[1]
        a1, a3 = harmonic_amp(t, y, 1000), harmonic_amp(t, y, 3000)
        peak = max(a1, a3, 1e-12)
        return min(a1, a3) > 0.1 * peak

    for pool in ([kv for kv in named if dual(kv)],
                 [kv for kv in big if dual(kv)], named, big, list(outs.items())):
        if pool:
            n, (t, y) = max(pool, key=lambda kv: float(np.ptp(kv[1][1])))
            return n, SignalStats(n, t, y), (t, y)
    return None


def _signals_digest(traces) -> str:
    """全部输出信号的一行摘要，附在验收差距里——LLM 能直接看出
    write 写错了节点、输出幅度衰减、或电路没起振。"""
    outs = _collect_outputs(traces)
    parts = []
    for name, (t, y) in outs.items():
        st = SignalStats(name, t, y)
        f = f"{st.f0:.0f}Hz" if st.f0 else "非周期/直流"
        parts.append(f"{name}(vpp={st.vpp:.3g}V, {f})")
    return "; ".join(parts) or "(无信号)"


def _not_found(msg: str, tr) -> dict:
    return _ck(msg, False, f"{msg}。当前 write 的输出信号实测：{_signals_digest(tr)}")


def validator_exp2(ev, tr) -> list[dict]:
    """1k+3k 合成近似方波：明显 3 次谐波（a3/a1>5%）、幅度 5V（vpp±20%）。"""
    outs = _collect_outputs(tr)
    s = _synth_output(outs)
    if s is None or s[1].f0 is None:
        return [_not_found("存在合成输出（基波1kHz）", tr)]
    n, st, (t, y) = s
    res = [_ck("合成输出基波1kHz", _near(st.f0, 1000, 0.06), f"实测 f0={st.f0:.1f}Hz")]
    a1 = st.harmonic(t, y, 1000)
    a3 = st.harmonic(t, y, 3000)
    r3 = a3 / max(a1, 1e-9)
    res.append({**_ck("含明显3次谐波(方波特征)", r3 > 0.05,
                      f"a3/a1={r3:.3f}（理想 1/3）"),
                **({"tune_hint": {"kind": "harmonic", "measured": r3, "target": 1 / 3,
                                  "signal": n, "freq": 3000, "base_freq": 1000}}
                   if r3 <= 0.05 else {})})
    res.append({**_ck("合成幅度≈5V", _near(st.vpp, 5, 0.2),
                       f"实测 vpp={st.vpp:.3f}V（目标 5V±20%）"),
                **({"tune_hint": {"kind": "vpp", "measured": st.vpp, "target": 5, "freq": st.f0}}
                   if not _near(st.vpp, 5, 0.2) else {})})
    return res


def validator_exp3(ev, tr) -> list[dict]:
    """1k+3k+5k 合成：5 次谐波可见（a5/a1>5%）且 3 次仍在（a3/a1>5%）。"""
    outs = _collect_outputs(tr)
    s = _synth_output(outs)
    if s is None or s[1].f0 is None:
        return [_not_found("存在合成输出（基波1kHz）", tr)]
    n, st, (t, y) = s
    res = [_ck("合成输出基波1kHz", _near(st.f0, 1000, 0.06), f"实测 f0={st.f0:.1f}Hz")]
    a1 = st.harmonic(t, y, 1000)
    a3 = st.harmonic(t, y, 3000)
    a5 = st.harmonic(t, y, 5000)
    r5 = a5 / max(a1, 1e-9)
    res.append({**_ck("含5次谐波(5kHz)", r5 > 0.05, f"a5/a1={r5:.3f}"),
                **({"tune_hint": {"kind": "harmonic", "measured": r5, "target": 0.2,
                                  "signal": n, "freq": 5000, "base_freq": 1000}}
                   if r5 <= 0.05 else {})})
    res.append(_ck("奇次谐波构成1k/3k/5k", a3 > 0.05 * a1 and a5 > 0.05 * a1,
                   f"a3/a1={a3 / max(a1, 1e-9):.3f} a5/a1={a5 / max(a1, 1e-9):.3f}"))
    return res


def validator_exp4(ev, tr) -> list[dict]:
    """1k/3k/5k 合成近似三角波：a3/a1 应接近 1/9≈0.111（1/n² 衰减特征）。"""
    outs = _collect_outputs(tr)
    s = _synth_output(outs)
    if s is None or s[1].f0 is None:
        return [_not_found("存在合成输出（基波1kHz）", tr)]
    n, st, (t, y) = s
    res = [_ck("合成输出基波1kHz", _near(st.f0, 1000, 0.06), f"实测 f0={st.f0:.1f}Hz")]
    a1 = st.harmonic(t, y, 1000)
    a3 = st.harmonic(t, y, 3000)
    r3 = a3 / max(a1, 1e-9)
    bad4 = not (0.04 < r3 < 0.20)
    res.append({**_ck("谐波衰减接近1/n²(三角波特征 a3/a1≈0.111)",
                      not bad4, f"a3/a1={r3:.3f}（理想 0.111）"),
                **({"tune_hint": {"kind": "harmonic", "measured": r3, "target": 1 / 9,
                                  "signal": n, "freq": 3000, "base_freq": 1000}}
                   if bad4 else {})})
    return res


VALIDATORS = {
    "exp1-basic-sine-gen": [validator_exp1],
    "exp2-basic-square-synth": [validator_exp2],
    "exp3-ext-5th-harmonic": [validator_exp3],
    "exp4-ext-triangle-synth": [validator_exp4],
}


def get_validators(task_id: str) -> list:
    return VALIDATORS.get(task_id, [])
