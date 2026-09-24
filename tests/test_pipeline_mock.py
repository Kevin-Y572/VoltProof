"""管线与 FastAPI 接口的离线测试：monkeypatch 掉 llm.chat，不花一分钱。

覆盖管线与接口的核心逻辑：
  - 一次生成的网表直接通过 → 证据卡完整
  - 静态检查失败 → 报错回喂 → 自动修复（仿真在环循环）
  - 仿真失败（raw 缺失）→ 自动修复
  - 会话多轮：第二轮携带上一轮验证过的网表
  - POST /chat、POST /demo/raw、静态页托管

运行：python tests/test_pipeline_mock.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ngspice 须在 PATH 中，或设 VOLTPROOF_NGSPICE 指向 ngspice.exe（见 README）
# LLM 设置指向临时文件：测试绝不读写真实 settings.json（里面可能有用户 Key）
os.environ["VOLTPROOF_SETTINGS"] = str(
    Path(__file__).resolve().parent / f"_llm_settings_test_{os.getpid()}.json")

from unittest.mock import patch  # noqa: E402

from app import llm, pipeline, prompts  # noqa: E402

GOOD = """```spice
* RC lowpass for test
V1 in 0 AC 1
R1 in out 1.59k
C1 out 0 100n
.control
set filetype=ascii
ac dec 20 10 100k
write out.raw v(out)
.endc
.end
```"""

NO_GROUND = """```spice
* broken: no node 0
V1 in gndx AC 1
R1 in out 1.59k
C1 out gndx 100n
.control
set filetype=ascii
ac dec 20 10 100k
write out.raw v(out)
.endc
.end
```"""

NO_CONTROL = """```spice
* passes static checks but writes no raw file
V1 in 0 AC 1
R1 in out 1.59k
C1 out 0 100n
.ac dec 20 10 100k
.end
```"""

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class Scripted:
    """按脚本顺序返回响应，并记录每次调用的参数。"""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str, temperature: float = 0.2, **kw) -> str:
        self.calls.append((system, user))
        return self.responses.pop(0)


def main() -> int:
    # 清结果缓存：多个用例共用同一请求文本，不能互相吃到缓存
    # （缓存现在按工作区落在 out/.cp/cache）
    import shutil as _sh
    from pathlib import Path as _P
    ws_root = str(pipeline.default_workspace().root)

    def sess(sid: str) -> dict:
        return pipeline._SESSIONS[ws_root][sid]

    _sh.rmtree(pipeline.default_workspace().cache_dir, ignore_errors=True)

    # ---- 0. 领域守卫：数字 RTL 需求前置拒绝，零消耗 ----
    class _Boom:
        def __call__(self, *a, **kw):
            raise AssertionError("守卫请求不应调用 LLM")

    with patch.object(llm, "chat", _Boom()):
        ev = pipeline.run_pipeline(
            "请用可综合的 Verilog 实现以下模块。模块名：temp_alarm，端口：clk, rst_n, temp[7:0]")
    check("领域守卫: 前置拒绝且不调 LLM",
          ev.rejected is True and ev.ok is False, str(ev.retry_log))
    check("领域守卫: 零消耗（无网表/无波形/秒回）",
          ev.netlist == "" and ev.waveform_b64 is None and ev.elapsed < 1)
    check("领域守卫: 解读给出能力边界与改问建议",
          "模拟电路" in ev.interpretation and "Verilog" in ev.interpretation)
    check("领域守卫: 正常电路需求不误伤",
          pipeline._domain_guard("设计桥式整流+电容滤波电路，纹波<1V") is None
          and pipeline._domain_guard("用 FPGA 产生 1kHz 时钟") is not None)

    # ---- 1. 一次通过 ----
    with patch.object(llm, "chat", Scripted([GOOD, "解读：实测-3dB约1kHz。"])):
        ev = pipeline.run_pipeline("1kHz低通滤波器")
    check("一次通过: ok", ev.ok, str(ev.retry_log))
    check("一次通过: 有波形", ev.waveform_b64 is not None and len(ev.waveform_b64) > 1000)
    check("一次通过: 有指标", len(ev.metrics) > 0, str(ev.metrics))
    check("一次通过: 有解读", "1kHz" in ev.interpretation)
    check("一次通过: 无重试", len([r for r in ev.retry_log if r["stage"] != "generate"]) == 0)
    check("一次通过: 证据/产物落盘工作区任务目录",
          bool(ev.task_dir) and (Path(ev.task_dir) / "evidence.json").exists()
          and (Path(ev.task_dir) / "wave.png").exists(), str(ev.task_dir))

    # ---- 2. 静态检查失败 → 自动修复 ----
    sc = Scripted([NO_GROUND, GOOD, "解读：修复后通过。"])
    with patch.object(llm, "chat", sc):
        ev = pipeline.run_pipeline("1kHz低通滤波器-静态修复用例")
    check("静态修复: ok", ev.ok, str(ev.retry_log))
    stages = [r["stage"] for r in ev.retry_log]
    check("静态修复: 记录了 static-check 轮", "static-check" in stages, str(stages))
    check("静态修复: 修复提示包含报错", "参考地" in sc.calls[1][1], sc.calls[1][1][:120])

    # ---- 3. 仿真失败（无 raw）→ 自动修复 ----
    with patch.object(llm, "chat", Scripted([NO_CONTROL, GOOD, "解读：修复后通过。"])):
        ev = pipeline.run_pipeline("1kHz低通滤波器-仿真修复用例")
    check("仿真修复: ok", ev.ok, str(ev.retry_log))
    stages = [r["stage"] for r in ev.retry_log]
    check("仿真修复: 记录了 simulate 轮", "simulate" in stages, str(stages))

    # ---- 4. 会话多轮：携带上一轮网表 ----
    # 会话状态已持久化到工作区 state.json——内存与磁盘都要清，
    # 否则上一次测试运行的 sess-test 被水合回来，历史条数翻倍
    pipeline._SESSIONS.clear()
    pipeline.default_workspace().state_path.unlink(missing_ok=True)
    s1 = Scripted([GOOD, "第一轮解读。"])
    with patch.object(llm, "chat", s1):
        d1 = pipeline.chat_with_session("sess-test", "先做1kHz低通")
    check("会话: 第一轮返回 session_id", d1["session_id"] == "sess-test")
    s2 = Scripted([GOOD.replace("1.59k", "8k").replace("100n", "20n"), "第二轮解读。"])
    with patch.object(llm, "chat", s2):
        d2 = pipeline.chat_with_session("sess-test", "把截止频率降到1kHz左右重选参数")
    gen_user2 = s2.calls[0][1]
    check("会话: 第二轮携带上轮网表", "当前网表" in gen_user2 and "V1 in 0" in gen_user2, gen_user2[:100])
    check("会话: 历史两条", len(sess("sess-test")["history"]) == 2)
    check("会话: 状态落盘 state.json（重启可恢复）",
          (pipeline.default_workspace().state_path).exists()
          and "sess-test" in pipeline.default_workspace().load_sessions())

    # ---- 4b. 验收环：指标不达标 → 差距回喂 → 调参重跑 ----
    calls = {"n": 0}

    def strict_validator(ev, tr):
        calls["n"] += 1
        if calls["n"] == 1:  # 第一轮：幅度不达标
            return [{"name": "峰峰值≈2V", "ok": False, "detail": "实测 vpp=1.0V（目标 2V±15%）"}]
        if calls["n"] == 2:  # 第二轮：仍未达标（触发带历史的调参）
            return [{"name": "峰峰值≈2V", "ok": False, "detail": "实测 vpp=1.4V（目标 2V±15%）"}]
        return [{"name": "峰峰值≈2V", "ok": True, "detail": "实测 vpp=2.0V"}]

    tune_calls: list[str] = []

    def fake_chat(system, user, temperature=0.2, **kw):
        if "调参专家" in system:
            tune_calls.append(user)
            return GOOD.replace("AC 1", "AC 2")  # 调参后的网表
        if "解读" in system:
            return "验收环解读。"
        return GOOD

    with patch.object(llm, "chat", fake_chat):
        ev = pipeline.run_pipeline("1kHz低通", validators=[strict_validator])
    stages = [r["stage"] for r in ev.retry_log]
    check("验收环: 记录 verify 轮", "verify" in stages, str(stages))
    check("验收环: 调参提示词含差距详情",
          any("实测 vpp=1.0V" in u for u in tune_calls), str(tune_calls)[:150])
    check("验收环: 二次调参携带历史（避免来回摆动）",
          len(tune_calls) >= 2 and "调参历史" in tune_calls[1] and "vpp=1.0V" in tune_calls[1],
          tune_calls[1][:150] if len(tune_calls) > 1 else "(无第二次调参)")
    check("验收环: 最终 checks 全过", ev.ok and all(c["ok"] for c in ev.checks))
    check("验收环: Evidence 带验收明细", len(ev.checks) == 1 and ev.checks[0]["ok"])

    # ---- 4c. 附件网表：跳过生成直接进仿真（诊断场景） ----
    pipeline._SESSIONS.clear()
    pipeline.default_workspace().state_path.unlink(missing_ok=True)
    with patch.object(llm, "chat", Scripted(["附件电路解读：实测-3dB约1kHz。"])) as sc_att:
        d_att = pipeline.chat_with_session("att-1", "帮我仿真验证这个电路", attachment={
            "filename": "my.cir", "content": GOOD.replace("```spice\n", "").replace("\n```", "")})
    check("附件: 网表直接仿真通过", d_att["ok"] is True, str(d_att.get("retry_log")))
    check("附件: 零次生成调用（跳过 LLM 生成）", len(sc_att.calls) == 1, f"{len(sc_att.calls)} 次")
    check("附件: 网表进入会话状态", sess("att-1")["netlist"] is not None)
    check("附件: 文本附件并入需求",
          pipeline._looks_like_netlist("note.txt", "设计一个放大器") is False
          and pipeline._looks_like_netlist("a.cir", "任意") is True
          and pipeline._looks_like_netlist("x.txt", "* cir\n.tran 1u 1m") is True)

    # ---- 4d. 数值调参：tune_hint 走确定性路径，不调 LLM 调参 ----
    SIN_NL = """```spice
* sine test for numeric tuning
V1 in 0 SIN(0 1 1k)
R1 in out 1k
R2 out 0 1k
.control
set filetype=ascii
tran 5u 10m
write out.raw v(out)
.endc
.end
```"""
    calls2 = {"n": 0}

    def hint_validator(ev, tr):
        calls2["n"] += 1
        if calls2["n"] == 1:
            return [{"name": "幅度", "ok": False, "detail": "vpp=1.0V（目标 2V）",
                     "tune_hint": {"kind": "vpp", "measured": 1.0, "target": 2.0, "freq": 1000}}]
        return [{"name": "幅度", "ok": True, "detail": "vpp=2.0V"}]

    with patch.object(llm, "chat", Scripted([SIN_NL, "数值调参后解读。"])) as sc_num:
        ev_num = pipeline.run_pipeline("test", validators=[hint_validator])
    stages_num = [r["stage"] for r in ev_num.retry_log]
    check("数值调参: verify 轮存在", "verify" in stages_num, str(stages_num))
    check("数值调参: 零 LLM 调参调用（确定性路径，仅 1 次生成+1 次解读）",
          len(sc_num.calls) == 2, f"{len(sc_num.calls)} 次调用")
    check("数值调参: 网表 SIN 幅度被确定性修改",
          "SIN(0 2 " in ev_num.netlist, ev_num.netlist.splitlines()[1] if len(ev_num.netlist.splitlines()) > 1 else "")
    check("数值调参: 最终通过", ev_num.ok)

    # ---- 4e. 修复环重启：连续 2 次修复失败 → 重新生成（temperature 提高）----
    sc3 = Scripted([NO_GROUND, NO_GROUND, NO_GROUND, GOOD, "重启后解读。"])
    gen_calls: list[float] = []
    orig_chat = sc3

    class ScriptedT(Scripted):
        def __call__(self, system, user, temperature=0.2, **kw):
            if "电路设计专家" in system:  # GENERATE_SYSTEM
                gen_calls.append(temperature)
            return super().__call__(system, user, temperature)

    with patch.object(llm, "chat", ScriptedT([NO_GROUND, NO_GROUND, NO_GROUND, GOOD, "重启后解读。"])):
        ev3 = pipeline.run_pipeline("1kHz低通")
    stages3 = [r["stage"] for r in ev3.retry_log]
    check("重启: 第 3 次失败后触发 regenerate", "regenerate" in stages3, str(stages3))
    check("重启: 重生成用更高温度求多样性", any(t > 0.3 for t in gen_calls), str(gen_calls))
    check("重启: 最终通过", ev3.ok)

    # ---- 4f. 结果缓存：相同请求第二次零 LLM 调用 ----
    import shutil as _sh
    from pathlib import Path as _P
    _sh.rmtree(pipeline.default_workspace().cache_dir, ignore_errors=True)
    sc_c1 = Scripted([GOOD, "缓存测试解读。"])
    with patch.object(llm, "chat", sc_c1):
        pipeline.run_pipeline("缓存测试电路")
    sc_c2 = Scripted([])  # 空：任何调用都会 IndexError
    with patch.object(llm, "chat", sc_c2):
        ev_c2 = pipeline.run_pipeline("缓存测试电路")
    check("缓存: 第二次命中（零 LLM 调用）", ev_c2.ok and len(sc_c2.calls) == 0
          and any(r["stage"] == "cache" for r in ev_c2.retry_log),
          str([r["stage"] for r in ev_c2.retry_log]))
    check("缓存: 证据完整（波形/指标/解读）",
          ev_c2.waveform_b64 is not None and ev_c2.metrics and ev_c2.interpretation)
    with patch.object(llm, "chat", Scripted([GOOD, "缓存测试解读。"])) as _s:
        ev_c3 = pipeline.run_pipeline("缓存测试电路", validators=[lambda e, t: []])
    check("缓存: 带 validators 时不吃缓存（真实测量）", ev_c3.ok and len(_s.calls) >= 2)

    # ---- 5. FastAPI 接口 ----
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/")
        check("GET / 返回对话页", r.status_code == 200 and "VoltProof" in r.text)
        r = client.get("/compare.html")
        check("GET /compare.html 返回对照页", r.status_code == 200 and "同题对照" in r.text)

    with patch.object(llm, "chat", Scripted([GOOD, "接口解读。"])) as sc2, TestClient(app) as client:
        r = client.post("/chat", json={"session_id": "api-test", "message": "1kHz低通"})
        d = r.json()
        check("POST /chat 200", r.status_code == 200, str(d)[:200])
        check("POST /chat 证据完整", d.get("ok") is True and d.get("waveform_b64")
              and d.get("session_id") == "api-test", str(d.keys()))

    with patch.object(llm, "chat", lambda *a, **k: "裸模型回答：用1.6k电阻和100nF电容。"), \
            TestClient(app) as client:
        r = client.post("/demo/raw", json={"message": "设计1kHz低通"})
        check("POST /demo/raw 200", r.status_code == 200 and "裸模型" in r.json()["text"])

    # ---- 5b. 工作区端点 ----
    import tempfile
    with TestClient(app) as client:
        r = client.get("/api/ws")
        d = r.json()
        check("GET /api/ws 返回当前工作区", r.status_code == 200 and d.get("root"), str(d))
        check("GET /api/ws/tasks 列出历史任务",
              r.status_code == 200 and isinstance(client.get("/api/ws/tasks").json().get("tasks"), list))
        tid = ev.task_id  # 用第 1 节落盘的任务取文件
        r = client.get(f"/api/files/{tid}/evidence.json")
        check("GET /api/files 取到任务证据", r.status_code == 200 and b"netlist" in r.content[:400])
        r = client.get(f"/api/files/{tid}/../../app/llm.py")
        check("GET /api/files 路径逃逸被拒", r.status_code in (403, 404), str(r.status_code))
        ws_tmp = tempfile.mkdtemp(prefix="cp_ws_test_")
        r = client.post("/api/ws/open", json={"path": ws_tmp})
        check("POST /api/ws/open 切换到新目录",
              r.status_code == 200 and r.json().get("root"), str(r.json()))
        r = client.get("/api/ws")
        check("切换后 /api/ws 指向新工作区", r.json().get("root") == str(Path(ws_tmp).resolve()), str(r.json()))
        r = client.post("/api/ws/open", json={"path": "D:\\不存在的目录_xyz"})
        check("POST /api/ws/open 拒绝不存在目录", r.status_code == 400)
        r = client.post("/api/ws/open", json={"path": str(pipeline.default_workspace().root)})
        check("切回缺省工作区", r.status_code == 200)

    # ---- 5c. 大模型配置端点（供应商 URL / Key / 模型）----
    _orig_model = llm._MODEL
    try:
        with TestClient(app) as client:
            r = client.get("/api/llm/config")
            d = r.json()
            check("LLM端点: GET /api/llm/config 返回脱敏配置",
                  r.status_code == 200 and d.get("model") and d.get("base_url")
                  and d.get("api_key") is None, str(d))
            r = client.post("/api/llm/config", json={"base_url": "http://127.0.0.1:9/v1"})
            check("LLM端点: 内网 URL 被 SSRF 校验拒绝（400 且状态不变）",
                  r.status_code == 400 and "内网" in r.json().get("error", ""))
            r = client.post("/api/llm/config", json={"model": "mock-model-tmp"})
            check("LLM端点: 只改模型成功并回显",
                  r.status_code == 200 and r.json().get("model") == "mock-model-tmp")
            check("LLM端点: 模型切换已重置客户端", llm._client is None)
            r = client.post("/api/llm/models", json={"base_url": "http://169.254.169.254/v1"})
            check("LLM端点: models 拉取同样过 SSRF 校验", r.status_code == 400)
    finally:
        llm._MODEL = _orig_model
        llm._client = None
        Path(os.environ["VOLTPROOF_SETTINGS"]).unlink(missing_ok=True)

    # ---- 6. 电路图受限执行 ----
    from app.render_schematic import _has_content, render_schematic
    import tempfile

    SCH_OK = """import schemdraw
import schemdraw.elements as elm
d = schemdraw.Drawing()
d += elm.SourceV().up().label('5V')
d += elm.Resistor().right().label('1k')
d += elm.Capacitor().down().label('1uF')
d.save('schematic.png', dpi=150)
"""
    tmpdir = tempfile.mkdtemp(prefix="cp_sch_test_")
    png = render_schematic(SCH_OK, Path(tmpdir) / "sch.png")
    check("电路图: 正常代码出 PNG 且有内容",
          png is not None and png.exists() and _has_content(png))
    check("电路图: import os 被拒", render_schematic("import os\nos.system('echo hi')", Path(tmpdir) / "x.png") is None)
    check("电路图: 语法错误被拒", render_schematic("def (", Path(tmpdir) / "y.png") is None)
    check("电路图: 无 save 被拒", render_schematic("import schemdraw\nd = schemdraw.Drawing()", Path(tmpdir) / "z.png") is None)

    print(f"\n{'='*40}\n{'全部通过' if not FAILURES else '失败: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
