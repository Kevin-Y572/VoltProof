"""画板直仿：用户在网页画布上连出的电路 → 网表 → 确定性验证。

与 /chat 管线的根本区别：不进 LLM 修复环。画板面向教学——学生画的
电路错在哪里必须原样告诉学生（浮空引脚、无地、ngspice 报错原文），
而不是让 AI 悄悄改掉再交卷。LLM 只在显式点「AI 解读」时出场
（/api/draw/interpret，解释波形，永远不改电路）。

项目制（project.json 是电路唯一权威表示）：成功后网表写入项目上下文，
所有线程/what-if/报告共用；历史由任务档案（docs/<task_id>/）承载，
不再写会话。
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from . import checks, i18n, llm, measure, prompts, specs, project
from .ngspice_runner import check_netlist_safety, run_netlist
from .render_wave import render_wave

_NETLIST_LIMIT = 128 * 1024  # 画布合成网表远小于此；防内存滥用


def _b64_png(png: Path | None) -> str | None:
    if not png:
        return None
    try:
        return base64.b64encode(Path(png).read_bytes()).decode()
    except OSError:
        return None


def verify_drawn(netlist: str, title: str, workspace, *, requirements=None, expected_revision=None, canvas=None) -> dict:
    """画板网表直仿。失败时 stage/errors 指明环节，绝不改写用户的网表。"""
    from .pipeline import _write_evidence_file  # 延迟导入避免环

    t0 = time.monotonic()
    proj = workspace.load_project()
    base_revision = project.revision(proj) if expected_revision is None else expected_revision
    requirements = specs.normalize(requirements) if requirements is not None else proj.get("requirements", [])
    result: dict = {"ok": False, "stage": "", "errors": [], "error": "",
                    "hint": "", "task_id": "", "netlist": netlist,
                    "metrics": None, "waveform_b64": None, "schematic_b64": None,
                    "analysis": "", "source": "draw-sim", "requirements": requirements,
                    "checks": [], "verdict": "failed", "validation": {}, "warnings": [],
                    "provenance": {"netlist_hash": project.digest(netlist), "measurement_version": specs.VERSION,
                                   "created": time.strftime("%Y-%m-%d %H:%M:%S")}}
    if len(netlist) > _NETLIST_LIMIT:
        result.update(stage="safety", errors=[i18n.t("api.netlist_big", kb=_NETLIST_LIMIT // 1024)],
                      error=i18n.t("api.netlist_big_short"), hint=i18n.t("api.netlist_big_hint"))
        return result

    tid = workspace.new_task_id()
    sim_dir = workspace.task_sim_dir(tid)
    doc_dir = workspace.task_docs_dir(tid)
    result["task_id"] = tid

    def _fail(stage: str, errors: list[str], error: str, hint: str) -> dict:
        result.update(stage=stage, errors=errors, error=error, hint=hint,
                      elapsed=round(time.monotonic() - t0, 2))
        _write_evidence_file(_evidence(result, title, tid), doc_dir)
        return result

    # 1. 安全门（画板网表是客户端任意输入，与附件网表同级信任）
    problems = check_netlist_safety(netlist, root=sim_dir)
    if problems:
        return _fail("safety", problems, i18n.t("api.safety_fail"), i18n.t("api.safety_hint"))

    # 2. 静态检查：浮空节点/无地/值语法——画板最常见的接线错误在这里拦下
    chk = checks.run_checks(netlist)
    result["validation"]["static"] = "passed" if chk.ok else "failed"
    if not chk.ok:
        return _fail("checks", list(chk.errors), i18n.t("api.check_fail"), i18n.t("api.check_hint"))

    # 3. 仿真
    sim = run_netlist(netlist, workdir=sim_dir)
    result["validation"]["simulation"] = "passed" if sim.ok else "failed"
    if not sim.ok:
        return _fail("simulate", [sim.error_snippet or i18n.t("api.sim_no_text")],
                     sim.error_snippet or i18n.t("api.sim_fail"), _sim_hint(sim.error_snippet))
    if sim.raw_path is None:
        return _fail("simulate", [i18n.t("api.no_wave")],
                     i18n.t("api.no_wave_short"), i18n.t("api.no_wave_hint"))

    # 4. 指标 + 图
    tr = None
    try:
        tr = measure.load_traces(sim.raw_path)
        measure.validate_traces(tr)
        result["analysis"] = tr.analysis
        result["metrics"] = measure.extract_metrics(tr)
        measure.save_trace_preview(tr, doc_dir / "wave.json")
        result["validation"]["measurement"] = "passed"
    except Exception as e:
        result["verdict"] = "incomplete"
        result["validation"]["measurement"] = "failed"
        return _fail("measure", [i18n.t("api.parse_wave", msg=e)], i18n.t("api.evidence_bad"), i18n.t("api.evidence_hint"))
    try:
        result["waveform_b64"] = _b64_png(render_wave(tr, doc_dir / "wave.png",
                                                      title=title[:40]))
    except Exception as e:
        result["warnings"].append(i18n.t("api.wave_png", msg=e))
    result["checks"] = specs.evaluate(tr, requirements, netlist)
    failed_checks = [c for c in result["checks"] if not c.get("ok")]
    if failed_checks:
        # 仿真已经完成。未达标也要留下可比较的版本，并作为当前电路，不能停在「尚未验证」。
        result["verdict"] = "incomplete" if any(c.get("ok") is None for c in failed_checks) else "failed"
        result["validation"]["requirements"] = result["verdict"]
        result["stage"] = "verify"
        result["errors"] = [c["detail"] for c in failed_checks]
        result["error"] = i18n.t("api.target_miss")
        result["hint"] = i18n.t("api.target_hint")
    else:
        result["ok"] = True
        result["verdict"] = "passed" if result["checks"] else "simulated"
        result["validation"]["requirements"] = "passed" if result["checks"] else "not_requested"
        result["stage"] = "done"
    try:
        from .schematic_layout import render_netlist_schematic
        result["schematic_b64"] = _b64_png(
            render_netlist_schematic(netlist, doc_dir / "schematic.png", title=title[:24]))
    except Exception:
        pass  # 原理图是加分项，用户画布本身就是图

    from .ngspice_runner import simulator_info
    result["provenance"]["simulator"] = simulator_info()
    result["elapsed"] = round(time.monotonic() - t0, 2)
    result["snapshot"] = True
    if canvas is None:
        from .canvas_layout import netlist_to_canvas
        canvas = netlist_to_canvas(netlist)
    v = project.candidate(workspace, result, canvas, base_revision)
    result["version_id"] = v["id"]
    try:
        p = project.apply(workspace, v["id"])
        result.update(applied=True, project_revision=project.revision(p))
    except project.Conflict as e:
        result.update(applied=False)
        result["warnings"].append(str(e))
    _write_evidence_file(_evidence(result, title, tid), doc_dir)
    return result


def _sim_hint(err: str) -> str:
    """把常见 ngspice 报错翻译成画板动作提示（不改网表，指路即可）。"""
    e = (err or "").lower()
    if "singular matrix" in e or "no dc path" in e or "floating" in e:
        return i18n.t("api.hint.singular")
    if "timestep" in e:
        return i18n.t("api.hint.timestep")
    if "undefined" in e and "model" in e:
        return i18n.t("api.hint.model")
    if "iteration" in e or "converge" in e:
        return i18n.t("api.hint.converge")
    return i18n.t("api.hint.generic")


def _evidence(result: dict, title: str, tid: str) -> "object":
    """组装与 /chat 管线同构的 Evidence（复用 _write_evidence_file 落盘）。"""
    from .pipeline import Evidence

    return Evidence(
        ok=result["ok"], rejected=False,
        request=f"画板仿真：{title}"[:200], netlist=result["netlist"],
        task_id=tid or "", task_dir="",
        metrics=result.get("metrics") or {},
        interpretation="",
        retry_log=[{"round": 0, "stage": result["stage"],
                    "problems": result.get("errors") or []}] if not result["ok"] else [],
        checks=result.get("checks", []),
        elapsed=result.get("elapsed") or 0.0,
        source=result.get("source", "draw-sim"),
        requirements=result.get("requirements", []), verdict=result.get("verdict", "failed"),
        validation=result.get("validation", {}), warnings=result.get("warnings", []),
        provenance=result.get("provenance", {}), version_id=result.get("version_id", ""),
    )


def _update_project_netlist(netlist: str, workspace) -> None:
    """验证通过的电路写入项目上下文（项目制：所有线程/画板共用这一份）。"""
    from .pipeline import _now

    proj = workspace.load_project()
    proj["netlist"] = netlist
    proj["updated"] = _now()
    workspace.save_project(proj)


def interpret_drawn(workspace, task_id=None) -> dict:
    """AI 解读项目电路：取项目网表 + 最新成功任务的指标，LLM 只解释不改。"""
    proj = workspace.load_project()
    try:
        netlist = project.active_netlist(proj, verified=not bool(task_id))
    except project.Conflict as e:
        return {"error": str(e)}
    bound_evidence = None
    if task_id:
        from .pipeline import _load_archived_evidence
        bound_evidence = _load_archived_evidence(workspace, task_id)
        if not bound_evidence or not bound_evidence.get("ok"):
            return {"error": i18n.t("api.evidence_incomplete")}
        netlist = bound_evidence.get("netlist")
    if not netlist:
        return {"error": i18n.t("api.no_verified")}
    metrics: dict = {}
    latest_tid = task_id or proj.get("evidence_id")
    for t in workspace.list_tasks(limit=50):  # 新任务在前
        if latest_tid:
            break
        if t.get("ok") and t.get("task_id"):
            from .pipeline import _load_archived_evidence
            old = _load_archived_evidence(workspace, t["task_id"])
            if old and project.digest(old.get("netlist", "")) == project.digest(netlist):
                latest_tid = t["task_id"]
    if latest_tid:
        try:
            f = workspace.resolve(Path("docs") / latest_tid / "evidence.json")
            bound_evidence = json.loads(f.read_text(encoding="utf-8"))
            if project.digest(bound_evidence.get("netlist", "")) == project.digest(netlist):
                metrics = bound_evidence.get("metrics") or {}
        except (PermissionError, OSError, json.JSONDecodeError):
            metrics = {}
    try:
        text = llm.chat(prompts.INTERPRET_SYSTEM,
                        prompts.interpret_user("画板电路仿真", netlist,
                                                metrics or {"提示": "未解析到波形数据"},
                                                (bound_evidence or {}).get("checks"),
                                                (bound_evidence or {}).get("verdict", "incomplete")),
                        thinking=False)
    except llm.LLMError as e:
        return {"error": i18n.t("api.llm_down_cfg", msg=e)}
    # 回写任务档案，实验报告随之完整
    if latest_tid:
        _patch_archived_interpretation(latest_tid, text, workspace)
    return {"text": text}


def _patch_archived_interpretation(task_id: str, text: str, workspace) -> None:
    try:
        f = workspace.resolve(Path("docs") / task_id / "evidence.json")
        d = json.loads(f.read_text(encoding="utf-8"))
    except (PermissionError, OSError, json.JSONDecodeError):
        return
    if d.get("ok"):
        d["interpretation"] = text
        try:
            f.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        except OSError:
            pass


def ai_draw_events(message: str, canvas_netlist: str | None, workspace, *, requirements=None,
                   expected_revision=None, cancelled=None, time_budget=600):
    """AI 画图轮：自然语言 → 修复环管线（生成/改+验证）→ 网表 → 画布。

    previous_netlist 取【画布当前网表】（用户可能改了还没仿真），画布是
    项目电路的编辑视图；验证通过后网表写入项目上下文。事件格式与
    /chat/stream 完全一致（前端同一套 SSE 解析）；done 事件额外携带
    canvas（确定性布局的画布 schema），前端进入预览→确认应用流程。
    落布失败（拓扑终检不过）canvas 为 None，前端降级为只展示证据卡。"""
    from .pipeline import Evidence, iter_pipeline_events

    ws = workspace
    message = message[:8000]
    proj = ws.load_project()
    base_revision = project.revision(proj) if expected_revision is None else expected_revision
    requirements = specs.normalize(requirements) if requirements is not None else specs.extract(
        message, proj.get("requirements", []) if canvas_netlist or proj.get("requirements_pending") else [])

    gen = iter_pipeline_events(message, previous_netlist=canvas_netlist,
                               workspace=ws, source="draw-ai", requirements=requirements,
                               cancelled=cancelled, time_budget=time_budget)
    ev: Evidence | None = None
    while True:
        try:
            yield next(gen)
        except StopIteration as s:
            ev = s.value
            break

    canvas = None
    if ev.ok:
        try:
            from .canvas_layout import netlist_to_canvas
            canvas = netlist_to_canvas(ev.netlist)
        except Exception:
            canvas = None  # 落布是加分项，失败不阻塞证据卡

    d = ev.to_dict()
    d["canvas"] = canvas
    d["applied"] = False
    d["base_revision"] = base_revision
    if ev.ok:
        v = project.candidate(ws, d, canvas, base_revision)
        d["candidate_id"] = v["id"]
        d["version_id"] = v["id"]
    yield {"type": "done", **d}
