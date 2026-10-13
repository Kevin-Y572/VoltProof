"""MCP Server 协议测试：真实子进程走一遍 initialize → tools/list → tools/call。

tools/call 用会失败的网表触发 isError 路径——全程零 API Key、零 LLM 调用，
验证的是协议握手与工具分发，不是电路质量（那由 bench 覆盖）。
运行：python tests/test_mcp.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


BAD_NETLIST = "* broken on purpose\nV1 in gndx DC 5\nR1 in out 1k\n.control\nop\nwrite out.raw v(out)\n.endc\n.end\n"


def main() -> int:
    import os
    import tempfile
    # 剥掉一切凭据（否则宿主环境的 API Key 会让修复环真实调用 LLM，
    # 测试结果随环境漂移）；ngspice 路径保留
    env = {k: v for k, v in os.environ.items()
           if "API_KEY" not in k and k != "VOLTPROOF_SETTINGS"}
    env["VOLTPROOF_SETTINGS"] = str(Path(tempfile.mkdtemp(prefix="cp_mcp_test_")) / "s.json")
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.mcp_server"], cwd=str(REPO),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env)

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def recv() -> dict:
        return json.loads(proc.stdout.readline())

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05"}})
    r = recv()
    check("MCP: initialize 握手", r.get("id") == 1
          and r["result"]["serverInfo"]["name"] == "voltproof"
          and "tools" in r["result"]["capabilities"])

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    r = recv()
    names = [t["name"] for t in r["result"]["tools"]]
    check("MCP: tools/list 暴露两个工具", r.get("id") == 2
          and names == ["design_circuit", "verify_netlist"], str(names))

    send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
          "params": {"name": "verify_netlist", "arguments": {"netlist": BAD_NETLIST}}})
    r = recv()
    text = r["result"]["content"][0]["text"]
    # 无 API Key 时修复环第一步就 LLMError → isError 结果（协议正确性即测试目标）
    check("MCP: verify_netlist 返回 isError 与可读原因", r.get("id") == 3
          and r["result"].get("isError") is True and "工具调用失败" in text,
          text[:120])

    send({"jsonrpc": "2.0", "id": 4, "method": "no/such/method"})
    r = recv()
    check("MCP: 未知方法返回 -32601", r.get("id") == 4
          and r["error"]["code"] == -32601)

    proc.stdin.close()
    proc.wait(timeout=30)
    print(f"\n{'全部通过' if not FAILURES else '失败: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
