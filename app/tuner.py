"""程序化数值调参：按验收器输出的结构化 tune_hint 确定性地修改网表数值。

动机（2026-09-20）：调参环此前由 LLM 全量重写网表执行 f∝1/RC 类比例校正，
执行不稳定（exp1 频率在 900↔1179Hz 摆动）。判分层（验收器）比 LLM 更清楚
"哪个指标差多少"，让它直接输出可执行的调参指令，本模块确定性执行——
数值搜索 + 仿真验证，比语言模型重写收敛可靠。

架构（红线：判分靠仿真器，调参靠数值搜索）：
  validator 的 check 可带 tune_hint 字段：
    {"kind": "vpp"|"freq", "measured": x, "target": y, "freq": 信号频率}
  tuner 按 hint 修改 SIN 源的幅度/频率（比例校正，线性近似下每轮
  按 实测/目标 缩放，类似不动点迭代）。
找不到匹配源（如自激振荡电路无 SIN 源）返回 None，管线回退 LLM 调参。
"""

from __future__ import annotations

import re

# SIN(偏置 幅度 频率 ...)：数值支持 SPICE 后缀单位（1k / 2.5meg / 100u …）
_SIN_RE = re.compile(
    r"(SIN\(\s*[-+0-9.eE]+[a-zA-Z]*\s+)([-+0-9.eE]+[a-zA-Z]*)(\s+)([-+0-9.eE]+[a-zA-Z]*)",
    re.IGNORECASE)

_SUFFIX = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3,
           "u": 1e-6, "µ": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}


def _spice_val(s: str) -> float:
    """'1k'->1000.0，'2.5meg'->2.5e6，'100u'->1e-4。"""
    m = re.fullmatch(r"([-+0-9.eE]+)([a-zA-Z]*)", s.strip())
    if not m:
        return 0.0
    val = float(m.group(1))
    suf = m.group(2).lower()
    if suf in _SUFFIX:
        return val * _SUFFIX[suf]
    return val  # 无后缀或未知后缀（当纯数值）


def _parse_sin_sources(netlist: str) -> list[dict]:
    """返回 [{line_idx, line, amp, freq, m_amp, m_freq}]，m_* 为 match 组。"""
    out = []
    for i, line in enumerate(netlist.splitlines()):
        if line.strip().upper().startswith("V"):
            m = _SIN_RE.search(line)
            if m:
                out.append({
                    "line_idx": i, "line": line,
                    "amp": _spice_val(m.group(2)), "freq": _spice_val(m.group(4)),
                    "m": m,
                })
    return out


def numeric_tune(netlist: str, hints: list[dict]) -> tuple[str | None, str]:
    """按 tune_hint 修改网表数值。返回 (新网表, 调参说明)；无可执行项返回 (None, "")。

    vpp hint：在频率最接近 hint["freq"] 的 SIN 源上，幅度 × (target/measured)。
    freq hint：该 SIN 源频率 × (target/measured)。
    harmonic hint：加法器线性，a_h/a_1 = (R_1·A_h)/(R_h·A_1)——把接到
      signal 节点、另一端连着 hint.freq 频率 SIN 源的电阻 R_h 改为
      R_h × (measured/target)，比值精确校正（源幅度不变时）。
    """
    lines = netlist.splitlines()
    srcs = _parse_sin_sources(netlist)
    notes: list[str] = []
    changed = False
    for h in hints:
        kind = h.get("kind")
        measured, target = h.get("measured"), h.get("target")
        if kind not in ("vpp", "freq", "harmonic") or not measured or not target:
            continue
        if measured <= 0 or target <= 0:
            continue
        ratio = target / measured
        # 限制单轮缩放幅度，避免一次过头
        ratio = max(0.05, min(20.0, ratio))
        if kind == "harmonic":
            # R_h 与谐波比成反比：新比值 T = M / (R_new/R_old) → R_new = R_old × M/T
            inv = 1.0 / ratio
            res = _tune_harmonic_resistor(netlist, lines, srcs, h, inv)
            if res is None:
                continue
            _idx, _newline, note = res
            lines[_idx] = _newline
            notes.append(note)
            changed = True
            continue
        anchor = h.get("freq") or 0.0
        cand = min(srcs, key=lambda s: abs(s["freq"] - anchor)) if srcs else None
        if cand is None or abs(cand["freq"] - anchor) > max(0.25 * anchor, 200):
            continue  # 找不到对应源（如自激振荡电路），回退 LLM
        m = cand["m"]
        if kind == "vpp":
            new_amp = cand["amp"] * ratio
            new_line = cand["line"][:m.start(2)] + _fmt(new_amp) + cand["line"][m.end(2):]
            notes.append(f"数值调参：{cand['line'].split()[0]} 幅度 {cand['amp']:g}→{new_amp:g}"
                         f"（vpp 实测 {measured:g} → 目标 {target:g}）")
        else:
            new_freq = cand["freq"] * ratio
            new_line = cand["line"][:m.start(4)] + _fmt(new_freq) + cand["line"][m.end(4):]
            notes.append(f"数值调参：{cand['line'].split()[0]} 频率 {cand['freq']:g}→{new_freq:g}"
                         f"（实测 {measured:g} → 目标 {target:g}）")
        lines[cand["line_idx"]] = new_line
        # 更新缓存中的源（同源可能连续两个 hint：先 vpp 后 freq）
        cand["line"] = new_line
        rem = _SIN_RE.search(new_line)
        if rem:
            cand["amp"] = _spice_val(rem.group(2))
            cand["freq"] = _spice_val(rem.group(4))
            cand["m"] = rem
        changed = True
    if not changed:
        return None, ""
    return "\n".join(lines) + "\n", "；".join(notes)


def _tune_harmonic_resistor(netlist: str, lines: list[str], srcs: list[dict],
                            h: dict, inv: float) -> tuple[int, str, str] | None:
    """定位加权电阻并按比例改值：signal 节点上、另一端接频率≈hint.freq 的
    SIN 源的电阻。返回 (行号, 新行, 说明)。"""
    signal = str(h.get("signal", "")).lower().strip()
    hfreq = float(h.get("freq", 0))
    # 节点 -> SIN 源所在行（源引脚1 的节点名）
    src_node: dict[str, dict] = {}
    for i, ln in enumerate(lines):
        toks = ln.split()
        if toks and toks[0].upper().startswith("V") and len(toks) >= 2:
            m = _SIN_RE.search(ln)
            if m:
                src_node[toks[1].lower()] = {"freq": _spice_val(m.group(4)), "idx": i}
    for i, ln in enumerate(lines):
        toks = ln.split()
        if len(toks) < 4 or not toks[0].upper().startswith("R"):
            continue
        n1, n2 = toks[1].lower(), toks[2].lower()
        # 一端接 signal 节点，另一端接目标谐波源
        for a, b in ((n1, n2), (n2, n1)):
            if a != signal or b not in src_node:
                continue
            sc = src_node[b]
            tol = max(0.25 * hfreq, 200)
            if abs(sc["freq"] - hfreq) > tol:
                continue
            old_val = _spice_val(toks[3])
            if old_val <= 0:
                continue
            new_val = old_val * inv
            toks[3] = _fmt(new_val)
            note = (f"数值调参：{toks[0]} {old_val:g}→{new_val:g}"
                    f"（谐波比实测 {h['measured']:.3f} → 目标 {h['target']:.3f}）")
            return i, " ".join(toks), note
    return None


def _fmt(v: float) -> str:
    """数值格式化：保留有效位，去多余小数。"""
    if v >= 100 or v == int(v):
        return f"{v:.6g}"
    return f"{v:.6g}"
