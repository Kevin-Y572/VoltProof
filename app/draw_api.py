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

from . import checks, llm, measure, prompts
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


def verify_drawn(netlist: str, title: str, workspace) -> dict:
    """画板网表直仿。失败时 stage/errors 指明环节，绝不改写用户的网表。"""
    from .pipeline import _write_evidence_file  # 延迟导入避免环

    t0 = time.monotonic()
    result: dict = {"ok": False, "stage": "", "errors": [], "error": "",
                    "hint": "", "task_id": "", "netlist": netlist,
                    "metrics": None, "waveform_b64": None, "schematic_b64": None,
                    "analysis": "", "source": "draw-sim"}
    if len(netlist) > _NETLIST_LIMIT:
        result.update(stage="safety", errors=[f"网表超过 {_NETLIST_LIMIT // 1024}KB 限制"],
                      error="网表过大", hint="元件数量异常，请检查画布")
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
    problems = check_netlist_safety(netlist)
    if problems:
        return _fail("safety", problems, "网表未通过安全检查", "画板生成的网表不应包含外部命令")

    # 2. 静态检查：浮空节点/无地/值语法——画板最常见的接线错误在这里拦下
    chk = checks.run_checks(netlist)
    if not chk.ok:
        return _fail("checks", list(chk.errors), "电路检查未通过",
                     "按错误提示回到画布连线；每个元件引脚都要接到网络，且电路必须有接地")

    # 3. 仿真
    sim = run_netlist(netlist, workdir=sim_dir)
    if not sim.ok:
        return _fail("simulate", [sim.error_snippet or "仿真失败且无报错文本"],
                     sim.error_snippet or "仿真失败", _sim_hint(sim.error_snippet))
    if sim.raw_path is None:
        return _fail("simulate", ["仿真通过但没有产出波形数据"],
                     "无波形数据", "检查仿真类型与分析参数是否匹配（如交流扫描需要 AC 源）")

    # 4. 指标 + 图
    tr = None
    try:
        tr = measure.load_traces(sim.raw_path)
        result["analysis"] = tr.analysis
        result["metrics"] = measure.extract_metrics(tr)
        result["waveform_b64"] = _b64_png(render_wave(tr, doc_dir / "wave.png",
                                                      title=title[:40]))
    except Exception as e:  # 波形解析失败不致命：指标缺省，证据卡降级
        result["errors"] = [f"波形解析失败: {e}"]
    try:
        from .schematic_layout import render_netlist_schematic
        result["schematic_b64"] = _b64_png(
            render_netlist_schematic(netlist, doc_dir / "schematic.png", title=title[:24]))
    except Exception:
        pass  # 原理图是加分项，用户画布本身就是图

    result["ok"] = True
    result["stage"] = "done"
    result["elapsed"] = round(time.monotonic() - t0, 2)
    _write_evidence_file(_evidence(result, title, tid), doc_dir)
    _update_project_netlist(netlist, workspace)  # 项目制：进入项目电路上下文
    return result


def _sim_hint(err: str) -> str:
    """把常见 ngspice 报错翻译成画板动作提示（不改网表，指路即可）。"""
    e = (err or "").lower()
    if "singular matrix" in e or "no dc path" in e or "floating" in e:
        return "存在与地没有直流通路的节点：检查每个节点是否经元件连到地（电容隔直的节点加大电阻接地）"
    if "timestep" in e:
        return "瞬态步长失败：常见于开关突变或增益过大引起的振荡发散，先减小仿真时长或检查反馈极性"
    if "undefined" in e and "model" in e:
        return "元件缺少模型：删除后重新放置该元件（画板会自动补教学模型）"
    if "iteration" in e or "converge" in e:
        return "直流工作点不收敛：常见原因是正反馈或电源直接短路，检查连线"
    return "回到画布检查连线与元件值，报错原文见上方"


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
        checks=[{"name": "画板直仿", "ok": result["ok"],
                 "detail": result.get("error") or "仿真通过"}],
        elapsed=result.get("elapsed") or 0.0,
        source=result.get("source", "draw-sim"),
    )


def _update_project_netlist(netlist: str, workspace) -> None:
    """验证通过的电路写入项目上下文（项目制：所有线程/画板共用这一份）。"""
    from .pipeline import _now

    proj = workspace.load_project()
    proj["netlist"] = netlist
    proj["updated"] = _now()
    workspace.save_project(proj)


def interpret_drawn(workspace) -> dict:
    """AI 解读项目电路：取项目网表 + 最新成功任务的指标，LLM 只解释不改。"""
    proj = workspace.load_project()
    netlist = proj.get("netlist")
    if not netlist:
        return {"error": "本项目还没有验证通过的电路——先点「仿真」再解读"}
    metrics: dict = {}
    latest_tid = None
    for t in workspace.list_tasks(limit=50):  # 新任务在前
        if t.get("ok") and t.get("task_id"):
            latest_tid = t["task_id"]
            break
    if latest_tid:
        try:
            f = workspace.resolve(Path("docs") / latest_tid / "evidence.json")
            metrics = json.loads(f.read_text(encoding="utf-8")).get("metrics") or {}
        except (PermissionError, OSError, json.JSONDecodeError):
            metrics = {}
    try:
        text = llm.chat(prompts.INTERPRET_SYSTEM,
                        prompts.interpret_user("画板电路仿真", netlist,
                                                metrics or {"提示": "未解析到波形数据"}),
                        thinking=False)
    except llm.LLMError as e:
        return {"error": f"大模型不可用：{e}（可在主页 ⚙ 模型 里配置）"}
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


def ai_draw_events(message: str, canvas_netlist: str | None, workspace):
    """AI 画图轮：自然语言 → 修复环管线（生成/改+验证）→ 网表 → 画布。

    previous_netlist 取【画布当前网表】（用户可能改了还没仿真），画布是
    项目电路的编辑视图；验证通过后网表写入项目上下文。事件格式与
    /chat/stream 完全一致（前端同一套 SSE 解析）；done 事件额外携带
    canvas（确定性布局的画布 schema），前端进入预览→确认应用流程。
    落布失败（拓扑终检不过）canvas 为 None，前端降级为只展示证据卡。"""
    from .pipeline import Evidence, iter_pipeline_events

    ws = workspace
    message = message[:8000]

    gen = iter_pipeline_events(message, previous_netlist=canvas_netlist,
                               workspace=ws, source="draw-ai")
    ev: Evidence | None = None
    while True:
        try:
            yield next(gen)
        except StopIteration as s:
            ev = s.value
            break

    canvas = None
    if ev.ok:
        _update_project_netlist(ev.netlist, ws)  # 项目制：AI 轮同样进项目上下文
        try:
            from .canvas_layout import netlist_to_canvas
            canvas = netlist_to_canvas(ev.netlist)
        except Exception:
            canvas = None  # 落布是加分项，失败不阻塞证据卡

    d = ev.to_dict()
    d["canvas"] = canvas
    yield {"type": "done", **d}
