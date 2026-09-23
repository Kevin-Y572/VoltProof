"""离线冒烟测试：不依赖 LLM，验证仿真器/解析/绘图/检查器链路。

运行：python tests/test_offline.py   （零依赖自写断言，不引入 pytest）
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ngspice 须在 PATH 中，或设 VOLTPROOF_NGSPICE 指向 ngspice.exe（见 README）
import os

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

        # ---- 5b. LLM 封装层（DeepSeek 官方文档行为，stub 客户端，零网络） ----
        from types import SimpleNamespace as NS

        import httpx
        import openai as openai_mod

        def _mk_client(captured, resp=None, err=None):
            def create(**kw):
                captured.update(kw)
                if err is not None:
                    raise err
                return resp
            return NS(chat=NS(completions=NS(create=create)))

        def _usage_ns():
            return NS(prompt_tokens=100, completion_tokens=7, total_tokens=107,
                      prompt_cache_hit_tokens=64, prompt_cache_miss_tokens=36,
                      completion_tokens_details=NS(reasoning_tokens=1024))

        old_client = llm._client
        cap: dict = {}
        try:
            resp_ok = NS(choices=[NS(finish_reason="stop",
                                     message=NS(content="网表内容", reasoning_content=""))],
                         usage=_usage_ns())
            llm._client = _mk_client(cap, resp=resp_ok)

            # 思考默认开启（官方默认）：thinking=enabled 且不下发 temperature
            out = llm.chat("sys", "usr")
            check("llm: 默认思考开启且不下发 temperature",
                  out == "网表内容"
                  and cap["extra_body"] == {"thinking": {"type": "enabled"}}
                  and "temperature" not in cap, str(cap.get("extra_body")))
            check("llm: usage 统计（KV 缓存命中 + 思考 token）",
                  llm.last_usage.get("prompt_cache_hit_tokens") == 64
                  and llm.last_usage.get("prompt_cache_miss_tokens") == 36
                  and llm.last_usage.get("reasoning_tokens") == 1024)

            # 关思考：temperature 真正下发（官方：思考模式下被静默忽略）
            llm.chat("sys", "usr", temperature=0.5, thinking=False)
            check("llm: 关思考时下发 temperature",
                  cap["temperature"] == 0.5
                  and cap["extra_body"]["thinking"]["type"] == "disabled")

            # reasoning_effort 官方别名映射（minimal→low / medium→high）
            llm.chat("sys", "usr", reasoning_effort="medium")
            check("llm: reasoning_effort 别名归一 medium->high",
                  cap.get("reasoning_effort") == "high")
            try:
                llm.chat("sys", "usr", reasoning_effort="ultra")
                check("llm: 非法 effort 被拒", False)
            except ValueError:
                check("llm: 非法 effort 被拒", True)

            # user_id 官方格式校验（限速隔离用，[A-Za-z0-9_-]{1,512}）
            try:
                llm.chat("sys", "usr", user_id="带空格的 id")
                check("llm: 非法 user_id 被拒", False)
            except ValueError:
                check("llm: 非法 user_id 被拒", True)
            llm.chat("sys", "usr", user_id="voltproof_01")
            check("llm: 合法 user_id 经 extra_body 下发",
                  cap["extra_body"].get("user_id") == "voltproof_01")

            # 空 content 回落含代码块的 reasoning_content（推理模型实测行为）
            llm._client = _mk_client(cap, resp=NS(
                choices=[NS(finish_reason="stop",
                            message=NS(content="", reasoning_content="想过了```V1 1 0 5```"))],
                usage=None))
            check("llm: 空 content 回落含代码块的 reasoning",
                  llm.chat("sys", "usr") == "想过了```V1 1 0 5```")

            # 官方错误码 -> 中文解释（402 余额不足 / 429 并发超限）
            def _status_err(code, msg):
                r = httpx.Response(code, request=httpx.Request(
                    "POST", "https://api.deepseek.com/chat/completions"))
                return openai_mod.APIStatusError(msg, response=r, body=None)

            llm._client = _mk_client(cap, err=_status_err(402, "Insufficient Balance"))
            try:
                llm.chat("sys", "usr")
                check("llm: 402 报 LLMError 并提示余额", False)
            except llm.LLMError as e:
                check("llm: 402 报 LLMError 并提示余额", "余额" in str(e), str(e))
            llm._client = _mk_client(cap, err=_status_err(429, "Concurrency Limit"))
            try:
                llm.chat("sys", "usr")
                check("llm: 429 提示并发超限", False)
            except llm.LLMError as e:
                check("llm: 429 提示并发超限", "并发" in str(e), str(e))

            # JSON 模式（官方指南：须含 json 字样 / 截断报错 / 空 content 重试）
            llm._client = _mk_client(cap, resp=NS(
                choices=[NS(finish_reason="stop",
                            message=NS(content='{"fc_hz": 1000}', reasoning_content=""))],
                usage=None))
            check("llm: JSON 模式解析成功",
                  llm.chat_json("输出 json", '求截止频率，示例 {"fc_hz": 1000}')
                  == {"fc_hz": 1000})

            llm._client = _mk_client(cap, resp=NS(
                choices=[NS(finish_reason="stop",
                            message=NS(content='{"fc_hz": 1000}', reasoning_content=""))],
                usage=None))
            llm.chat_json("系统提示", "用户问题（完全没提那个词）")
            check("llm: prompt 缺 json 字样时自动补",
                  "json" in cap["messages"][1]["content"].lower()
                  and cap.get("response_format") == {"type": "json_object"})

            llm._client = _mk_client(cap, resp=NS(
                choices=[NS(finish_reason="length",
                            message=NS(content='{"fc_hz": 10', reasoning_content=""))],
                usage=None))
            try:
                llm.chat_json("输出 json", '求 {"fc_hz": 1000}')
                check("llm: 截断报截断错（finish_reason=length）", False)
            except llm.LLMError as e:
                check("llm: 截断报截断错（finish_reason=length）", "截断" in str(e), str(e))

            seq = {"n": 0}

            def _flaky_json(**kw):
                seq["n"] += 1
                content = "" if seq["n"] == 1 else '{"ok": 1}'
                return NS(choices=[NS(finish_reason="stop",
                                      message=NS(content=content, reasoning_content=""))],
                          usage=None)

            llm._client = NS(chat=NS(completions=NS(create=_flaky_json)))
            check("llm: JSON 空 content 自动重试一次",
                  llm.chat_json("输出 json", "x") == {"ok": 1} and seq["n"] == 2)

            # 流式：reasoning 与 content 分流累计（官方 delta 分流示例）+ 末块 usage
            def _stream_create(**kw):
                cap.update(kw)
                return iter([
                    NS(choices=[], usage=None),
                    NS(choices=[NS(finish_reason=None,
                                   delta=NS(content=None, reasoning_content="思考中"))],
                       usage=None),
                    NS(choices=[NS(finish_reason=None,
                                   delta=NS(content="你好", reasoning_content=None))],
                       usage=None),
                    NS(choices=[NS(finish_reason="stop",
                                   delta=NS(content=None, reasoning_content=None))],
                       usage=_usage_ns()),
                ])

            llm._client = NS(chat=NS(completions=NS(create=_stream_create)))
            got = list(llm.chat_stream("s", "u", yield_reasoning=True))
            text = "".join(t for k, t in got if k == "content")
            reasoning = "".join(t for k, t in got if k == "reasoning")
            check("llm: 流式 content/reasoning 分流累计",
                  text == "你好" and reasoning == "思考中"
                  and cap.get("stream") is True
                  and cap.get("stream_options") == {"include_usage": True},
                  str(got))
            check("llm: 流式 usage 取自末块",
                  llm.last_usage.get("prompt_cache_hit_tokens") == 64)
        finally:
            llm._client = old_client

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

        # ---- 7. 确定性原理图布局（graphviz dot + schemdraw 按位渲染）----
        from app.schematic_layout import parse_to_graph, render_netlist_schematic

        g = parse_to_graph(RC_NETLIST)
        check("布局: RC 网表解析 3 元件", len(g) == 3 and g[0].name == "V1"
              and g[1].nets == ["in", "out"], str([(e.name, e.nets) for e in g]))
        for nm, nl in [("rc", RC_NETLIST), ("op", OP_NETLIST)]:
            png = render_netlist_schematic(nl, tmp / f"sch_{nm}.png", title=nm)
            check(f"布局: {nm} 渲染出有内容的图",
                  png is not None and png.exists() and png.stat().st_size > 8000,
                  str(png))
        EXP3_NL = ("* synth\nV1 n1 0 DC 0 SIN(0 1 1000)\nV2 n2 0 DC 0 SIN(0 0.33 3000)\n"
                   "R1 n1 sum 10k\nR2 n2 sum 30k\nRf sum out 10k\n"
                   "X1 sum 0 out vcc vee opamp\nVcc vcc 0 12\n"
                   ".subckt opamp a b c p m\nE1 c 0 a b 100000\n.ends\n.end\n")
        png3 = render_netlist_schematic(EXP3_NL, tmp / "sch_exp3.png", title="exp3")
        check("布局: 多端元件(X)+子电路 网表渲染成功", png3 is not None and png3.exists())

        # ---- 8. 程序化数值调参（tune_hint → 确定性改源幅度/频率）----
        from app.tuner import _spice_val, numeric_tune

        NL2 = ("* synth\nV1 a 0 SIN(0 1 1000)\nV2 b 0 SIN(0 0.33 3000)\n"
               "R1 a sum 10k\nR2 b sum 30k\n.end\n")
        tuned, note = numeric_tune(NL2, [
            {"kind": "vpp", "measured": 2.5, "target": 5.0, "freq": 1000},
            {"kind": "freq", "measured": 950.0, "target": 1000.0, "freq": 950.0},
        ])
        check("tuner: 返回新网表", tuned is not None, note)
        if tuned:
            l1 = [ln for ln in tuned.splitlines() if ln.startswith("V1")][0]
            l2 = [ln for ln in tuned.splitlines() if ln.startswith("V2")][0]
            check("tuner: 1kHz 源幅度按比例缩放", "SIN(0 2 " in l1, l1)
            check("tuner: 偏差源频率被校正（3000 源不动）", "3000" in l2 or "3157" in l2, l2)
        tuned2, _ = numeric_tune("* osc\nVcc vcc 0 12\nQ1 a b c m\n.model m NPN\n.end\n",
                                 [{"kind": "vpp", "measured": 3, "target": 6, "freq": 1000}])
        check("tuner: 无匹配 SIN 源时返回 None（回退 LLM）", tuned2 is None)

        # 谐波比调参：加法器 R_h 与谐波比成反比
        SYN = ("* synth\nV1 n1 0 SIN(0 1 1000)\nV3 n3 0 SIN(0 1 3000)\n"
               "R1 n1 sum 10k\nR3 n3 sum 200k\nRf sum out 10k\n.end\n")
        tuned3, note3 = numeric_tune(SYN, [{"kind": "harmonic", "measured": 0.05,
                                            "target": 1 / 3, "signal": "sum", "freq": 3000}])
        check("tuner: 谐波 hint 定位加权电阻", tuned3 is not None, note3)
        if tuned3:
            r3_line = [ln for ln in tuned3.splitlines() if ln.startswith("R3")][0]
            new_r3 = _spice_val(r3_line.split()[3])
            check("tuner: R3 按反比缩放（200k×0.15=30k）", abs(new_r3 - 30000) < 1, r3_line)
            r1_line = [ln for ln in tuned3.splitlines() if ln.startswith("R1")][0]
            check("tuner: 基波电阻不动", "10k" in r1_line, r1_line)

        # ---- 9. 安全过滤（两个实测 PoC 的回归）----
        from app.ngspice_runner import check_netlist_safety
        from app.sandbox import ast_check, minimal_env

        shell_nl = ("* evil\nV1 a 0 1\nR1 a 0 1k\n.control\n"
                    "shell echo PWNED > C:/pwned.txt\nop\n.endc\n.end")
        check("安全: .control shell 命令被拒绝", any("系统命令" in p for p in check_netlist_safety(shell_nl)))
        inc_nl = "* x\n.include C:/Users/secret.txt\nV1 a 0 1\n.end\n"
        check("安全: 绝对路径 .include 被拒绝", any("路径" in p for p in check_netlist_safety(inc_nl)))
        check("安全: ../ 相对 .include 放行（ngspice 惯用法）",
              not check_netlist_safety("* x\n.include ../adder_common.inc\nV1 a 0 1\n.end\n"))
        check("安全: quit 放行（无副作用）",
              not check_netlist_safety("* x\nV1 a 0 1\n.control\nop\nquit\n.endc\n.end\n"))
        # 外部程序调用：gnuplot/edit 会在服务器桌面弹 GUI（官方示例 combplot.cir 实测）
        gp_nl = ("* gp\nV1 a 0 1\n.control\ngnuplot gpout1 v(a)\nop\n.endc\n.end")
        check("安全: .control gnuplot 被拒绝",
              any("外部程序" in p for p in check_netlist_safety(gp_nl)))
        check("安全: .control edit 被拒绝",
              any("外部程序" in p
                  for p in check_netlist_safety("* e\nV1 a 0 1\n.control\nedit\n.endc\n.end")))
        check("安全: 正常网表零误报", check_netlist_safety(RC_NETLIST) == [])

        ESCAPE = ('cw = [c for c in (1).__class__.__base__.__subclasses__() '
                  'if c.__name__ == "catch_warnings"][0]')
        check("安全: dunder 逃逸链被 AST 拦截",
              ast_check("import schemdraw\n" + ESCAPE, {"schemdraw"}) is False)
        check("安全: 正常 schemdraw 代码放行",
              ast_check("import schemdraw\nimport schemdraw.elements as elm\nd = schemdraw.Drawing()",
                        {"schemdraw", "schemdraw.elements", "math"}) is True)
        env = minimal_env()
        check("安全: 沙箱环境不含任何凭据",
              not any(("KEY" in k or "SECRET" in k or "TOKEN" in k) for k in env))

        # ---- 10. Traces.analysis 字段（AC 数据被当 tran 判分曾致 sensor 假失败）----
        sim_ac2 = run_netlist(AC_NETLIST, workdir=tmp / "an_ac")
        if sim_ac2.raw_path:
            check("analysis: ac 网表识别为 ac",
                  measure.load_traces(sim_ac2.raw_path).analysis == "ac")
        sim_tr2 = run_netlist(RC_NETLIST, workdir=tmp / "an_tran")
        if sim_tr2.raw_path:
            check("analysis: tran 网表识别为 tran",
                  measure.load_traces(sim_tr2.raw_path).analysis == "tran")
        sim_op2 = run_netlist(OP_NETLIST, workdir=tmp / "an_op")
        if sim_op2.raw_path:
            check("analysis: op 网表识别为 op",
                  measure.load_traces(sim_op2.raw_path).analysis == "op")

        # ---- 11. 起振/限幅确定性修复（bench 两失败任务的闭环）----
        OSC_NL = ("* square osc 1k (relaxation)\nV1 vcc 0 DC 12\n"
                  "Rf out in 4.55k\nC1 in 0 100n\nR1 out fb 10k\nR2 fb 0 10k\n"
                  "X1 fb in out opamp\n.subckt opamp a b c\nE1 c 0 a b 100000\n.ends\n"
                  ".control\nset filetype=ascii\ntran 5u 10m\n"
                  "write out.raw v(out) v(in)\n.endc\n.end\n")
        tuned_up, note_up = numeric_tune(OSC_NL, [{"kind": "startup", "message": ""}])
        check("tuner: startup hint 插入 .ic", tuned_up is not None and ".ic v(in)=" in tuned_up,
              note_up)
        if tuned_up:
            ic_idx = next(i for i, ln in enumerate(tuned_up.splitlines())
                          if ln.startswith(".ic"))
            ctrl_idx = next(i for i, ln in enumerate(tuned_up.splitlines())
                            if ln.strip() == ".control")
            check("tuner: .ic 插在 .control 之前", ic_idx < ctrl_idx)
        tuned_up2, _ = numeric_tune(tuned_up or OSC_NL, [{"kind": "startup", "message": ""}])
        check("tuner: 已有 .ic 时不再重复插", tuned_up2 is None)

        SUM_NL = ("* summer\nVcc vcc 0 DC 15\nV1 a 0 DC 1\nE1 out 0 a 0 1\nE2 mid 0 a 0 100000\n"
                  "R1 out 0 1meg\nR2 mid 0 1meg\n.end\n")
        tuned_cl, note_cl = numeric_tune(SUM_NL, [{"kind": "clamp", "message": ""}])
        check("tuner: clamp 只改高增益 E 源（增益1求和源不动）",
              tuned_cl is not None
              and "E1 out 0 a 0 1" in tuned_cl
              and "max(-14.5, min(100000*v(a,0), 14.5))" in tuned_cl,
              (note_cl, tuned_cl))
        # 端到端：对称不振 → .ic 起振 → 幅度上天 → min/max 钳位 → ~1kHz 方波
        sim_o1 = run_netlist(OSC_NL, workdir=tmp / "osc")
        check("osc: 原网表仿真通过（静态亚稳）", sim_o1.ok)
        from app.measure import dominant_freq
        tr_o1 = measure.load_traces(sim_o1.raw_path)
        vo1 = next(v for k, v in tr_o1.signals.items() if "out" in k.lower())
        check("osc: 对称网表判非周期（未起振）", dominant_freq(tr_o1.time, vo1) is None)
        sim_o2 = run_netlist(tuned_up, workdir=tmp / "osc")
        tr_o2 = measure.load_traces(sim_o2.raw_path)
        vo2 = next(v for k, v in tr_o2.signals.items() if "out" in k.lower())
        dom2 = dominant_freq(tr_o2.time, vo2)
        check("osc: .ic 后起振", dom2 is not None)
        check("osc: 无钳位时幅度上天（>1e6V，触发 clamp 的场景）",
              float(np.ptp(vo2)) > 1e6, f"vpp={float(np.ptp(vo2)):.3g}")
        tuned_cl2, _ = numeric_tune(tuned_up, [{"kind": "clamp", "message": ""}])
        sim_o3 = run_netlist(tuned_cl2, workdir=tmp / "osc")
        tr_o3 = measure.load_traces(sim_o3.raw_path)
        vo3 = next(v for k, v in tr_o3.signals.items() if "out" in k.lower())
        dom3 = dominant_freq(tr_o3.time, vo3)
        check("osc: 钳位后 vpp≈23V（±11.5 轨）", abs(float(np.ptp(vo3)) - 23.0) < 0.5,
              f"vpp={float(np.ptp(vo3)):.3g}")
        check("osc: 钳位后频率≈1kHz", dom3 is not None and abs(dom3[0] - 1000) < 60,
              f"f={None if dom3 is None else dom3[0]:.0f}")

        # ---- 12. 兼容层：多 plot raw + CIDER 静态拦截 + 超时报错透出 ----
        from app.measure import _fallback_parse, split_plot_sections

        mp = tmp / "multiplot.raw"
        mp.write_text(
            "Title: demo\nPlotname: Transient Analysis\nFlags: real\n"
            "No. Variables: 2\nNo. Points: 2\nVariables:\n\t0\ttime\ttime\n\t1\tv(out)\tvoltage\n"
            "Values:\n0 0.0\n\t0.0\n1 0.001\n\t9.9\n"
            "Title: demo\nPlotname: AC Analysis\nFlags: complex\n"
            "No. Variables: 2\nNo. Points: 2\nVariables:\n\t0\tfrequency\tfrequency\n\t1\tv(out)\tvoltage\n"
            "Values:\n0 10\n\t1.0,0.5\n1 100\n\t0.5,0.2\n")
        secs = split_plot_sections(mp.read_text())
        check("多plot: 按 Title 切出 2 段", len(secs) == 2)
        tr_mp = measure.load_traces(mp)
        check("多plot: 取末段(AC)且警告带段名",
              tr_mp.analysis == "ac"
              and tr_mp.warning is not None and "2 个 plot" in tr_mp.warning
              and "AC Analysis" in tr_mp.warning, str(tr_mp.warning))
        check("多plot: 数据是末段的（|1+0.5j|≈1.118 开头）",
              abs(float(tr_mp.signals["v(out)"][0]) - 1.118) < 0.01
              and len(tr_mp.signals["v(out)"]) == 2)
        tr_mp2 = _fallback_parse(mp)
        check("多plot: fallback 分段后不再搅拌（老实现产出混杂数据）",
              len(tr_mp2.signals.get("v(out)", [])) == 2
              and abs(float(tr_mp2.signals["v(out)"][0]) - 1.118) < 0.01,
              str(tr_mp2.signals))

        cc = checks.run_checks("* c\nQ1 c b e mq\n.model mq nbjt level=2\n.tran 1u 1m\nV1 b 0 0\n")
        check("兼容: CIDER NBJT 模型被拦截并给出改法",
              any("CIDER" in e and "compact" in e for e in cc.errors), str(cc.errors))
        cc2 = checks.run_checks("* c\nQ1 c b e mq\n.model mq npn\n.tran 1u 1m\nV1 b 0 0\n")
        cc3 = checks.run_checks("* m\nM1 d g s b mm\n.model mm nmos\n.tran 1u 1m\nV1 g 0 0\n")
        check("兼容: 标准 NPN/NMOS 不被误伤",
              not any("CIDER" in e for e in cc2.errors + cc3.errors))

        # 超时报错透出：大步数非线性瞬态 + 缩短超时（确定性慢，不依赖具体电路挂死）
        from app import ngspice_runner

        _old_to = ngspice_runner._TIMEOUT
        try:
            ngspice_runner._TIMEOUT = 2
            t0 = time.monotonic()
            sim_to = run_netlist(
                "* long nonlinear tran\nV1 in 0 SIN(0 5 1k)\nD1 in mid d1\n"
                "R1 mid out 1k\nC1 out 0 10n\n.model d1 D\n"
                ".control\nset filetype=ascii\ntran 10u 10\nwrite out.raw v(out)\n.endc\n.end\n",
                workdir=tmp / "hang")
            check("兼容: 超时网表按上限被杀且报错点明原因",
                  not sim_to.ok and "超时" in sim_to.error_snippet
                  and "收敛死循环" in sim_to.error_snippet
                  and time.monotonic() - t0 < 15,
                  sim_to.error_snippet[:120])
        finally:
            ngspice_runner._TIMEOUT = _old_to

        # ---- 工作区（Workspace：路径守卫 / 任务清单 / 会话持久化）----
        from app.workspace import Workspace, WorkspaceError, default_workspace

        def _perm(fn) -> bool:
            try:
                fn()
                return False
            except PermissionError:
                return True

        wsroot = (tmp / "ws1").resolve()
        wsroot.mkdir()
        ws = Workspace(wsroot)
        try:
            Workspace("relative/path")
            rel_bad = False
        except WorkspaceError:
            rel_bad = True
        check("工作区: 相对路径被拒", rel_bad)
        check("工作区: 正常 resolve 返回区内绝对路径",
              ws.resolve("docs/a.txt") == (wsroot / "docs" / "a.txt").resolve())
        check("工作区: .. 逃逸被拒", _perm(lambda: ws.resolve("docs/../../evil.txt")))
        check("工作区: 区外绝对路径被拒",
              _perm(lambda: ws.resolve(wsroot.parent / "evil.txt")))
        check("工作区: 恶意 task_id 被守卫拦截",
              _perm(lambda: ws.task_sim_dir("../../evil")))
        tid = ws.new_task_id()
        check("工作区: task_id 含时间戳且唯一", "-" in tid and tid != ws.new_task_id())
        d = ws.task_docs_dir(tid)
        (d / "evidence.json").write_text(
            '{"ok": true, "request": "r", "metrics": {"a": 1}}', encoding="utf-8")
        (d / "wave.png").write_bytes(b"png")
        tasks = ws.list_tasks()
        check("工作区: list_tasks 读到任务与文件清单",
              len(tasks) == 1 and tasks[0]["task_id"] == tid and tasks[0]["ok"] is True
              and tasks[0]["files"] == ["evidence.json", "wave.png"], str(tasks))
        ws.save_sessions({"s1": {"id": "s1", "netlist": "N", "history": []}})
        check("工作区: 会话状态落盘可往返",
              Workspace(wsroot).load_sessions().get("s1", {}).get("netlist") == "N")
        check("工作区: 缺省工作区指向仓库 out/",
              str(default_workspace().root).endswith("out")
              and str(default_workspace().root.parent).endswith("voltproof"))

        # ---- LLM 供应商配置（前端可配 + SSRF 校验 + 多供应商方言）----
        def _llm_reject(url: str) -> str:
            try:
                llm.validate_base_url(url)
                return ""
            except llm.LLMError as e:
                return str(e)

        for bad in ("ftp://example.com/v1", "example.com/v1",
                    "http://localhost:11434/v1", "http://api.localhost/v1",
                    "http://127.0.0.1:8000/v1", "http://192.168.1.5/v1",
                    "http://10.0.0.1/v1", "http://169.254.169.254/v1",
                    "http://[::1]:8000/v1"):
            check(f"LLM配置: 拒绝 {bad}", bool(_llm_reject(bad)))

        # 测试占位 Key（运行时拼接，非任何真实凭据）
        _test_key = "sk-" + "dummy" * 8
        _old_llm = (llm._BASE_URL, llm._MODEL, llm._API_KEY, llm._client)
        _old_settings_env = os.environ.get("VOLTPROOF_SETTINGS")
        _old_priv = os.environ.get("VOLTPROOF_ALLOW_PRIVATE_LLM")
        _setf = tmp / "llm_settings.json"
        os.environ["VOLTPROOF_SETTINGS"] = str(_setf)
        try:
            # 豁免开关：机主显式放行本机模型服务（Ollama/LM Studio）
            os.environ["VOLTPROOF_ALLOW_PRIVATE_LLM"] = "1"
            check("LLM配置: 豁免开关放行本机地址", _llm_reject("http://localhost:11434/v1") == "")
            d = llm.configure(base_url="http://127.0.0.1:1234/v1/",
                              api_key=_test_key,
                              model="local-model-x")
            check("LLM配置: configure 生效且 Key 脱敏",
                  d["base_url"] == "http://127.0.0.1:1234/v1"
                  and d["model"] == "local-model-x"
                  and d["has_key"] is True
                  and _test_key not in str(d) and d.get("api_key") is None, str(d))
            check("LLM配置: 配置落盘且可回读",
                  _setf.exists() and llm.load_runtime_config().get("model") == "local-model-x")
            check("LLM配置: 客户端已重置待重建", llm._client is None)
            check("LLM配置: 非 DeepSeek 方言判定", llm._deepseek_dialect() is False)
            # 方言落到请求参数：非 DeepSeek 供应商不下发 thinking，temperature 恒可调
            cap2: dict = {}
            llm._client = NS(chat=NS(completions=NS(create=lambda **kw: cap2.update(kw) or NS(
                choices=[NS(finish_reason="stop", message=NS(content="ok", reasoning_content=""))],
                usage=None))))
            llm.chat("s", "u", temperature=0.7)
            check("LLM配置: 非 DeepSeek 供应商不下发 thinking 扩展",
                  "extra_body" not in cap2 and cap2.get("temperature") == 0.7,
                  str(cap2.get("extra_body")))
            # 非法 URL：先校验后改动，状态不被污染
            os.environ.pop("VOLTPROOF_ALLOW_PRIVATE_LLM", None)
            _before = llm._BASE_URL
            try:
                llm.configure(base_url="http://192.168.0.1/v1")
                rejected = False
            except llm.LLMError:
                rejected = True
            check("LLM配置: 非法 URL 拒绝且状态不变",
                  rejected and llm._BASE_URL == _before)
        finally:
            llm._BASE_URL, llm._MODEL, llm._API_KEY, llm._client = _old_llm
            if _old_settings_env is None:
                os.environ.pop("VOLTPROOF_SETTINGS", None)
            else:
                os.environ["VOLTPROOF_SETTINGS"] = _old_settings_env
            if _old_priv is None:
                os.environ.pop("VOLTPROOF_ALLOW_PRIVATE_LLM", None)
            else:
                os.environ["VOLTPROOF_ALLOW_PRIVATE_LLM"] = _old_priv
            _setf.unlink(missing_ok=True)

        print(f"\n{'='*40}\n{'全部通过' if not FAILURES else '失败: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
