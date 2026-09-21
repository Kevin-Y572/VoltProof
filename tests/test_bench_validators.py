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

os.environ.setdefault("CIRCUITPILOT_NGSPICE",
                      "D:/Users/Lenovo/tools/ngspice-47/Spice64/bin/ngspice.exe")

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
}

FAILS: list[str] = []


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cp_bv_"))
    for tid, nl in NL.items():
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

    print(f"\n{'全部通过' if not FAILS else '失败: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
