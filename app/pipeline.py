"""核心编排：生成 → 静态检查 → 仿真 → 失败重试 → 证据组装。  [W1 串联 / W2 重试 / W3 会话]

用法（W1 命令行验证）：
    python -m app.pipeline "设计一个截止频率1kHz的RC低通滤波器"

这是整个产品的灵魂模块："仿真在环"循环。
每次 LLM 的产出都要过检查器和 ngspice，失败就把真实报错喂回去修复，最多 max_retries 次。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import checks, llm, measure, prompts
from .ngspice_runner import run_netlist
from .render_schematic import render_schematic
from .render_wave import render_wave
from .skidl_builder import build_netlist as skidl_build

if TYPE_CHECKING:  # 只做类型标注，避免运行时循环依赖
    from .measure import Traces

# 验收器：从 Evidence + 波形数据计算结构化检查项 [{name, ok, detail}]
Validator = Callable[["Evidence", "Traces | None"], list[dict]]

MAX_RETRIES = 3
SKIDL_BUILD_FIX = 2  # SKiDL 轨：代码层（执行报错）的修复轮数
OUT_DIR = Path(__file__).resolve().parent.parent / "out"


@dataclass
class Evidence:
    """一次请求的完整证据包，序列化后就是 /chat 接口的响应体。"""

    ok: bool = False
    request: str = ""
    netlist: str = ""
    waveform_b64: str | None = None
    schematic_b64: str | None = None
    metrics: dict = field(default_factory=dict)
    interpretation: str = ""
    retry_log: list[dict] = field(default_factory=list)  # 每轮 {round, stage, problems}
    checks: list[dict] = field(default_factory=list)      # 验收明细 [{name, ok, detail}]
    generator_code: str = ""  # skidl 轨的生成代码（诊断用）
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "request": self.request,
            "netlist": self.netlist,
            "generator_code": self.generator_code,
            "waveform_b64": self.waveform_b64,
            "schematic_b64": self.schematic_b64,
            "metrics": self.metrics,
            "interpretation": self.interpretation,
            "retry_log": self.retry_log,
            "checks": self.checks,
            "elapsed": round(self.elapsed, 1),
        }


def _b64_png(png: Path) -> str:
    return base64.b64encode(png.read_bytes()).decode()


def run_pipeline(request: str, previous_netlist: str | None = None,
                 max_retries: int = MAX_RETRIES,
                 validators: list[Validator] | None = None,
                 backend: str = "spice",
                 initial_netlist: str | None = None) -> Evidence:
    """initial_netlist：用户上传的网表——跳过生成环节直接进"检查→仿真→
    验收→修复"循环（诊断场景），验证通过后同样进入会话状态供后续修改。"""
    ev = Evidence(request=request)
    t0 = time.monotonic()

    if initial_netlist:
        netlist = initial_netlist
        ev.retry_log.append({"round": 0, "stage": "attach", "problems": []})
    elif backend == "skidl":
        netlist = _skidl_track(request, ev)
    else:
        netlist = llm.extract_code_block(
            llm.chat(prompts.GENERATE_SYSTEM, prompts.generate_user(request, previous_netlist))
        )
        if not netlist:
            # 推理型模型偶发空响应——重生成一次，别让空网表直接终止管线
            netlist = llm.extract_code_block(
                llm.chat(prompts.GENERATE_SYSTEM, prompts.generate_user(request, previous_netlist))
            )
        ev.retry_log.append({"round": 0, "stage": "generate", "problems": []})

    tune_history: list[str] = []  # 验收环的调参历史（每轮实测记录，跨轮累积）
    numeric_tunes = 0             # 数值调参次数上限：线性近似不成立时及时回退 LLM
    for rnd in range(max_retries + 1):
        if not netlist:  # skidl 轨构建彻底失败（日志已在 _skidl_track 里）
            break
        # ---- 静态检查 ----
        chk = checks.run_checks(netlist)
        if not chk.ok:
            ev.retry_log.append({"round": rnd, "stage": "static-check", "problems": chk.errors})
            if rnd == max_retries:
                break
            netlist = _repair(netlist, chk.errors)
            continue

        # ---- 仿真 ----
        sim = run_netlist(netlist, workdir=OUT_DIR)
        if not sim.ok:
            ev.retry_log.append({"round": rnd, "stage": "simulate",
                                 "problems": [sim.error_snippet or "仿真失败且无报错文本"]})
            if rnd == max_retries:
                break
            netlist = _repair(netlist, [sim.error_snippet])
            continue
        if sim.raw_path is None:
            # 仿真本身通过但没写出波形数据——报错必须说清楚，否则 LLM 会盲改电路
            ev.retry_log.append({"round": rnd, "stage": "simulate", "problems": [
                "仿真通过但没有产出波形数据：网表缺少 write。请在 .control 块中"
                "添加 set filetype=ascii 和 write out.raw v(输出节点)，其余部分保持不变"]})
            if rnd == max_retries:
                break
            netlist = _repair(netlist, [
                "仿真通过但没有产出波形数据：网表缺少 write。请在 .control 块中"
                "添加 set filetype=ascii 和 write out.raw v(输出节点)，其余部分保持不变"])
            continue

        # ---- 成功：解析、测指标、出图 ----
        tr = None
        try:
            tr = measure.load_traces(sim.raw_path)
            if tr.warning:
                ev.retry_log.append({"round": rnd, "stage": "measure",
                                     "problems": [tr.warning]})  # 非致命，留痕
            ev.metrics = measure.extract_metrics(tr)
            png = render_wave(tr, OUT_DIR / "wave.png", title=request[:40])
            ev.waveform_b64 = _b64_png(png)
        except Exception as e:  # 波形解析失败不致命，证据卡降级
            ev.retry_log.append({"round": rnd, "stage": "measure", "problems": [f"波形解析失败: {e}"]})

        # ---- 验收环：实测指标 vs 需求（2026-09-19 综合实验教训：
        # "仿真跑通"≠"实验达标"，差距必须回喂 LLM 调参重跑）----
        if validators:
            vchecks: list[dict] = []
            for v in validators:
                vchecks.extend(v(ev, tr))
            ev.checks = vchecks
            fails = [c for c in vchecks if not c.get("ok")]
            if fails:
                problems = [f"{c.get('name', '检查项')}未达标：{c.get('detail', '')}" for c in fails]
                ev.retry_log.append({"round": rnd, "stage": "verify", "problems": problems})
                if rnd == max_retries:
                    break
                # 优先程序化数值调参（确定性收敛）：判分层输出的 tune_hint
                # 直接改源幅度/频率；无可执行项才回退 LLM 全量调参
                hints = [c.get("tune_hint") for c in fails if c.get("tune_hint")]
                tuned_nl, note = ("", "")
                if hints and numeric_tunes < 3:
                    from .tuner import numeric_tune
                    tuned_nl, note = numeric_tune(netlist, hints)
                    if tuned_nl:
                        numeric_tunes += 1
                if tuned_nl:
                    tune_history.append(f"第 {rnd + 1} 轮（数值调参）：{note}")
                    netlist = tuned_nl
                else:
                    tune_history.append(f"第 {rnd + 1} 轮实测未达标：" + "；".join(problems)[:400])
                    netlist = _tune(request, netlist, problems, history=tune_history)
                continue

        # ---- 全部通过：解读、电路图 ----
        ev.netlist = netlist
        ev.ok = True
        ev.interpretation = llm.chat(
            prompts.INTERPRET_SYSTEM,
            prompts.interpret_user(request, netlist, ev.metrics or {"提示": "未解析到波形数据"}),
        )

        # 电路原理图：优先确定性自动布局（网表→graphviz 坐标→按位渲染），
        # 失败才回退 LLM 生成 schemdraw 代码的老路
        try:
            from .schematic_layout import render_netlist_schematic
            png = render_netlist_schematic(netlist, OUT_DIR / "schematic.png",
                                           title=request[:24])
            if png is None:
                code = llm.extract_code_block(
                    llm.chat(prompts.SCHEMATIC_SYSTEM, prompts.schematic_user(netlist))
                )
                png = render_schematic(code, OUT_DIR / "schematic.png")
            if png:
                ev.schematic_b64 = _b64_png(png)
        except Exception:
            pass
        break

    ev.elapsed = time.monotonic() - t0
    ev.netlist = ev.netlist or netlist  # 失败时也保留最后版本，便于诊断
    return ev


def _repair(netlist: str, problems: list[str]) -> str:
    fixed = llm.chat(prompts.REPAIR_SYSTEM, prompts.repair_user(netlist, problems))
    return llm.extract_code_block(fixed)


def _tune(request: str, netlist: str, problems: list[str],
          history: list[str] | None = None) -> str:
    tuned = llm.chat(prompts.TUNE_SYSTEM, prompts.tune_user(request, netlist, problems, history))
    return llm.extract_code_block(tuned)


def _skidl_track(request: str, ev: "Evidence") -> str | None:
    """SKiDL 轨：LLM 写 Python 电路代码 → 沙箱执行出网表（代码层修复环），
    产出的网表进入主管线（静态检查/仿真/验收环与 spice 轨完全复用）。"""
    code = llm.extract_code_block(llm.chat(prompts.SKIDL_SYSTEM, prompts.skidl_user(request)))
    ev.generator_code = code
    for rnd in range(SKIDL_BUILD_FIX + 1):
        netlist, err = skidl_build(code)
        if netlist:
            ev.retry_log.append({"round": rnd, "stage": "skidl-build", "problems": []})
            return netlist
        ev.retry_log.append({"round": rnd, "stage": "skidl-build", "problems": [err[:400]]})
        if rnd == SKIDL_BUILD_FIX:
            return None
        code = llm.extract_code_block(
            llm.chat(prompts.SKIDL_REPAIR_SYSTEM, prompts.skidl_repair_user(code, [err]))
        )
        ev.generator_code = code
    return None


# ---------------------------------------------------------------------------
# 会话（W3）：多轮修改作用于同一电路
# ---------------------------------------------------------------------------

_SESSIONS: dict[str, dict] = {}


def get_session(session_id: str | None = None) -> dict:
    sid = session_id or uuid.uuid4().hex[:12]
    if sid not in _SESSIONS:
        _SESSIONS[sid] = {"id": sid, "netlist": None, "history": []}
    return _SESSIONS[sid]


def chat_with_session(session_id: str, message: str,
                      attachment: dict | None = None) -> dict:
    """attachment: {filename, content}——网表文件直接作为初始电路，文本文件
    内容并入需求。"""
    s = get_session(session_id)
    initial_netlist = None
    if attachment and attachment.get("content"):
        if _looks_like_netlist(attachment.get("filename", ""), attachment["content"]):
            initial_netlist = attachment["content"]
        else:
            message = f"{message}\n\n[附件 {attachment.get('filename', '')} 的内容]\n{attachment['content'][:4000]}"
    ev = run_pipeline(message, previous_netlist=initial_netlist or s["netlist"],
                      initial_netlist=initial_netlist)
    if ev.ok:
        s["netlist"] = ev.netlist  # 只有验证通过的电路才进入会话状态
    s["history"].append({"user": message, "ok": ev.ok,
                         "attachment": attachment and attachment.get("filename")})
    d = ev.to_dict()
    d["session_id"] = s["id"]  # 空入参时客户端也能拿到新建的会话 id
    return d


def _looks_like_netlist(filename: str, content: str) -> bool:
    """扩展名或内容特征判断是否 SPICE 网表。"""
    if filename.lower().split(".")[-1] in ("cir", "sp", "ckt", "net", "spi", "spice"):
        return True
    head = content[:3000].lower()
    return any(k in head for k in (".tran", ".ac ", ".op", ".model", ".control",
                                   ".subckt", ".include", ".end"))


if __name__ == "__main__":
    import sys

    req = " ".join(sys.argv[1:]) or "设计一个截止频率1kHz的RC低通滤波器"
    result = run_pipeline(req)
    print(json.dumps({k: v for k, v in result.to_dict().items() if k != "waveform_b64"},
                     ensure_ascii=False, indent=2))
    if result.waveform_b64:
        (OUT_DIR / "wave.png").exists() and print(f"\n波形图: {OUT_DIR / 'wave.png'}")
    sys.exit(0 if result.ok else 1)
