"""FastAPI 入口：对话接口 + 同题对照裸调用接口 + 静态托管。  [W3]

启动：uvicorn app.main:app --reload
  http://127.0.0.1:8000            -> static/index.html   产品对话页
  http://127.0.0.1:8000/compare.html -> 同题对照演示页
  POST /chat      {session_id, message} -> 证据包 JSON（仿真在环）
  POST /demo/raw  {message}             -> 裸 LLM 回复（对照组，无仿真）
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import llm, pipeline

app = FastAPI(title="CircuitPilot")

_STATIC = Path(__file__).resolve().parent.parent / "static"


class ChatReq(BaseModel):
    session_id: str | None = None
    message: str


class RawReq(BaseModel):
    message: str


@app.post("/chat")
def chat(req: ChatReq):
    """产品管线：生成 → 检查 → 仿真 → 重试 → 证据。"""
    sid = req.session_id or ""
    try:
        data = pipeline.chat_with_session(sid, req.message)
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


@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


# 静态托管放在最后：/chat、/demo/raw 等显式路由优先生效
from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")


# TODO(W3): /chat 改 SSE 流式（边重试边推送日志，演示"AI 被仿真器打回"的过程，观感拉满）
