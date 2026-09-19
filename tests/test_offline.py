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

SIN_NETLIST = """\
* 1kHz sine through divider (symbol/frequency regression)
V1 in 0 SIN(0 2.5 1k)
R1 in out 1k
R2 out 0 1k
.control
set filetype=ascii
tran 5u 10m
write out.raw v(out)
.endc
.end
"""

DUP_NETLIST = """\
* op with duplicate v(in) -> forces fallback parser; negative DC
V1 in 0 DC -5
R1 in out 1k
R2 out 0 1k
.control
set filetype=ascii
op
write out.raw v(in) v(out)
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

        # ---- 3e. 瞬态符号回归：负半周不许被翻正（np.abs 老 bug）----
        sim_sin = run_netlist(SIN_NETLIST, workdir=tmp / "sin")
        check("正弦仿真通过", sim_sin.ok)
        if sim_sin.raw_path:
            tr_sin = measure.load_traces(sim_sin.raw_path)
            vout = tr_sin.signals.get("v(out)")
            check("负半周保留（min<0）", vout is not None and float(np.min(vout)) < -1.0,
                  f"min={None if vout is None else float(np.min(vout)):.3f}")
            check("均值≈0（全波整流老 bug 会得到 0.8）",
                  vout is not None and abs(float(np.mean(vout))) < 0.05,
                  f"mean={None if vout is None else float(np.mean(vout)):.3f}")
            check("vpp≈2.5V（分压一半）", vout is not None
                  and abs(float(np.ptp(vout)) - 2.5) < 0.1,
                  f"vpp={None if vout is None else float(np.ptp(vout)):.3f}")
            m_sin = measure.extract_metrics(tr_sin)
            check("主频实测≈1kHz（重采样+抛物线细化）",
                  abs(m_sin.get("v(out)_freq_Hz", 0) - 1000) < 20,
                  f"freq={m_sin.get('v(out)_freq_Hz')}")

        # ---- 3f. fallback 解析器：重复变量触发降级 + 负直流符号 ----
        sim_dup = run_netlist(DUP_NETLIST, workdir=tmp / "dup")
        check("重复变量网表仿真通过", sim_dup.ok)
        if sim_dup.raw_path:
            tr_dup = measure.load_traces(sim_dup.raw_path)
            check("降级原因已记录（不再静默）", tr_dup.warning is not None and "spyci" in tr_dup.warning,
                  str(tr_dup.warning))
            vd = tr_dup.signals.get("v(out)")
            check("fallback 保留符号（-5V 分压≈-2.5V）",
                  vd is not None and abs(float(vd[0]) + 2.5) < 0.05,
                  f"v={None if vd is None else float(vd[0]):.3f}")

        # ---- 4. 静态检查四类规则 ----
        c1 = checks.run_checks("V1 in x0 DC 5\nR1 in out 1k\nC1 out x0 1u\n.tran 1u 1m\n")
        check("检查器: 缺节点0 报错", any("参考地" in e for e in c1.errors), str(c1.errors))

        c2 = checks.run_checks("V1 in 0 DC 5\nR1 in mid 1k\nR2 mid flt 2k\n.tran 1u 1m\n")
        check("检查器: 浮空节点报错", any("浮空" in e for e in c2.errors), str(c2.errors))

        c3 = checks.run_checks("V1 in 0 DC 5\nR1 in out 1M\nC1 out 0 1u\n.tran 1u 1m\n")
        check("检查器: 1M 毫兆歧义报错", any("M/m" in e or "Meg" in e for e in c3.errors), str(c3.errors))

        c4 = checks.run_checks("V1 in 0 DC 5\nQ1 c b e mynpn\nRc vcc c 1k\n.model npn1 npn\n.tran 1u 1m\n")
        check("检查器: 缺模型报错", any("模型" in e for e in c4.errors), str(c4.errors))

        # 2N2222 等数字开头型号是合法模型名，不能误报（曾导致 ce-amplifier 修不掉）
        c4b = checks.run_checks(
            "V1 in 0 DC 5\nRc in 2 10k\nRb1 in 3 100k\nRb2 3 0 20k\n"
            "Q1 2 3 4 2N2222\nRe 4 0 100\n.model 2N2222 NPN\n.tran 1u 1m\n")
        check("检查器: 2N2222 模型名零误报", c4b.ok, str(c4b.errors))

        # .subckt 体内部节点是局部作用域，不许当顶层浮空误报（exp1 曾卡死 4 轮）
        c6 = checks.run_checks(
            "* top\n"
            "X1 a mid mysub\nR1 a mid 1k\nR2 mid 0 2k\n"
            ".subckt mysub p q\nRin p qq 1k\nRload qq 0 2k\nRout q qq 3k\n.ends\n"
            ".model d1 D\n.tran 1u 1m\n")
        check("检查器: .subckt 体零误报", c6.ok, str(c6.errors))

        c5 = checks.run_checks(RC_NETLIST)
        check("检查器: 好网表零误报", c5.ok, str(c5.errors))

        # ---- 5. extract_code_block ----
        check("code block 提取",
              llm.extract_code_block("说明\n```spice\n* t\nV1 1 0 5\n```\n尾") == "* t\nV1 1 0 5")
        check("无 code block 原样返回", llm.extract_code_block("* plain") == "* plain")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

        # ---- 6. SKiDL 双轨构建器 ----
        from app.skidl_builder import build_netlist as skidl_build

        SKIDL_OK = """\
from skidl import generate_netlist
from skidl.pyspice import R, C, V, gnd, Net
inp, out = Net("IN"), Net("OUT")
v1 = V(value="AC 1")
r1 = R(value="1.59k")
c1 = C(value="100n")
inp += v1[1], r1[1]
out += r1[2], c1[1]
gnd += v1[2], c1[2]
generate_netlist()
print("ANALYSIS: ac dec 20 10 100k")
print("OUT_NODES: v(OUT)")
"""
        nl, err = skidl_build(SKIDL_OK)
        check("skidl: 构建成功", nl is not None, err)
        if nl:
            check("skidl: 拼接 .control/write/.end",
                  "set filetype=ascii" in nl and "write out.raw v(OUT)" in nl and nl.strip().endswith(".end"))
            sim_sk = run_netlist(nl, workdir=tmp / "skidl")
            check("skidl: 产出的网表能过 ngspice 且有 raw",
                  sim_sk.ok and sim_sk.raw_path is not None, (sim_sk.log or "")[-200:])
            if sim_sk.raw_path:
                tr_sk = measure.load_traces(sim_sk.raw_path)
                check("skidl: 解析到 v(OUT)", any("out" in k.lower() for k in tr_sk.signals))
        nl_bad, _err = skidl_build("import os\nos.system('echo hi')")
        check("skidl: import os 被拒", nl_bad is None)

        print(f"\n{'='*40}\n{'全部通过' if not FAILURES else '失败: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
