"""管线与 FastAPI 接口的离线测试：monkeypatch 掉 llm.chat，不花一分钱。  [W3]

覆盖路线图 W2/W3 的核心逻辑：
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

# 确保测试用本地 ngspice（同 test_offline 的约定，已在用户环境变量持久化）
os.environ.setdefault("CIRCUITPILOT_NGSPICE",
                      "D:/Users/Lenovo/tools/ngspice-47/Spice64/bin/ngspice.exe")

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

    def __call__(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.calls.append((system, user))
        return self.responses.pop(0)


def main() -> int:
    # ---- 1. 一次通过 ----
    with patch.object(llm, "chat", Scripted([GOOD, "解读：实测-3dB约1kHz。"])):
        ev = pipeline.run_pipeline("1kHz低通滤波器")
    check("一次通过: ok", ev.ok, str(ev.retry_log))
    check("一次通过: 有波形", ev.waveform_b64 is not None and len(ev.waveform_b64) > 1000)
    check("一次通过: 有指标", len(ev.metrics) > 0, str(ev.metrics))
    check("一次通过: 有解读", "1kHz" in ev.interpretation)
    check("一次通过: 无重试", len([r for r in ev.retry_log if r["stage"] != "generate"]) == 0)

    # ---- 2. 静态检查失败 → 自动修复 ----
    sc = Scripted([NO_GROUND, GOOD, "解读：修复后通过。"])
    with patch.object(llm, "chat", sc):
        ev = pipeline.run_pipeline("1kHz低通滤波器")
    check("静态修复: ok", ev.ok, str(ev.retry_log))
    stages = [r["stage"] for r in ev.retry_log]
    check("静态修复: 记录了 static-check 轮", "static-check" in stages, str(stages))
    check("静态修复: 修复提示包含报错", "参考地" in sc.calls[1][1], sc.calls[1][1][:120])

    # ---- 3. 仿真失败（无 raw）→ 自动修复 ----
    with patch.object(llm, "chat", Scripted([NO_CONTROL, GOOD, "解读：修复后通过。"])):
        ev = pipeline.run_pipeline("1kHz低通滤波器")
    check("仿真修复: ok", ev.ok, str(ev.retry_log))
    stages = [r["stage"] for r in ev.retry_log]
    check("仿真修复: 记录了 simulate 轮", "simulate" in stages, str(stages))

    # ---- 4. 会话多轮：携带上一轮网表 ----
    pipeline._SESSIONS.clear()
    s1 = Scripted([GOOD, "第一轮解读。"])
    with patch.object(llm, "chat", s1):
        d1 = pipeline.chat_with_session("sess-test", "先做1kHz低通")
    check("会话: 第一轮返回 session_id", d1["session_id"] == "sess-test")
    s2 = Scripted([GOOD.replace("1.59k", "8k").replace("100n", "20n"), "第二轮解读。"])
    with patch.object(llm, "chat", s2):
        d2 = pipeline.chat_with_session("sess-test", "把截止频率降到1kHz左右重选参数")
    gen_user2 = s2.calls[0][1]
    check("会话: 第二轮携带上轮网表", "当前网表" in gen_user2 and "V1 in 0" in gen_user2, gen_user2[:100])
    check("会话: 历史两条", len(pipeline._SESSIONS["sess-test"]["history"]) == 2)

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

    def fake_chat(system, user, temperature=0.2):
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
    with patch.object(llm, "chat", Scripted(["附件电路解读：实测-3dB约1kHz。"])) as sc_att:
        d_att = pipeline.chat_with_session("att-1", "帮我仿真验证这个电路", attachment={
            "filename": "my.cir", "content": GOOD.replace("```spice\n", "").replace("\n```", "")})
    check("附件: 网表直接仿真通过", d_att["ok"] is True, str(d_att.get("retry_log")))
    check("附件: 零次生成调用（跳过 LLM 生成）", len(sc_att.calls) == 1, f"{len(sc_att.calls)} 次")
    check("附件: 网表进入会话状态", pipeline._SESSIONS["att-1"]["netlist"] is not None)
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

    # ---- 5. FastAPI 接口 ----
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/")
        check("GET / 返回对话页", r.status_code == 200 and "CircuitPilot" in r.text)
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
