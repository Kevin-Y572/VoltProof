"""bench 验收器判分逻辑的离线验证：标准网表 → 仿真 → validator → 应全过。  [离线]

用法：python tests/test_bench_validators.py
（不调 LLM；judgement 正确性 = validator 逻辑 + measure 实测）
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

# ngspice 须在 PATH 中，或设 VOLTPROOF_NGSPICE 指向 ngspice.exe（见 README）

from bench_validators import VALIDATORS  # noqa: E402
from app.measure import load_traces  # noqa: E402
from app.ngspice_runner import run_netlist  # noqa: E402

# 标准答案网表（人类手写，指标确定）
NL = {
    "voltage-divider": """* div 12->5
V1 in 0 DC 12
R1 in out 7k
R2 out 0 5k
.control
set filetype=ascii
op
write out.raw v(out)
.endc
.end
""",
    "rc-charging": """* RC charge tau=1ms
V1 in 0 PULSE(0 5 0 1n 1n 10m 20m)
R1 in out 1k
C1 out 0 1u
.control
set filetype=ascii
tran 20u 10m
write out.raw v(out)
.endc
.end
""",
    "rc-lowpass": """* RC LPF 1k
V1 in 0 AC 1
R1 in out 1.59k
C1 out 0 100n
.control
set filetype=ascii
ac dec 20 10 100k
write out.raw v(out)
.endc
.end
""",
    "inverting-amp": """* inv amp gain -10
V1 in 0 SIN(0 0.1 1k)
R1 in mid 1k
R2 mid out 10k
X1 mid 0 out opamp
.subckt opamp a b c
E1 c 0 a b 100000
.ends
.control
set filetype=ascii
tran 20u 5m
write out.raw v(in) v(out)
.endc
.end
""",
    "noninverting-amp": """* noninv gain 11 = 1 + Rf/Rg = 1 + 10k/1k
V1 in 0 SIN(0 0.2 1k)
Rf out fb 10k
Rg fb 0 1k
X1 in fb out opamp
.subckt opamp a b c
E1 c 0 a b 100000
.ends
.control
set filetype=ascii
tran 20u 5m
write out.raw v(in) v(out)
.endc
.end
""",
    "zener-regulator": """* zener 5.1V（反接击穿：cathode=out, anode=gnd）
V1 in 0 DC 15
R1 in out 500
D1 0 out z5v1
.model z5v1 D(bv=5.1 ibv=1m)
.control
set filetype=ascii
op
write out.raw v(out)
.endc
.end
""",
    # 双分析 .control（ac 的 write 在最后）——runner 取到的是 ac.raw。
    # 曾把 AC 恒定幅值 1.0 当 tran 信号 ptp 判增益，除出 5e14 的假爆炸
    "sensor-conditioning": """* sensor cond: 0-10mV -> 0-5V, two-stage + 100Hz LPF
Vcc vcc 0 DC 12
Vin in 0 DC 0 AC 1 PWL(0 0 30m 10m)
.subckt opamp inp inn out
E1 out 0 VALUE={max(-11.5, min(1e5*v(inp,inn), 11.5))}
.ends
X1 in fb1 amp1 opamp
R1 amp1 fb1 19k
R2 fb1 0 1k
X2 amp1 fb2 amp2 opamp
R3 amp2 fb2 24k
R4 fb2 0 1k
R5 amp2 vout 1.59k
C1 vout 0 1u
.control
set filetype=ascii
tran 20u 30m
write tran.raw v(in) v(vout)
ac dec 20 1 100k
write ac.raw v(in) v(vout)
.endc
.end
""",
    # 对称张弛振荡器：无 .ic 不起振、理想 E 源无钳位——验收器必须抓住
    "square-osc": """* square osc 1k (relaxation, symmetric on purpose)
V1 vcc 0 DC 12
Rf out in 4.55k
C1 in 0 100n
R1 out fb 10k
R2 fb 0 10k
X1 fb in out opamp
.subckt opamp a b c
E1 c 0 a b 100000
.ends
.control
set filetype=ascii
tran 5u 10m
write out.raw v(out) v(in)
.endc
.end
""",
}

FAILS: list[str] = []


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cp_bv_"))
    for tid, nl in NL.items():
        if tid == "square-osc":
            continue  # 该网表"应判失败"，在下方专段验证
        v = VALIDATORS.get(tid)
        if not v:
            continue
        sim = run_netlist(nl, workdir=tmp / tid)
        checks = v(None, load_traces(sim.raw_path)) if sim.raw_path else v(None, None)
        bad = [c for c in checks if not c["ok"]]
        status = "PASS" if sim.ok and not bad else "FAIL"
        print(f"[{status}] {tid}: " + "; ".join(
            f"{'✓' if c['ok'] else '✗'}{c['name']}({c['detail']})" for c in checks))
        if not sim.ok or bad:
            FAILS.append(tid)

    # 反例：错指标网表必须被判 FAIL（防验收器形同虚设）
    wrong = """* div 12->9 (错指标)
V1 in 0 DC 12
R1 in out 9k
R2 out 0 27k
.control
set filetype=ascii
op
write out.raw v(out)
.endc
.end
"""
    sim = run_netlist(wrong, workdir=tmp / "wrong")
    checks = VALIDATORS["voltage-divider"](None, load_traces(sim.raw_path))
    bad = [c for c in checks if not c["ok"]]
    print(f"[{'PASS' if bad else 'FAIL'}] 反例: 12→9V 分压被正确判不达标")
    if not bad:
        FAILS.append("反例")

    # square-osc 闭环：对称网表必须被抓住"未起振"并给出 startup hint，
    # 且 tune_hint 确定性修复三轮内达标（.ic 起振 → min/max 钳位 → ~1kHz）
    from app.tuner import numeric_tune

    netlist = NL["square-osc"]
    caught_startup = False
    closed_loop = False
    for rnd in range(4):
        sim = run_netlist(netlist, workdir=tmp / f"osc{rnd}")
        if not sim.raw_path:
            break
        checks = VALIDATORS["square-osc"](None, load_traces(sim.raw_path))
        if all(c["ok"] for c in checks):
            closed_loop = True
            print(f"[{'PASS' if closed_loop else 'FAIL'}] square-osc: 确定性闭环第 {rnd} 轮全达标")
            break
        hints = [c.get("tune_hint") for c in checks if c.get("tune_hint")]
        if rnd == 0:
            caught_startup = any(h and h.get("kind") == "startup" for h in hints)
            print(f"[{'PASS' if caught_startup else 'FAIL'}] square-osc: 未起振被抓出并附 startup hint")
        tuned, _note = numeric_tune(netlist, hints) if hints else (None, "")
        if not tuned:
            break
        netlist = tuned
    if not closed_loop:
        FAILS.append("square-osc-闭环")

    print(f"\n{'全部通过' if not FAILS else '失败: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
