"""核心编排：生成 → 静态检查 → 仿真 → 失败重试 → 证据组装。  [W1 串联 / W2 重试 / W3 会话]

用法（W1 命令行验证）：
    python -m app.pipeline "设计一个截止频率1kHz的RC低通滤波器"

这是整个产品的灵魂模块："仿真在环"循环。
每次 LLM 的产出都要过检查器和 ngspice，失败就把真实报错喂回去修复，最多 max_retries 次。
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import checks, i18n, llm, measure, prompts, specs, project
from .ngspice_runner import run_netlist
from .render_schematic import render_schematic
from .render_wave import render_wave
from .skidl_builder import build_netlist as skidl_build
from .workspace import SAFE_CID_RE, Workspace, default_workspace

if TYPE_CHECKING:  # 只做类型标注，避免运行时循环依赖
    from .measure import Traces

# 验收器：从 Evidence + 波形数据计算结构化检查项 [{name, ok, detail}]
Validator = Callable[["Evidence", "Traces | None"], list[dict]]

MAX_RETRIES = 3
SKIDL_BUILD_FIX = 2  # SKiDL 轨：代码层（执行报错）的修复轮数


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
    task_id: str = ""         # 本次运行在工作区内的任务目录名
    task_dir: str = ""        # 任务产物目录绝对路径（sim/docs 各有同名子目录）
    elapsed: float = 0.0
    rejected: bool = False  # 领域守卫前置拒绝（与仿真失败区分）
    source: str = ""  # 产生来源：chat（对话线程）/ draw-sim（画板直仿）/ draw-ai（AI 画图轮）
    requirements: list[dict] = field(default_factory=list)
    verdict: str = "failed"  # passed / simulated / incomplete / failed / cancelled / explained
    validation: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    version_id: str = ""
    action: str = "design"
    changes: str = ""
    from_cache: bool = False
    comparison: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "rejected": self.rejected,
            "request": self.request,
            "netlist": self.netlist,
            "generator_code": self.generator_code,
            "task_id": self.task_id,
            "task_dir": self.task_dir,
            "waveform_b64": self.waveform_b64,
            "schematic_b64": self.schematic_b64,
            "metrics": self.metrics,
            "interpretation": self.interpretation,
            "retry_log": self.retry_log,
            "checks": self.checks,
            "elapsed": round(self.elapsed, 1),
            "source": self.source,
            "requirements": self.requirements, "verdict": self.verdict,
            "validation": self.validation, "warnings": self.warnings,
            "provenance": self.provenance, "version_id": self.version_id,
            "action": self.action, "changes": self.changes, "from_cache": self.from_cache,
            "comparison": self.comparison,
        }


def _b64_png(png: Path) -> str:
    return base64.b64encode(png.read_bytes()).decode()


# 领域守卫：数字逻辑（Verilog/RTL）类需求不在能力范围——本工具做的是
# 模拟电路 + SPICE 仿真。前置拒绝（零 LLM/仿真消耗），而不是让修复环
# 烧完重试预算后给一张莫名的失败卡。
# 注意不收录裸词 testbench：SPICE 圈也说 testbench（"给放大器搭个 testbench
# 仿真"是合法模拟需求），裸词会误伤；数字语义由 verilog/fpga/rtl 等词承载
_RTL_GUARD_RE = re.compile(
    r"verilog|systemverilog|vhdl|\bfpga\b|\brtl\b|可综合|always\s*@",
    re.IGNORECASE)

_GUARD_INTERP = (
    "这个需求属于数字逻辑设计（Verilog / RTL）范畴，超出了 VoltProof 的能力范围。\n"
    "VoltProof 做的是模拟电路：自然语言描述电路需求 → 生成 SPICE 网表 → ngspice 仿真验证，"
    "输出波形、实测指标与原理图。\n"
    "建议：Verilog 实现请使用数字设计工具链（iverilog / Verilator 等）；"
    "如果题目背后有模拟部分（传感器信号调理、比较器阈值、滤波放大），"
    "欢迎改用电路语言描述，例如：设计一个温度报警电路，输入电压超过 2V 时"
    "输出翻转为高电平并带迟滞。"
)


def _domain_guard(request: str) -> Evidence | None:
    """命中数字 RTL 关键词时立即返回拒绝证据（不进生成/仿真环）。"""
    if not _RTL_GUARD_RE.search(request):
        return None
    return Evidence(
        request=request, ok=False, rejected=True, elapsed=0.0,
        interpretation=i18n.t("api.guard.text"),
        retry_log=[{"round": 0, "stage": "guard",
                    "problems": [i18n.t("api.guard")]}],
    )


def run_pipeline(request: str, previous_netlist: str | None = None,
                 max_retries: int = MAX_RETRIES,
                 validators: list[Validator] | None = None,
                 backend: str = "spice",
                 initial_netlist: str | None = None,
                 workspace: Workspace | None = None, *,
                 requirements: list[dict] | None = None, allow_repair: bool = True,
                 cancelled=None, time_budget: float = 600, include_interpretation=True) -> Evidence:
    """同步包装：消费事件生成器，返回最终证据（bench/tests/CLI/MCP 用）。
    参数语义见 iter_pipeline_events。"""
    gen = iter_pipeline_events(request, previous_netlist, max_retries,
                               validators, backend, initial_netlist, workspace,
                               requirements=requirements, allow_repair=allow_repair,
                               cancelled=cancelled, time_budget=time_budget, include_interpretation=include_interpretation)
    try:
        while True:
            next(gen)
    except StopIteration as s:
        return s.value  # noqa: F821（生成器 return 的证据）


def iter_pipeline_events(request: str, previous_netlist: str | None = None,
                         max_retries: int = MAX_RETRIES,
                         validators: list[Validator] | None = None,
                         backend: str = "spice",
                         initial_netlist: str | None = None,
                         workspace: Workspace | None = None,
                         source: str = "", *, requirements: list[dict] | None = None,
                         allow_repair: bool = True, cancelled=None, time_budget: float = 600,
                         include_interpretation=True):
    """生成器版本：修复环每前进一步 yield 一个事件（/chat/stream SSE 推送用），
    跑完 return Evidence。

    事件：{"type": "stage", "round": r, "stage": <见下>, "problems": [...],
           "note": "可选说明"}
    stage ∈ generate / attach / cache / guard / static-check / simulate /
    verify / regenerate / skidl-build / interpret / schematic。
    initial_netlist：用户上传的网表——跳过生成环节直接进"检查→仿真→
    验收→修复"循环（诊断场景）。workspace：产物与会话状态落点。
    source：产生来源标记（chat / draw-ai），随 evidence.json 落盘——
    项目时间线按它区分轮次类型；必须在 Evidence 创建时传入（档案在
    管线内部写盘，外层补设为时已晚）。"""
    from . import cache as _cache

    ws = workspace or default_workspace()
    external_validators = validators is not None
    requirements = specs.normalize(requirements) if requirements is not None else (
        [] if external_validators else specs.extract(request))
    validators = list(validators or [])
    max_retries = max(0, min(int(max_retries), 12)) if allow_repair else 0
    started = time.monotonic()

    def interrupted():
        return (cancelled is not None and cancelled()) or time.monotonic() - started > time_budget

    guard = _domain_guard(request)
    if guard is not None:
        guard.source = source or "chat"
        yield {"type": "stage", "round": 0, "stage": "guard",
               "problems": [i18n.t("api.guard")]}
        return guard

    # 结果缓存：相同请求（无验收器）直接返回完整证据——演示防翻车
    cache_context = {"requirements": requirements, "policy": specs.VERSION, "repair": allow_repair,
                     "interpretation":include_interpretation}
    if not external_validators and initial_netlist is None and not interrupted():
        cached = _cache.get(request, previous_netlist, backend, cache_dir=ws.cache_dir, context=cache_context)
        if cached is not None:
            ev = Evidence(**{k: v for k, v in cached.items()
                             if k in Evidence.__dataclass_fields__ and k != "elapsed"})
            ev.elapsed = 0.0
            ev.source = source or "chat"
            ev.retry_log.append({"round": 0, "stage": "cache", "problems": []})
            yield {"type": "stage", "round": 0, "stage": "cache", "problems": []}
            return ev

    task_id = ws.new_task_id()
    sim_dir = ws.task_sim_dir(task_id)
    doc_dir = ws.task_docs_dir(task_id)
    ev = Evidence(request=request, task_id=task_id, task_dir=str(doc_dir),
                  source=source or "chat", requirements=requirements,
                  provenance={"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                              "model": llm._MODEL, "measurement_version": specs.VERSION,
                              "window": "瞬态按时间去除前20%暂态，平均/RMS按时间积分",
                              "simulator": "ngspice", "backend": backend})
    t0 = time.monotonic()
    if interrupted():
        ev.verdict = "cancelled"
        return ev
    contract = ""
    if requirements:
        contract = "\n\n固定验收条件（不可修改目标；write 必须包含明确的输入输出节点）：\n" + json.dumps(requirements, ensure_ascii=False)
        yield {"type": "stage", "round": 0, "stage": "requirements", "problems": [], "requirements": requirements}

    if initial_netlist:
        netlist = initial_netlist
        ev.retry_log.append({"round": 0, "stage": "attach", "problems": []})
        yield {"type": "stage", "round": 0, "stage": "attach", "problems": []}
    elif backend == "skidl":
        netlist = yield from _skidl_track_events(request, ev)
    else:
        yield {"type": "stage", "round": 0, "stage": "generate", "state":"running", "problems": [], "note": "正在生成网表"}
        netlist = llm.extract_code_block(
            llm.chat(prompts.GENERATE_SYSTEM, prompts.generate_user(request + contract, previous_netlist))
        )
        if not netlist:
            # 推理型模型偶发空响应——重生成一次，别让空网表直接终止管线
            netlist = llm.extract_code_block(
                llm.chat(prompts.GENERATE_SYSTEM, prompts.generate_user(request + contract, previous_netlist))
            )
        ev.retry_log.append({"round": 0, "stage": "generate", "problems": []})
        yield {"type": "stage", "round": 0, "stage": "generate", "problems": []}

    tune_history: list[str] = []  # 验收环的调参历史（每轮实测记录，跨轮累积）
    numeric_tunes = 0             # 数值调参次数上限：线性近似不成立时及时回退 LLM
    llm_fix_fails = 0             # 连续 LLM 修复失败次数：≥2 时重启换架构（跳出局部解）
    for rnd in range(max_retries + 1):
        if interrupted():
            ev.verdict = "cancelled"
            ev.warnings.append("任务已取消或达到总耗时预算；没有应用电路变更")
            break
        if not netlist:  # skidl 轨构建彻底失败（日志已在 _skidl_track_events 里）
            break
        ev.netlist = netlist
        ev.metrics = {}
        ev.checks = []
        ev.waveform_b64 = None
        ev.validation = {"static": "pending", "simulation": "pending", "measurement": "pending", "requirements": "pending"}
        # ---- 修复环重启：同一网表连续修不好，说明架构有问题，重新设计 ----
        if llm_fix_fails >= 2:
            ev.retry_log.append({"round": rnd, "stage": "regenerate",
                                 "problems": ["连续修复失败，换一种电路架构重新生成"]})
            yield {"type": "stage", "round": rnd, "stage": "regenerate",
                   "problems": ["连续修复失败，换一种电路架构重新生成"]}
            netlist = llm.extract_code_block(
                llm.chat(prompts.GENERATE_SYSTEM,
                         prompts.generate_user(request + contract, previous_netlist),
                         temperature=0.5, thinking=False)
            )
            # 换架构重启要快且求多样：官方文档明确思考模式下 temperature 被静默
            # 忽略——关思考后 temperature=0.5 才真正生效，也不再每轮苦等长思考
            llm_fix_fails = 0
            continue
        # ---- 静态检查 ----
        chk = checks.run_checks(netlist)
        ev.validation["static"] = "passed" if chk.ok else "failed"
        if not chk.ok:
            ev.retry_log.append({"round": rnd, "stage": "static-check", "problems": chk.errors})
            yield {"type": "stage", "round": rnd, "stage": "static-check", "problems": chk.errors}
            if rnd == max_retries:
                break
            llm_fix_fails += 1
            netlist = _repair(netlist, chk.errors)
            continue

        # ---- 仿真 ----
        yield {"type": "stage", "round": rnd, "stage": "simulate", "state":"running", "problems": [], "note": "正在运行 ngspice"}
        round_dir = sim_dir / f"round-{rnd + 1}"
        simulation_netlist, verification_plan = specs.ac_verification_netlist(netlist, requirements)
        if verification_plan:
            round_dir.mkdir(parents=True, exist_ok=True)
            (round_dir / "source.cir").write_text(netlist, encoding="utf-8")
            ev.provenance["source_netlist_hash"] = project.digest(netlist)
            netlist = ev.netlist = simulation_netlist
        sim = run_netlist(simulation_netlist, workdir=round_dir)
        ev.provenance["verification_plan"] = verification_plan
        ev.provenance["simulated_netlist_hash"] = project.digest(simulation_netlist)
        ev.provenance["netlist_hash"] = project.digest(netlist)
        ev.provenance["raw_file"] = str(Path("sim") / task_id / round_dir.name / "out.raw")
        ev.validation["simulation"] = "passed" if sim.ok else "failed"
        if not sim.ok:
            probs = [sim.error_snippet or "仿真失败且无报错文本"]
            ev.retry_log.append({"round": rnd, "stage": "simulate", "problems": probs})
            yield {"type": "stage", "round": rnd, "stage": "simulate", "problems": probs}
            if rnd == max_retries:
                break
            llm_fix_fails += 1
            netlist = _repair(netlist, [sim.error_snippet])
            continue
        if sim.raw_path is None:
            # 仿真本身通过但没写出波形数据——报错必须说清楚，否则 LLM 会盲改电路
            probs = ["仿真通过但没有产出波形数据：网表缺少 write。请在 .control 块中"
                     "添加 set filetype=ascii 和 write out.raw v(输出节点)，其余部分保持不变"]
            ev.retry_log.append({"round": rnd, "stage": "simulate", "problems": probs})
            yield {"type": "stage", "round": rnd, "stage": "simulate", "problems": probs}
            if rnd == max_retries:
                break
            netlist = _repair(netlist, probs)
            continue

        yield {"type":"stage", "round":rnd, "stage":"simulate", "problems":[]}
        # ---- 成功：解析、测指标、出图（修复环计数归零）----
        llm_fix_fails = 0
        tr = None
        try:
            tr = measure.load_traces(sim.raw_path)
            measure.validate_traces(tr)
            if tr.warning:
                ev.retry_log.append({"round": rnd, "stage": "measure",
                                     "problems": [tr.warning]})  # 非致命，留痕
            ev.metrics = measure.extract_metrics(tr)
            measure.save_trace_preview(tr, doc_dir / "wave.json")
            ev.validation["measurement"] = "passed"
        except Exception as e:
            ev.validation["measurement"] = "failed"
            ev.verdict = "incomplete"
            ev.retry_log.append({"round": rnd, "stage": "measure", "problems": [f"波形解析失败: {e}"]})
            break
        try:
            png = render_wave(tr, doc_dir / "wave.png", title=request[:40])
            ev.waveform_b64 = _b64_png(png)
        except Exception as e:
            ev.warnings.append(f"波形图片生成失败，原始数据仍可复核：{e}")

        # ---- 验收环：实测指标 vs 需求（"仿真跑通"≠"实验达标"，
        # 差距必须回喂 LLM 调参重跑）----
        if validators or requirements:
            vchecks: list[dict] = specs.evaluate(tr, requirements, netlist)
            for v in validators:
                vchecks.extend(v(ev, tr))
            ev.checks = vchecks
            (round_dir / "checks.json").write_text(json.dumps(vchecks, ensure_ascii=False, indent=1), encoding="utf-8")
            ev.validation["requirements"] = "incomplete" if any(c.get("ok") is None for c in vchecks) else (
                "passed" if all(c.get("ok") for c in vchecks) else "failed")
            fails = [c for c in vchecks if not c.get("ok")]
            if fails:
                ev.verdict = "incomplete" if any(c.get("ok") is None for c in fails) else "failed"
                problems = [f"{c.get('name', '检查项')}未达标：{c.get('detail', '')}" for c in fails]
                ev.retry_log.append({"round": rnd, "stage": "verify", "problems": problems})
                if rnd == max_retries or any(c.get("ok") is None for c in fails):
                    yield {"type": "stage", "round": rnd, "stage": "verify", "problems": problems}
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
                    yield {"type": "stage", "round": rnd, "stage": "verify",
                           "problems": problems, "note": f"数值调参：{note}"}
                else:
                    tune_history.append(f"第 {rnd + 1} 轮实测未达标：" + "；".join(problems)[:400])
                    netlist = _tune(request + contract, netlist, problems, history=tune_history)
                    yield {"type": "stage", "round": rnd, "stage": "verify",
                           "problems": problems, "note": "回喂差距，LLM 调参重跑"}
                continue

        # ---- 全部通过：解读、电路图 ----
        ev.netlist = netlist
        ev.ok = True
        ev.verdict = "passed" if ev.checks else "simulated"
        if not ev.checks:
            ev.validation["requirements"] = "not_requested"
        if interrupted():
            ev.ok = False
            ev.verdict = "cancelled"
            break
        if include_interpretation:
            yield {"type": "stage", "round": rnd, "stage": "interpret", "problems": []}
            try:
                ev.interpretation = llm.chat(
                    prompts.INTERPRET_SYSTEM,
                    prompts.interpret_user(request, netlist, ev.metrics, ev.checks, ev.verdict),
                    thinking=False,
                )
            except Exception as e:
                ev.warnings.append(i18n.t("api.interpret_down", detail=type(e).__name__))

        # 电路原理图：优先确定性自动布局（网表→graphviz 坐标→按位渲染），
        # 失败才回退 LLM 生成 schemdraw 代码的老路
        try:
            yield {"type": "stage", "round": rnd, "stage": "schematic", "problems": []}
            from .schematic_layout import render_netlist_schematic
            png = render_netlist_schematic(netlist, doc_dir / "schematic.png",
                                           title=request[:24])
            if png is None:
                ev.warnings.append("该电路暂时不能自动绘图；请按完整网表复核连接")
            if png:
                ev.schematic_b64 = _b64_png(png)
        except Exception:
            pass
        break

    ev.elapsed = time.monotonic() - t0
    if interrupted():
        ev.ok = False
        ev.verdict = "cancelled"
    ev.netlist = ev.netlist or netlist  # 失败时也保留最后版本，便于诊断
    from .ngspice_runner import simulator_info
    ev.provenance.update(simulator=simulator_info(), max_retries=max_retries,
        first_attempt_passed=bool(ev.ok and not any(r.get("problems") and r.get("stage") in
            ("static-check", "simulate", "verify", "skidl-build") for r in ev.retry_log)),
        model_definitions_hash=project.digest("\n".join(line for line in (ev.netlist or "").splitlines()
            if re.match(r"\s*\.(model|subckt|include|lib)\b", line, re.I))))
    if ev.ok and not external_validators and initial_netlist is None:
        _cache.put(request, previous_netlist, backend, ev.to_dict(),
                   cache_dir=ws.cache_dir, context=cache_context)
    _write_evidence_file(ev, doc_dir)
    return ev


def _skidl_track_events(request: str, ev: "Evidence"):
    """SKiDL 轨的事件化版本（yield 构建事件，return 网表或 None）。"""
    code = llm.extract_code_block(llm.chat(prompts.SKIDL_SYSTEM, prompts.skidl_user(request)))
    ev.generator_code = code
    for rnd in range(SKIDL_BUILD_FIX + 1):
        netlist, err = skidl_build(code)
        if netlist:
            ev.retry_log.append({"round": rnd, "stage": "skidl-build", "problems": []})
            yield {"type": "stage", "round": rnd, "stage": "skidl-build", "problems": []}
            return netlist
        ev.retry_log.append({"round": rnd, "stage": "skidl-build", "problems": [err[:400]]})
        yield {"type": "stage", "round": rnd, "stage": "skidl-build", "problems": [err[:400]]}
        if rnd == SKIDL_BUILD_FIX:
            return None
        code = llm.extract_code_block(
            llm.chat(prompts.SKIDL_REPAIR_SYSTEM, prompts.skidl_repair_user(code, [err]))
        )
        ev.generator_code = code
    return None


def _write_evidence_file(ev: Evidence, doc_dir: Path) -> None:
    """证据 JSON 落盘（成败都写，失败样本是诊断素材）。剥掉 base64 大字段——
    图已经是 docs/ 下的独立文件，JSON 里再放一份只是翻倍占空间。"""
    d = ev.to_dict()
    d.pop("waveform_b64", None)
    d.pop("schematic_b64", None)
    try:
        (doc_dir / "evidence.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # 落盘失败不阻塞返回


def _repair(netlist: str, problems: list[str]) -> str:
    fixed = llm.chat(prompts.REPAIR_SYSTEM, prompts.repair_user(netlist, problems))
    return llm.extract_code_block(fixed)


def _tune(request: str, netlist: str, problems: list[str],
          history: list[str] | None = None) -> str:
    tuned = llm.chat(prompts.TUNE_SYSTEM, prompts.tune_user(request, netlist, problems, history))
    return llm.extract_code_block(tuned)


def _skidl_track(request: str, ev: "Evidence") -> str | None:
    """SKiDL 轨（同步版，供需要普通函数调用的场合）：内部消费事件流。"""
    gen = _skidl_track_events(request, ev)
    try:
        while True:
            next(gen)
    except StopIteration as s:
        return s.value


# ---------------------------------------------------------------------------
# 会话（W3）：多轮修改作用于同一电路。
# 按工作区分键 + 落盘 .cp/state.json（内存 dict 只是热缓存，重启不丢）。
# ---------------------------------------------------------------------------

_CONVERSATIONS: dict[str, dict[str, dict]] = {}  # 工作区根路径 -> cid -> 对话


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def get_conversation(conversation_id: str | None = None,
                     workspace: Workspace | None = None) -> dict:
    """取（或新建）对话记录：消息流 + 最后验证通过的网表（多轮电路上下文）。
    conversation_id 来自客户端任意输入，不合法（含路径字符等）一律换新 id。"""
    ws = workspace or default_workspace()
    store = _CONVERSATIONS.setdefault(str(ws.root), ws.load_conversations())
    cid = conversation_id or ""
    if not SAFE_CID_RE.fullmatch(cid):
        cid = uuid.uuid4().hex[:12]
    if cid not in store:
        store[cid] = {"id": cid, "title": "", "created": _now(),
                      "updated": _now(), "netlist": None, "messages": []}
    return store[cid]


def _save_conversations(workspace: Workspace, conv: dict) -> None:
    workspace.save_conversation(conv)  # 只写这一个对话（分文件存储）


def find_conversation(conversation_id: str | None,
                      workspace: Workspace | None = None) -> dict | None:
    """只读查找对话（不新建）：what-if 扫描等旁路功能取当前电路用。"""
    if not conversation_id:
        return None
    ws = workspace or default_workspace()
    store = _CONVERSATIONS.setdefault(str(ws.root), ws.load_conversations())
    return store.get(conversation_id)


def conversation_list(workspace: Workspace | None = None) -> list[dict]:
    """对话清单（新的在前，只带摘要不带消息流）。"""
    ws = workspace or default_workspace()
    store = _CONVERSATIONS.setdefault(str(ws.root), ws.load_conversations())
    items = [{"id": c["id"], "title": c.get("title") or "（无标题）",
              "updated": c.get("updated", ""),
              "messages": len(c.get("messages", []))}
             for c in store.values()]
    items.sort(key=lambda x: x["updated"], reverse=True)
    return items


def _assistant_entry(ev: "Evidence") -> dict:
    """消息流里的助手条目：正常结果引用任务档案（task_id，恢复时从
    evidence.json + 图片文件重建）；无档案的轻量结果（如领域守卫拒绝）
    内联携带证据，恢复对话时同样完整还原。"""
    if ev.task_id:
        return {"role": "assistant", "task_id": ev.task_id, "ok": ev.ok,
                "rejected": ev.rejected, "ts": _now()}
    return {"role": "assistant",
            "inline": {"ok": ev.ok, "rejected": ev.rejected,
                       "request": ev.request,
                       "interpretation": ev.interpretation,
                       "retry_log": ev.retry_log},
            "ts": _now()}


def _load_archived_evidence(ws: Workspace, task_id: str) -> dict | None:
    """从工作区任务档案重建证据（图片转 /api/files URL，不回传 base64）。"""
    try:
        f = ws.resolve(Path("docs") / task_id / "evidence.json")
        d = json.loads(f.read_text(encoding="utf-8"))
    except (PermissionError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    for key, name in (("wave_url", "wave.png"), ("sch_url", "schematic.png")):
        try:
            if (f.parent / name).is_file():
                d[key] = f"/api/files/{task_id}/{name}"
        except OSError:
            pass
    return d


def restore_conversation(conversation_id: str,
                         workspace: Workspace | None = None) -> dict | None:
    """恢复对话：消息流完整还原——用户消息原文 + 助手证据卡（档案重建）。"""
    ws = workspace or default_workspace()
    store = _CONVERSATIONS.setdefault(str(ws.root), ws.load_conversations())
    c = store.get(conversation_id)
    if c is None:
        return None
    messages = []
    for m in c.get("messages", []):
        if m.get("role") != "assistant":
            messages.append({"role": "user", "text": m.get("text", ""),
                             "attachment": m.get("attachment"), "ts": m.get("ts")})
            continue
        ev = m.get("inline")
        if not ev and m.get("task_id"):
            ev = _load_archived_evidence(ws, m["task_id"])
        if ev is None:
            ev = {"ok": m.get("ok", False), "rejected": m.get("rejected", False),
                  "interpretation": "（该次结果的档案已清理）"}
        messages.append({"role": "assistant", "evidence": ev, "ts": m.get("ts")})
    return {"id": c["id"], "title": c.get("title") or "（无标题）",
            "created": c.get("created"), "updated": c.get("updated"),
            "messages": messages}


def chat_with_conversation_events(conversation_id: str | None, message: str,
                                  attachment: dict | None = None,
                                  workspace: Workspace | None = None, *,
                                  requirements: list[dict] | None = None, expected_revision=None,
                                  cancelled=None, time_budget=600, adopt: bool = True):
    """生成器版本：逐个 yield 管线事件，最后 yield {"type": "done", **响应}。
    响应 dict 与同步版 chat_with_conversation 完全一致（含 conversation_id）。"""
    ws = workspace or default_workspace()
    c = get_conversation(conversation_id, workspace=ws)
    message = message[:8000]  # 正文限长（防内存滥用）
    proj = ws.load_project()
    base_revision = project.revision(proj)
    if expected_revision is not None and expected_revision != base_revision:
        yield {"type": "error", "error": i18n.t("api.chat_stale")}
        return
    attached_netlist = None
    if attachment and attachment.get("content"):
        if len(attachment["content"]) > 256 * 1024:
            yield {"type": "error", "error": i18n.t("api.attach_big")}
            return
        if _looks_like_netlist(attachment.get("filename", ""), attachment["content"]):
            attached_netlist = attachment["content"]
        else:
            message = f"{message}\n\n[附件 {attachment.get('filename', '')} 的内容]\n{attachment['content'][:4000]}"
    current_netlist = attached_netlist or project.active_netlist(proj)
    if proj.get("dirty") and not current_netlist and (proj.get("canvas") or {}).get("comps"):
        yield {"type":"error", "error": i18n.t("api.no_netlist")}
        return
    action = specs.classify(message, bool(current_netlist), bool(attached_netlist))
    inherited = proj.get("requirements", []) if action != "design" or proj.get("requirements_pending") else []
    last_evidence = _load_archived_evidence(ws, proj.get("evidence_id", "")) if proj.get("evidence_id") else None
    if last_evidence and last_evidence.get("provenance", {}).get("netlist_hash") != project.digest(current_netlist or ""):
        last_evidence = None
    requirements = specs.normalize(requirements) if requirements is not None else specs.extract(
        message, inherited, (last_evidence or {}).get("checks"))
    yield {"type": "stage", "round": 0, "stage": "intent", "problems": [], "action": action}
    if action == "compare":
        ev=Evidence(request=message, source="chat",action=action,verdict="explained",task_id=ws.new_task_id())
        current=project.read_version(ws, proj["version_id"]) if proj.get("version_id") else None
        if current and current.get("parent"):
            ev.comparison=project.compare(ws,current["parent"],current["id"])
            ev.changes=ev.comparison["changes"]
            ev.interpretation=ev.comparison["note"]
        else:
            ev.interpretation="还没有两个已采用的电路版本。先完成一次修改，再比较前后方案。"
    elif action == "explain":
        ev = Evidence(request=message, netlist=current_netlist, source="chat", action=action,
                      verdict="explained", requirements=requirements,
                      task_id=ws.new_task_id(), provenance={"netlist_hash": project.digest(current_netlist or "")})
        if current_netlist:
            ev.metrics = (last_evidence or {}).get("metrics", {})
            ev.checks = (last_evidence or {}).get("checks", [])
            if not ev.metrics:
                ev.warnings.append("当前电路没有绑定的仿真证据；以下只解释结构，数值表现尚未验证")
            yield {"type": "stage", "round": 0, "stage": "interpret", "problems": []}
            ev.interpretation = llm.chat(prompts.INTERPRET_SYSTEM,
                prompts.interpret_user(message, current_netlist, ev.metrics, ev.checks,
                                       (last_evidence or {}).get("verdict", "incomplete")), thinking=False)
        else:
            ev.interpretation = "当前项目没有电路。请先绘制电路或上传网表，再进行解释。"
    else:
        initial_netlist = current_netlist if action in ("verify", "diagnose") else None
        # 网表决定当前工程事实；历史只提供用户先前的约束与讨论背景。
        history = "\n".join(m.get("text", "")[:700] for m in c.get("messages", [])[-8:] if m.get("role") == "user")
        prompt_message = message + ("\n\n之前的需求背景（当前网表优先）：\n" + history if history else "")
        gen = iter_pipeline_events(prompt_message, previous_netlist=current_netlist,
                                   initial_netlist=initial_netlist, workspace=ws, source="chat",
                                   requirements=requirements, allow_repair=action != "verify",
                                   cancelled=cancelled, time_budget=time_budget)
        while True:
            try:
                yield next(gen)
            except StopIteration as s:
                ev = s.value
                break
        ev.request = message
        ev.action = action
    if cancelled and cancelled():
        ev.ok = False
        ev.verdict = "cancelled"
    canvas_synced = False
    candidate_id = ""
    applied = False
    if ev.ok and action != "explain":
        import difflib
        ev.changes = "\n".join(difflib.unified_diff((current_netlist or "").splitlines(),
                                                  ev.netlist.splitlines(), fromfile="之前", tofile="候选", lineterm=""))
        try:
            from .canvas_layout import netlist_to_canvas
            _cv = netlist_to_canvas(ev.netlist)
        except Exception:
            _cv = None
        candidate = project.candidate(ws, ev.to_dict(), _cv, base_revision)
        candidate_id = ev.version_id = candidate["id"]
        if adopt and action != "diagnose":
            try:
                project.apply(ws, candidate_id)
                applied = True
                canvas_synced = True  # 空画布也需刷新，防止旧图代表新网表
            except project.Conflict as e:
                ev.warnings.append(str(e))
        elif not adopt:
            ev.warnings.append("没有替换当前项目电路")
    if ev.task_id and not ev.from_cache:
        _write_evidence_file(ev, ws.task_docs_dir(ev.task_id))
    with project.lock(ws):
        c["messages"].append({"role": "user", "text": message, "ts": _now(),
                              "attachment": attachment and attachment.get("filename")})
        c["messages"].append(_assistant_entry(ev))
        if not c.get("title"):
            c["title"] = message.strip().replace("\n", " ")[:36]
        c["updated"] = _now()
        _save_conversations(ws, c)
    d = ev.to_dict()
    d["conversation_id"] = c["id"]  # 空入参时客户端也能拿到新建的对话 id
    d["canvas_synced"] = canvas_synced
    d["candidate_id"] = candidate_id
    d["applied"] = applied
    d["base_revision"] = base_revision
    d["project_revision"] = project.revision(ws.load_project())
    yield {"type": "done", **d}


def chat_with_conversation(conversation_id: str | None, message: str,
                           attachment: dict | None = None,
                           workspace: Workspace | None = None, **kwargs) -> dict:
    """同步版（/chat、测试与外部调用用）：消费事件流返回最终响应。"""
    d: dict = {}
    for ev in chat_with_conversation_events(conversation_id, message,
                                            attachment, workspace, **kwargs):
        if ev.get("type") == "done":
            d = {k: v for k, v in ev.items() if k != "type"}
        elif ev.get("type") == "error":
            d = ev
    return d or {"error": i18n.t("api.pipe_silent")}


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
        (Path(result.task_dir) / "wave.png").exists() and \
            print(f"\n波形图: {Path(result.task_dir) / 'wave.png'}")
    sys.exit(0 if result.ok else 1)
