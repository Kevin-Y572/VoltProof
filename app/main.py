"""FastAPI 入口：对话接口 + 同题对照裸调用接口 + 工作区端点 + 静态托管。

启动：uvicorn app.main:app --reload
  http://127.0.0.1:8000            -> static/index.html   产品对话页
  http://127.0.0.1:8000/compare.html -> 同题对照演示页
  POST /chat          {session_id, message} -> 证据包 JSON（仿真在环）
  POST /chat/stream   同 /chat，但以 SSE 逐事件推送修复环过程（前端进度）
  POST /demo/raw      {message}             -> 裸 LLM 回复（对照组，无仿真）

工作区：
  启动时经 VOLTPROOF_WORKSPACE 绑定（缺省 = 仓库 out/），运行中可经
  POST /api/ws/open 切换。agent 的所有产物与会话状态都落在当前工作区内。
  /api/files 只在当前工作区内取文件（路径守卫在 Workspace.resolve）。

安全注意：/api/ws/*、/api/files/*、/api/llm/* 是无鉴权的本地信任端点，
只应绑定 127.0.0.1 单机使用——公网部署前必须加鉴权（见 README 安全章节）。
/api/llm/config 接受用户自填的供应商 URL，llm.validate_base_url 做 SSRF 校验
（仅 http/https、拒内网/环回地址）；API Key 只落本机 settings.json、响应脱敏。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import llm, pipeline
from .workspace import Workspace, WorkspaceError, default_workspace

app = FastAPI(title="VoltProof")

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


class ChatReq(BaseModel):
    conversation_id: str | None = None
    message: str
    attachment: dict | None = None  # {filename, content}：网表或文本文件


class RawReq(BaseModel):
    message: str


class WsOpenReq(BaseModel):
    path: str


class LlmConfigReq(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


class WhatifReq(BaseModel):
    conversation_id: str
    component: str
    values: list[str]
    metric: str = "vpp"
    out_frag: str = "out"
    in_frag: str = "in"


@app.post("/chat")
def chat(req: ChatReq):
    """产品管线：生成 → 检查 → 仿真 → 重试 → 证据。对话可跨请求续接。"""
    ws = _active_ws  # 入口捕获：切换工作区不影响进行中的请求
    try:
        return pipeline.chat_with_conversation(req.conversation_id, req.message,
                                               attachment=req.attachment, workspace=ws)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/chat/stream")
def chat_stream(req: ChatReq):
    """SSE 版 /chat：修复环每前进一步推一个事件，最后推 done（完整证据包）。
    事件格式 data: {"type":"stage"|"done"|"error", ...}\\n\\n"""
    ws = _active_ws

    def gen():
        try:
            for ev in pipeline.chat_with_conversation_events(
                    req.conversation_id, req.message,
                    attachment=req.attachment, workspace=ws):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # 中途异常也要以 error 事件收尾，前端不会挂死
            yield f'data: {json.dumps({"type": "error", "error": str(e)}, ensure_ascii=False)}\n\n'

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


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
        return JSONResponse(status_code=400, content={"error": "path 不能为空"})
    try:
        ws = Workspace(p)
    except WorkspaceError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    if not ws.root.is_dir():
        return JSONResponse(status_code=400, content={"error": f"目录不存在：{ws.root}"})
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
        return JSONResponse(status_code=404, content={"error": "对话不存在"})
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
        return JSONResponse(status_code=404, content={"error": "文件不存在"})
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
# what-if 参数扫描（本地信任，纯确定性：零 LLM，逐值真实仿真）
# ---------------------------------------------------------------------------

@app.get("/api/whatif/components")
def whatif_components(conversation_id: str):
    """当前对话已验证电路里可扫描的元件清单（R/C/L 与直流源）。"""
    conv = pipeline.find_conversation(conversation_id, workspace=_active_ws)
    if not conv or not conv.get("netlist"):
        return JSONResponse(status_code=400,
                            content={"error": "当前对话还没有验证通过的电路——先设计一个再扫描"})
    from .whatif import scan_components
    return {"components": scan_components(conv["netlist"])}


@app.post("/api/whatif")
def whatif_run(req: WhatifReq):
    """对指定元件逐值改写并真实仿真，返回逐点指标 + 曲线族图。"""
    conv = pipeline.find_conversation(req.conversation_id, workspace=_active_ws)
    if not conv or not conv.get("netlist"):
        return JSONResponse(status_code=400,
                            content={"error": "当前对话还没有验证通过的电路"})
    from . import whatif
    tid = _active_ws.new_task_id()
    try:
        return whatif.run_scan(conv["netlist"], req.component, req.values,
                               req.metric, req.out_frag, req.in_frag,
                               workdir=_active_ws.task_sim_dir(tid))
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/report/{cid}")
def report(cid: str):
    """实验报告：对话 → 单文件 HTML（图片内嵌），浏览器打印即 PDF。"""
    conv = pipeline.find_conversation(cid, workspace=_active_ws)
    if conv is None:
        return JSONResponse(status_code=404, content={"error": "对话不存在"})
    from .report import build_report_html
    return HTMLResponse(content=build_report_html(conv, _active_ws),
                        headers={"Content-Disposition":
                                 f'inline; filename="voltproof-report-{cid}.html"'})


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


# 静态托管放在最后：/chat、/api/* 等显式路由优先生效
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")
