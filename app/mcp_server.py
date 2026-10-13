"""VoltProof MCP Server（stdio）——把"仿真在环"管线暴露为编程 agent 的工具。

适用 Claude Desktop / Cursor / 任何 MCP 客户端。配置示例：

```json
{
  "mcpServers": {
    "voltproof": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "<VoltProof 仓库或便携包根目录>"
    }
  }
}
```

协议实现是手写的最小 stdio JSON-RPC（MCP 规范：每行一个 JSON 消息），
支持 initialize / notifications/* / ping / tools/list / tools/call——
刻意不引入 mcp SDK 依赖：便携发行版零新依赖，且核心就这一个接口面。

工具：
  design_circuit(request, backend?)  自然语言 → 生成网表 → 仿真验证 → 实测指标
  verify_netlist(netlist)            外部网表 → 静态检查 → 仿真 → 实测指标（失败自动修复）
返回文本里的所有数字来自 ngspice 实测，不由模型自报。
"""

from __future__ import annotations

import json
import sys

from .pipeline import run_pipeline

_TOOLS = [
    {
        "name": "design_circuit",
        "description": (
            "按自然语言电路需求设计电路并经 ngspice 真实仿真验证。"
            "返回判定、实测指标（峰峰值/均值/主频等，来自仿真器而非模型自报）、"
            "完整网表与产物目录。指标不达标会自动带报错修复重试。"
            "适用：模拟电路（滤波/放大/整流/振荡/传感器调理）；数字 RTL 不支持。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {"type": "string",
                            "description": "电路需求，例如：设计截止频率1kHz的RC低通滤波器并验证"},
                "backend": {"type": "string", "enum": ["spice", "skidl"],
                            "description": "spice=直接写网表（默认）；skidl=写SKiDL代码构建网表"},
            },
            "required": ["request"],
        },
    },
    {
        "name": "verify_netlist",
        "description": (
            "验证一份 SPICE 网表：静态检查 → ngspice 真实仿真 → 实测指标；"
            "失败时自动修复并返回修复后的完整网表。适合检查别处生成的电路答案。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "netlist": {"type": "string", "description": "完整 SPICE 网表文本"},
            },
            "required": ["netlist"],
        },
    },
]


def _fmt_evidence(ev) -> str:
    verdict = "✅ 仿真验证通过" if ev.ok else "⚠️ 自动修复后仍未通过"
    lines = [verdict, f"需求：{ev.request}", f"耗时：{ev.elapsed:.1f}s"]
    if ev.metrics:
        lines.append("实测指标（ngspice 实测）：")
        lines += [f"  - {k} = {v}" for k, v in list(ev.metrics.items())[:12]]
    if ev.retry_log:
        n = len([r for r in ev.retry_log if r.get("problems")])
        lines.append(f"修复环：{n} 轮（每轮报错已回喂修正）")
    if ev.netlist:
        lines.append("网表：")
        lines.append("```spice")
        lines.append(ev.netlist)
        lines.append("```")
    if ev.task_dir:
        lines.append(f"产物目录（波形/原理图/证据 JSON）：{ev.task_dir}")
    return "\n".join(lines)


def _dispatch(name: str, args: dict) -> str:
    if name == "design_circuit":
        ev = run_pipeline(args["request"], backend=args.get("backend", "spice"))
        return _fmt_evidence(ev)
    if name == "verify_netlist":
        ev = run_pipeline("仿真验证用户提供的网表并给出实测指标",
                          initial_netlist=args["netlist"])
        return _fmt_evidence(ev)
    raise ValueError(f"unknown tool: {name}")


def _handle(msg: dict) -> dict | None:
    """返回响应 dict；通知类（notifications/*）与未知无 id 消息返回 None。"""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": msg.get("params", {}).get(
                "protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "voltproof", "version": "0.2.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            text = _dispatch(params.get("name", ""), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": text}]}}
        except Exception as e:
            # 工具内部失败按协议以 isError 结果返回（不是 JSON-RPC error）
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": f"工具调用失败: {e}"}],
                "isError": True}}
    if msg_id is not None:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg) if isinstance(msg, dict) else None
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
