"""FastAPI 入口：对话接口 + 同题对照裸调用接口 + 工作区端点 + 静态托管。

启动：uvicorn app.main:app --reload
  http://127.0.0.1:8000            -> static/index.html   产品对话页
  http://127.0.0.1:8000/compare.html -> 同题对照演示页
  POST /chat      {session_id, message} -> 证据包 JSON（仿真在环）
  POST /demo/raw  {message}             -> 裸 LLM 回复（对照组，无仿真）

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

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import llm, pipeline
from .workspace import Workspace, WorkspaceError, default_workspace

app = FastAPI(title="VoltProof")

_STATIC = Path(__file__).resolve().parent.parent / "static"


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
    session_id: str | None = None
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


@app.post("/chat")
def chat(req: ChatReq):
    """产品管线：生成 → 检查 → 仿真 → 重试 → 证据。"""
    ws = _active_ws  # 入口捕获：切换工作区不影响进行中的请求
    sid = req.session_id or ""
    try:
        data = pipeline.chat_with_session(sid, req.message,
                                          attachment=req.attachment, workspace=ws)
        data["session_id"] = sid or data.get("session_id")
        return data
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


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


# 静态托管放在最后：/chat、/api/* 等显式路由优先生效
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")


# TODO(W3): /chat 改 SSE 流式（边重试边推送日志，演示"AI 被仿真器打回"的过程，观感拉满）
