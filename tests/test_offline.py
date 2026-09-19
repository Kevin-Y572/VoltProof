"""离线冒烟测试：不依赖 LLM，验证仿真器/解析/绘图/检查器链路。  [W1/W2]

运行：python tests/test_offline.py   （零依赖自写断言，不引入 pytest）
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 本机 ngspice 安装位置（已 setx 到用户环境变量，此处兜底保证测试可独立运行）
import os

os.environ.setdefault("CIRCUITPILOT_NGSPICE",
                      "D:/Users/Lenovo/tools/ngspice-47/Spice64/bin/ngspice.exe")

from app import checks, llm, measure  # noqa: E402
from app.ngspice_runner import run_netlist  # noqa: E402
from app.render_wave import render_wave  # noqa: E402

import numpy as np  # noqa: E402

RC_NETLIST = """\
* RC charging circuit
V1 in 0 DC 5
R1 in out 1k
C1 out 0 1u
.control
set filetype=ascii
tran 10u 5m
write out.raw v(out)
.endc
.end
"""

BAD_NETLIST = """\
* no ground + floating node
V1 in gndx DC 5
R1 in mid 1k
R2 mid floater 2k
.control
op
write out.raw v(mid)
.endc
.end
"""

AC_NETLIST = """\
* RC lowpass 1kHz
V1 in 0 AC 1
R1 in out 1.59k
C1 out 0 100n
.control
set filetype=ascii
ac dec 20 10 100k
write out.raw v(out)
.endc
.end
"""

OP_NETLIST = """\
* voltage divider
V1 in 0 DC 12
R1 in out 7k
R2 out 0 5k
.control
set filetype=ascii
op
write out.raw v(out)
.endc
.end
"""

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cp_test_"))
    try:
        # ---- 1. 好网表：仿真 ok + raw 落盘 ----
        sim = run_netlist(RC_NETLIST, workdir=tmp / "good")
        check("RC 仿真通过", sim.ok, f"stderr={sim.stderr[:200]}")
        check("raw 文件生成", sim.raw_path is not None and sim.raw_path.exists())
        check("仿真耗时 <10s", sim.elapsed < 10, f"{sim.elapsed:.1f}s")

        # ---- 2. 波形解析 + 指标 + 渲染 ----
        if sim.raw_path:
            tr = measure.load_traces(sim.raw_path)
            check("解析到时间轴", tr.time is not None and len(tr.time) > 100,
                  f"len={0 if tr.time is None else len(tr.time)}")
            check("解析到 v(out) 信号", any("out" in k.lower() for k in tr.signals),
                  f"signals={list(tr.signals)}")
            m = measure.extract_metrics(tr)
            check("指标非空", len(m) > 0, f"metrics={m}")
            # RC=1ms：v(out) 终值应接近 5V，均值明显 >0
            out_arr = next((v for k, v in tr.signals.items() if "out" in k.lower()), None)
            if out_arr is not None:
                check("终值≈5V (RC充电)", abs(float(out_arr[-1]) - 5.0) < 0.15,
                      f"final={float(out_arr[-1]):.3f}")
            png = render_wave(tr, tmp / "wave.png", title="RC charging")
            check("波形 PNG 生成", png.exists() and png.stat().st_size > 5000,
                  f"size={png.stat().st_size if png.exists() else 0}")

        # ---- 3. 坏网表：能拿到报错给 LLM ----
        bad = run_netlist(BAD_NETLIST, workdir=tmp / "bad")
        check("坏网表仿真失败", not bad.ok)
        check("报错摘要非空", len(bad.error_snippet.strip()) > 0,
              f"snippet={bad.error_snippet[:150]}")

        # ---- 3b. .ac 频扫：频率轴解析 + -3dB 点实测 ----
        ac = run_netlist(AC_NETLIST, workdir=tmp / "ac")
        check("AC 仿真通过", ac.ok)
        if ac.raw_path:
            tr_ac = measure.load_traces(ac.raw_path)
            check("AC 解析到频率轴", tr_ac.time is not None and len(tr_ac.time) > 50)
            vout = next((v for k, v in tr_ac.signals.items() if "out" in k.lower()), None)
            if vout is not None and tr_ac.time is not None:
                # -3dB 点：|v(out)| 首次跌破 0.707 的频率，应 ≈1kHz
                idx = int(np.argmax(vout < 0.7071))
                fc = float(tr_ac.time[idx])
                check("AC -3dB 截止频率≈1kHz", 700 < fc < 1400, f"fc={fc:.0f}Hz")

        # ---- 3c. .op 工作点：分压 ≈ 5V ----
        opsim = run_netlist(OP_NETLIST, workdir=tmp / "op")
        check("OP 仿真通过", opsim.ok)
        if opsim.raw_path:
            tr_op = measure.load_traces(opsim.raw_path)
            vout = next((v for k, v in tr_op.signals.items() if "out" in k.lower()), None)
            check("分压输出≈5V", vout is not None and abs(float(vout[0]) - 5.0) < 0.05,
                  f"v={None if vout is None else float(vout[0]):.3f}")

        # ---- 3d. 自定义 raw 文件名（LLM 常不守 out.raw 约名）也能被发现 ----
        renamed = run_netlist(AC_NETLIST.replace("out.raw", "my_lpf.raw"), workdir=tmp / "renamed")
        check("自定义 raw 名被发现", renamed.ok and renamed.raw_path is not None
              and renamed.raw_path.name == "my_lpf.raw", str(renamed.raw_path))

        # ---- 4. 静态检查四类规则 ----
        c1 = checks.run_checks("V1 in x0 DC 5\nR1 in out 1k\nC1 out x0 1u\n.tran 1u 1m\n")
        check("检查器: 缺节点0 报错", any("参考地" in e for e in c1.errors), str(c1.errors))

        c2 = checks.run_checks("V1 in 0 DC 5\nR1 in mid 1k\nR2 mid flt 2k\n.tran 1u 1m\n")
        check("检查器: 浮空节点报错", any("浮空" in e for e in c2.errors), str(c2.errors))

        c3 = checks.run_checks("V1 in 0 DC 5\nR1 in out 1M\nC1 out 0 1u\n.tran 1u 1m\n")
        check("检查器: 1M 毫兆歧义报错", any("M/m" in e or "Meg" in e for e in c3.errors), str(c3.errors))

        c4 = checks.run_checks("V1 in 0 DC 5\nQ1 c b e mynpn\nRc vcc c 1k\n.model npn1 npn\n.tran 1u 1m\n")
        check("检查器: 缺模型报错", any("模型" in e for e in c4.errors), str(c4.errors))

        c5 = checks.run_checks(RC_NETLIST)
        check("检查器: 好网表零误报", c5.ok, str(c5.errors))

        # ---- 5. extract_code_block ----
        check("code block 提取",
              llm.extract_code_block("说明\n```spice\n* t\nV1 1 0 5\n```\n尾") == "* t\nV1 1 0 5")
        check("无 code block 原样返回", llm.extract_code_block("* plain") == "* plain")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*40}\n{'全部通过' if not FAILURES else '失败: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
