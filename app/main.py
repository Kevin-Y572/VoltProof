"""FastAPI 入口：对话接口 + 同题对照裸调用接口 + 工作区端点 + 静态托管。

启动：uvicorn app.main:app --reload
  http://127.0.0.1:8000            -> static/index.html   一体化工作台（左画板右对话）
  http://127.0.0.1:8000/draw.html  -> 独立电路画板（拖放连线，直仿零 LLM）
  http://127.0.0.1:8000/compare.html -> 同题对照演示页
  POST /chat          {session_id, message} -> 证据包 JSON（仿真在环）
  POST /chat/stream   同 /chat，但以 SSE 逐事件推送修复环过程（前端进度）
  POST /demo/raw      {message}             -> 裸 LLM 回复（对照组，无仿真）
  POST /demo/verified {message}             -> 临时工作区仿真（不改当前项目）
  POST /api/draw/verify   {netlist}         -> 画板网表直仿（安全门+静态检查+ngspice，零 LLM）
  POST /api/draw/interpret {conversation_id} -> AI 解读画板最近一次仿真（只解释不改）

工作区：
  启动时经 VOLTPROOF_WORKSPACE 绑定（缺省 = 仓库 out/），运行中可经
  POST /api/ws/open 切换。agent 的所有产物与会话状态都落在当前工作区内。
  /api/files 只在当前工作区内取文件（路径守卫在 Workspace.resolve）。

安全注意：/api/ws/*、/api/files/*、/api/llm/* 是无鉴权的本地信任端点，
只应绑定 127.0.0.1 单机使用。网表和模型生成的 Python 只做命令/路径/AST
检查，不是操作系统沙箱。说明见 README 的 Notes / 补充说明。
/api/llm/config 接受用户自填的供应商 URL，llm.validate_base_url 做 SSRF 校验
（仅 http/https、拒内网/环回地址）；API Key 只落本机 settings.json、响应脱敏。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import i18n, llm, pipeline, project, specs
from .workspace import Workspace, WorkspaceError, default_workspace

app = FastAPI(title="VoltProof")


@app.middleware("http")
async def _bind_locale(request: Request, call_next):
    lang = i18n.from_request(request)
    token = i18n.use(lang)
    try:
        response = await call_next(request)
        response.headers["Content-Language"] = lang
        return response
    finally:
        i18n.reset(token)

# PCB 能力为可选：路由无条件挂载（capabilities 如实报告工具在场性），
# 工具缺失时相关端点返回 400 带修复指引，不影响仿真/画板主功能。
from .pcb import api as pcb_api  # noqa: E402
app.include_router(pcb_api.router)

_STATIC = Path(__file__).resolve().parent / "static"  # 随 app 包分发（pip 安装也可用）


def _init_workspace() -> Workspace:
    p = os.environ.get("VOLTPROOF_WORKSPACE", "").strip()
    if not p:
        return default_workspace()
    try:
        ws = Workspace(p)
    except WorkspaceError as e:
        raise SystemExit(f"VOLTPROOF_WORKSPACE 无效：{e}")
    if not ws.root.is_dir():
        raise SystemExit(f"VOLTPROOF_WORKSPACE 目录不存在：{ws.root}")
    ws.ensure()
    return ws


# 当前工作区（单活）。切换只影响后续请求；进行中的请求在入口已捕获旧引用。
_active_ws = _init_workspace()
_runs: dict[str, threading.Event] = {}
_runs_lock = threading.Lock()


def _register_run(request_id=None):
    rid = request_id or uuid.uuid4().hex
    if not isinstance(rid, str) or not rid.replace("-", "").isalnum() or len(rid) > 64:
        raise ValueError(i18n.t("api.run_id"))
    with _runs_lock:
        if rid in _runs:
            raise ValueError(i18n.t("api.run_busy"))
        event = _runs[rid] = threading.Event()
    return rid, event


class ChatReq(BaseModel):
    conversation_id: str | None = None
    message: str
    attachment: dict | None = None  # {filename, content}：网表或文本文件
    requirements: list[dict] | None = None
    expected_revision: int | None = None
    request_id: str | None = None
    time_budget: float = Field(default=600, ge=10, le=900)


class RawReq(BaseModel):
    message: str


class WsOpenReq(BaseModel):
    path: str


class LlmConfigReq(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class WhatifReq(BaseModel):
    component: str
    values: list[str]
    metric: str = "vpp"
    out_frag: str = "out"
    in_frag: str = "in"
    expected_revision: int | None = None


class DrawVerifyReq(BaseModel):
    conversation_id: str | None = None  # 兼容保留：项目制下不再使用
    title: str = "未命名电路"
    netlist: str
    requirements: list[dict] | None = None
    expected_revision: int | None = None
    canvas: dict | None = None
    use_current_version: bool = False


class DrawInterpretReq(BaseModel):
    conversation_id: str | None = None  # 兼容保留：项目制下不再使用
    task_id: str | None = None


class DrawAiReq(BaseModel):
    conversation_id: str | None = None  # 兼容保留：项目制下不再使用
    message: str
    canvas_netlist: str | None = None  # 画布当前网表（空画布=生成，非空=修改）
    requirements: list[dict] | None = None
    expected_revision: int | None = None
    request_id: str | None = None
    time_budget: float = Field(default=600, ge=10, le=900)


class ProjectPutReq(BaseModel):
    name: str | None = None
    canvas: dict | None = None    # {comps, wires, labels}
    analysis: dict | None = None  # {type, tstop, fstart, fstop}
    draft_netlist: str | None = Field(default=None, max_length=128 * 1024)
    expected_revision: int | None = None


class VersionReq(BaseModel):
    version_id: str
    expected_revision: int
    requirements: list[dict] | None = None


class RequirementsReq(BaseModel):
    requirements: list[dict]
    expected_revision: int


@app.post("/chat")
def chat(req: ChatReq):
    """产品管线：生成 → 检查 → 仿真 → 重试 → 证据。对话可跨请求续接。"""
    ws = _active_ws  # 入口捕获：切换工作区不影响进行中的请求
    try:
        return pipeline.chat_with_conversation(req.conversation_id, req.message,
                                               attachment=req.attachment, workspace=ws,
                                               requirements=req.requirements, expected_revision=req.expected_revision,
                                               time_budget=req.time_budget)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/chat/stream")
def chat_stream(req: ChatReq, request: Request):
    """SSE 版 /chat：修复环每前进一步推一个事件，最后推 done（完整证据包）。
    事件格式 data: {"type":"stage"|"done"|"error", ...}\\n\\n"""
    ws = _active_ws
    try:
        if req.requirements is not None:
            specs.normalize(req.requirements)
        rid, cancel_event = _register_run(req.request_id)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    lang = i18n.from_request(request)

    def gen():
        token = i18n.use(lang)
        try:
            yield f'data: {json.dumps({"type": "stage", "stage": "start", "request_id": rid, "round": 0, "problems": []})}\n\n'
            for ev in pipeline.chat_with_conversation_events(
                    req.conversation_id, req.message,
                    attachment=req.attachment, workspace=ws, requirements=req.requirements,
                    expected_revision=req.expected_revision, cancelled=cancel_event.is_set,
                    time_budget=req.time_budget):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # 中途异常也要以 error 事件收尾，前端不会挂死
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'
        finally:
            cancel_event.set()
            with _runs_lock:
                _runs.pop(rid, None)
            i18n.reset(token)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.post("/api/runs/{request_id}/cancel")
def cancel_run(request_id: str):
    with _runs_lock:
        event = _runs.get(request_id)
        if event is None:
            return JSONResponse(status_code=404, content={"error": i18n.t("api.run_gone")})
        event.set()
    return {"ok": True, "note": i18n.t("api.run_cancel")}


@app.post("/demo/verified")
def demo_verified(req: RawReq):
    """对照页：在一次性工作区仿真。成功也不采用，当前项目电路保持不变。"""
    import tempfile
    message = (req.message or "").strip()
    if not message:
        return JSONResponse(status_code=400, content={"error": i18n.t("api.compare_empty")})
    try:
        with tempfile.TemporaryDirectory(prefix="voltproof-demo-") as tmp:
            ws = Workspace(Path(tmp))
            ws.ensure()
            result = pipeline.chat_with_conversation(None, message, workspace=ws, adopt=False)
        result["applied"] = False
        result["demo"] = True
        result["note"] = i18n.t("api.demo_note")
        return result
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/demo/raw")
def demo_raw(req: RawReq):
    """对照组：同一个问题直连大模型，不经过任何仿真——用于演示差异。"""
    try:
        text = llm.chat(
            "你是电路设计助手，请直接回答用户的问题。",
            req.message,
        )
        return {"text": text}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# 工作区端点（本地信任，无鉴权——见模块 docstring 安全注意）
# ---------------------------------------------------------------------------

@app.get("/api/ws")
def ws_info():
    ws = _active_ws
    return {
        "root": str(ws.root),
        "is_default": str(ws.root) == str(default_workspace().root),
        "tasks": len(ws.list_tasks(limit=1000)),
    }


@app.post("/api/ws/open")
def ws_open(req: WsOpenReq):
    """切换当前工作区：目录必须已存在（不在服务器上创建用户目录）。"""
    global _active_ws
    p = req.path.strip().strip('"').strip("'")
    if not p:
        return JSONResponse(status_code=400, content={"error": i18n.t("api.path_empty")})
    try:
        ws = Workspace(p)
    except WorkspaceError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not ws.root.is_dir():
        return JSONResponse(status_code=400, content={"error": i18n.t("api.dir_missing", path=ws.root)})
    ws.ensure()
    _active_ws = ws
    return {"root": str(ws.root)}


@app.get("/api/ws/tasks")
def ws_tasks():
    return {"root": str(_active_ws.root), "tasks": _active_ws.list_tasks()}


@app.get("/api/convs")
def convs_list():
    """对话清单（新在前，摘要信息）。"""
    return {"conversations": pipeline.conversation_list(_active_ws)}


@app.get("/api/convs/{cid}")
def conv_detail(cid: str):
    """恢复对话：完整消息流，证据卡从任务档案重建（图片走 /api/files）。"""
    conv = pipeline.restore_conversation(cid, workspace=_active_ws)
    if conv is None:
        return JSONResponse(status_code=404, content={"error": i18n.t("api.conv_missing")})
    return conv


@app.get("/api/files/{task_id}/{name}")
def ws_file(task_id: str, name: str):
    """取当前工作区 docs/<task_id>/ 下的产物文件（经路径守卫）。"""
    ws = _active_ws
    try:
        f = ws.resolve(Path("docs") / task_id / name)
    except PermissionError as e:
        return JSONResponse(status_code=403, content={"error": str(e)})
    if not f.is_file():
        return JSONResponse(status_code=404, content={"error": i18n.t("api.file_missing")})
    return FileResponse(f)


# ---------------------------------------------------------------------------
# 大模型配置端点（本地信任：Key 只存本机 settings.json，响应永远脱敏）
# ---------------------------------------------------------------------------

@app.get("/api/llm/config")
def llm_config():
    """当前生效的供应商/模型配置（Key 脱敏，不回传完整凭据）。"""
    return llm.current_config()


@app.post("/api/llm/config")
def llm_config_set(req: LlmConfigReq):
    """保存供应商 Base URL / API Key / 模型。非法 URL（SSRF 校验）返回 400
    且不改动任何状态。空字段表示该项不变。"""
    try:
        return llm.configure(base_url=req.base_url, api_key=req.api_key,
                             model=req.model)
    except llm.LLMError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/llm/models")
def llm_models(req: LlmConfigReq):
    """按给定（或当前）配置拉取供应商模型列表，供前端下拉选择。
    保存前可先传 base_url/api_key 试连；失败返回 400 与可读原因。"""
    try:
        return {"models": llm.list_models(base_url=req.base_url,
                                          api_key=req.api_key)}
    except llm.LLMError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# 项目（电路唯一权威表示：画布 + 最后验证网表；所有线程/画板共用）
# ---------------------------------------------------------------------------

@app.get("/api/project")
def project_get():
    p = project.ensure_version(_active_ws)
    return {"name": p.get("name") or "",
            "canvas": p.get("canvas"),
            "analysis": p.get("analysis"),
            "updated": p.get("updated") or "",
            "has_netlist": bool(p.get("netlist")), "revision": project.revision(p),
            "version_id": p.get("version_id", ""), "dirty": bool(p.get("dirty")),
            "requirements": p.get("requirements", []), "evidence_id": p.get("evidence_id", ""),
            "netlist_hash": p.get("netlist_hash", "")}


@app.get("/api/project/versions")
def project_versions():
    project.ensure_version(_active_ws)
    return {"versions": project.list_versions(_active_ws)}


@app.get("/api/project/compare")
def project_compare(left_id: str, right_id: str):
    try:
        return project.compare(_active_ws, left_id, right_id)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error":str(e)})


@app.put("/api/project/requirements")
def project_requirements(req: RequirementsReq):
    ws = _active_ws
    try:
        requirements = specs.normalize(req.requirements)
        with project.lock(ws):
            p = ws.load_project()
            if project.revision(p) != req.expected_revision:
                raise project.Conflict(i18n.t("api.req_stale"))
            if requirements != p.get("requirements", []):
                p.update(requirements=requirements, dirty=bool(p.get("netlist") or p.get("dirty")),
                         requirements_pending=True,
                         draft_netlist=p.get("draft_netlist") or p.get("netlist"),
                         revision=project.revision(p) + 1)
                ws.save_project(p)
        return {"ok": True, "revision": project.revision(p)}
    except project.Conflict as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/project/apply")
def project_apply(req: VersionReq):
    try:
        p = project.apply(_active_ws, req.version_id, req.expected_revision)
        return {"ok": True, "revision": project.revision(p), "version_id": p["version_id"]}
    except project.Conflict as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/project/restore")
def project_restore(req: VersionReq):
    try:
        p = project.restore(_active_ws, req.version_id, req.expected_revision,
                            specs.normalize(req.requirements) if req.requirements is not None else None)
        return {"ok": True, "revision": project.revision(p), "version_id": p["version_id"]}
    except project.Conflict as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.put("/api/project")
def project_put(req: ProjectPutReq):
    """画板编辑防抖保存。轻校验（三数组、坐标钳到画布内、总体积上限），
    校验不过 400 且不落盘——绝不写坏 project.json。"""
    try:
        payload = json.dumps(req.canvas or {}, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return JSONResponse(status_code=400, content={"error": i18n.t("api.canvas_ser", msg=e)})
    if len(payload) > 256 * 1024:
        return JSONResponse(status_code=400, content={"error": i18n.t("api.canvas_big")})
    canvas = req.canvas
    if canvas is not None:
        if not isinstance(canvas, dict) or not all(
                isinstance(canvas.get(k), list) for k in ("comps", "wires", "labels")):
            return JSONResponse(status_code=400,
                                content={"error": i18n.t("api.canvas_shape")})
        for c in canvas["comps"]:
            if not isinstance(c, dict):
                return JSONResponse(status_code=400, content={"error": i18n.t("api.comp_shape")})
            try:
                c["x"] = max(0, min(50, int(c.get("x") or 0)))
                c["y"] = max(0, min(35, int(c.get("y") or 0)))
                c["rot"] = max(0, min(3, int(c.get("rot") or 0)))
            except (TypeError, ValueError, OverflowError):
                return JSONResponse(status_code=400, content={"error": i18n.t("api.comp_coord")})
    analysis = ({k: str(v)[:16] for k, v in req.analysis.items()
                 if k in ("type", "tstop", "fstart", "fstop")} if req.analysis is not None else None)
    try:
        p = project.update_draft(_active_ws, name=req.name, canvas=canvas, analysis=analysis,
                                 draft_netlist=req.draft_netlist, expected=req.expected_revision)
        return {"ok": True, "updated": p.get("updated"), "revision": project.revision(p), "dirty": bool(p.get("dirty"))}
    except project.Conflict as e:
        return JSONResponse(status_code=409, content={"error": str(e)})


# ---------------------------------------------------------------------------
# what-if 参数扫描（本地信任，纯确定性：零 LLM，逐值真实仿真；项目电路）
# ---------------------------------------------------------------------------

@app.get("/api/whatif/components")
def whatif_components():
    """项目当前电路里可扫描的元件清单（R/C/L 与直流源）。"""
    ws = _active_ws
    p = ws.load_project()
    try:
        netlist = project.active_netlist(p, verified=True)
    except project.Conflict as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    if not netlist:
        return JSONResponse(status_code=400,
                            content={"error": i18n.t("api.whatif_none")})
    from .whatif import scan_components
    return {"components": scan_components(netlist), "revision": project.revision(p), "version_id": p.get("version_id")}


@app.post("/api/whatif")
def whatif_run(req: WhatifReq):
    """对项目电路的指定元件逐值改写并真实仿真，返回逐点指标 + 曲线族图。"""
    ws = _active_ws
    p = ws.load_project()
    try:
        netlist = project.active_netlist(p, verified=True)
        if req.expected_revision is not None and req.expected_revision != project.revision(p):
            raise project.Conflict(i18n.t("api.whatif_rev"))
    except project.Conflict as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    if not netlist:
        return JSONResponse(status_code=400,
                            content={"error": i18n.t("api.whatif_none_short")})
    from . import whatif
    tid = ws.new_task_id()
    try:
        d = whatif.run_scan(netlist, req.component, req.values,
                               req.metric, req.out_frag, req.in_frag,
                               workdir=ws.task_sim_dir(tid), requirements=p.get("requirements", []))
        if d.get("error"):
            return JSONResponse(status_code=400, content=d)
        for i, point in enumerate(d.get("points", [])):
            if not point.get("ok") or point.get("passed") is False:
                continue
            point_id = f"{tid}-p{i + 1}"
            nl = (ws.task_sim_dir(tid) / f"point-{i + 1}" / "circuit.cir").read_text(encoding="utf-8")
            ev = pipeline.Evidence(ok=True, request=i18n.t_zh("timeline.whatif.point", comp=req.component, value=point["value"]), netlist=nl,
                source="whatif-point", task_id=point_id, requirements=p.get("requirements", []),
                metrics=point.get("metrics", {}), checks=point.get("checks", []),
                verdict="passed" if point.get("checks") else "simulated",
                validation={"static":"passed", "simulation":"passed", "measurement":"passed", "requirements":"passed" if point.get("checks") else "not_requested"},
                provenance={"netlist_hash":project.digest(nl), "measurement_version":specs.VERSION,
                            "base_version":p.get("version_id"), "raw_file":str(Path("sim") / tid / f"point-{i + 1}" / "out.raw")})
            canvas = whatif.canvas_with_component_value(p.get("canvas"), req.component, point["value"])
            if canvas is None:
                from .canvas_layout import netlist_to_canvas
                canvas = netlist_to_canvas(nl)
            v = project.candidate(ws, ev.to_dict(), canvas, project.revision(p))
            ev.version_id = v["id"]
            pipeline._write_evidence_file(ev, ws.task_docs_dir(point_id))
            from . import measure
            raw=ws.task_sim_dir(tid) / f"point-{i + 1}" / "out.raw"
            if raw.is_file():
                measure.save_trace_preview(measure.load_traces(raw), ws.task_docs_dir(point_id) / "wave.json")
            point["candidate_id"] = v["id"]
        d["recommended_index"] = next((i for i, point in enumerate(d.get("points", [])) if point.get("passed") is True), None)
        d.update(version_id=p.get("version_id"), base_revision=project.revision(p), source="whatif", task_id=tid)
        scan_ev = pipeline.Evidence(ok=True, request=i18n.t_zh("timeline.whatif.scan", comp=req.component, metric=whatif.metric_label(req.metric, lang="zh-CN")),
                                    source="whatif", task_id=tid, netlist=netlist, verdict="simulated")
        pipeline._write_evidence_file(scan_ev, ws.task_docs_dir(tid))
        f = ws.task_docs_dir(tid) / "scan.json"
        f.write_text(json.dumps({k: v for k, v in d.items() if k != "png_b64"}, ensure_ascii=False, indent=1), encoding="utf-8")
        plot = ws.task_sim_dir(tid) / "whatif.png"
        if plot.is_file():
            import shutil
            shutil.copyfile(plot, f.parent / "whatif.png")
        return d
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ---------------------------------------------------------------------------
# 画板端点（本地信任；确定性直仿零 LLM，AI 解读显式触发且只解释不改电路）
# ---------------------------------------------------------------------------

@app.post("/api/draw/verify")
def draw_verify(req: DrawVerifyReq):
    """画板网表直仿：静态检查 → ngspice → 波形/指标/原理图。不进 LLM 修复环。"""
    from . import draw_api
    ws = _active_ws
    try:
        netlist = req.netlist
        if req.use_current_version:
            p = ws.load_project()
            if req.expected_revision != project.revision(p):
                raise project.Conflict(i18n.t("api.sim_rev"))
            netlist = project.active_netlist(p, verified=True)
        return draw_api.verify_drawn(netlist, req.title.strip() or i18n.t("untitled"),
                                     ws, requirements=req.requirements,
                                     expected_revision=req.expected_revision, canvas=req.canvas)
    except project.Conflict as e:
        return JSONResponse(status_code=409, content={"error":str(e)})
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error":str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/draw/interpret")
def draw_interpret(req: DrawInterpretReq):
    """AI 解读项目电路：LLM 只解释最近一次仿真结果，不修改电路。"""
    from . import draw_api
    try:
        return draw_api.interpret_drawn(_active_ws, req.task_id)
    except llm.LLMError as e:
        return JSONResponse(status_code=400, content={"error": i18n.t("api.llm_down", msg=e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/draw/ai/stream")
def draw_ai_stream(req: DrawAiReq, request: Request):
    """AI 画图轮（SSE）：自然语言 → 生成/修改网表 → 修复环验证 → 网表确定性
    布局成画布 schema。事件格式与 /chat/stream 一致；done 额外带 canvas。"""
    from . import draw_api
    ws = _active_ws
    try:
        if req.requirements is not None:
            specs.normalize(req.requirements)
        rid, cancel_event = _register_run(req.request_id)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    lang = i18n.from_request(request)

    def gen():
        token = i18n.use(lang)
        try:
            for ev in draw_api.ai_draw_events(req.message, req.canvas_netlist, ws,
                    requirements=req.requirements, expected_revision=req.expected_revision,
                    cancelled=cancel_event.is_set, time_budget=req.time_budget):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # 中途异常也要以 error 事件收尾，前端不会挂死
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'
        finally:
            cancel_event.set()
            with _runs_lock:
                _runs.pop(rid, None)
            i18n.reset(token)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/api/report/{cid}")
def report(cid: str):
    """实验报告：对话 → 单文件 HTML（图片内嵌），浏览器打印即 PDF。"""
    conv = pipeline.find_conversation(cid, workspace=_active_ws)
    if conv is None:
        return JSONResponse(status_code=404, content={"error": i18n.t("api.conv_missing")})
    from .report import build_report_html
    return HTMLResponse(content=build_report_html(conv, _active_ws),
                        headers={"Content-Disposition":
                                 f'inline; filename="voltproof-report-{cid}.html"'})


@app.get("/api/project/report")
def project_report(version_id: str | None = None):
    from .report import build_project_report
    try:
        return HTMLResponse(build_project_report(_active_ws, version_id))
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error":str(e)})


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


# 静态托管放在最后：/chat、/api/* 等显式路由优先生效
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")
