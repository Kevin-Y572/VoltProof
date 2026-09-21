"""SKiDL 代码 → SPICE 网表的受限执行构建器。  [双轨 backend="skidl"]

动机（2026-09-19 综合实验结论）：LLM 写裸 SPICE 网表的失败大多来自方言陷阱
（浮空节点/单位/续行/行首字母/接线断链），事后靠 ngspice 报错回喂补救。
SKiDL 是 MIT 的 Python"电路即代码"库：连接显式、数值就是数值，无格式陷阱。
实测 skidl 2.3.0 的 skidl.pyspice 原语**不加载 PySpice**（GPL 红线无忧）。

安全（同 render_schematic 的三层受限措施）：
  1. AST 白名单：只允许 import skidl / skidl.pyspice / math
  2. 独立子进程 + python -I + 超时
  3. 失败返回 None + 报错文本（回喂修复环）

代码末尾 print 协议（由提示词约定，builder 据此拼接固定格式的 .control）：
  print("ANALYSIS: tran 10u 10m")        # .control 里的分析命令（可多行 \\n 分隔）
  print("OUT_NODES: v(out1k) v(out3k)")  # write 的信号，必须是真实节点
  print("EXTRA: .model mynpn NPN(beta=100)")  # 追加到网表的原生指令（可多行）

实测 quirk（写进 few-shot，防 LLM 踩坑）：
  - V 原语网表模板固定加 DC 前缀：value="12"→"DC 12"✓；瞬态写
    value="0 SIN(0 2.5 1k)"→"DC 0 SIN(...)"✓；交流 value="AC 1"→"DC AC 1"✓
    （ngspice 均接受）；value 不得以 SIN/PULSE 等关键字直接开头。
  - 默认输出文件是 cwd 下的 skidl.net。
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .sandbox import ast_check, minimal_env

_ALLOWED_MODULES = {"skidl", "skidl.pyspice", "math"}

_CONTROL_TMPL = """\
.control
set filetype=ascii
{analysis}
write out.raw {outs}
.endc"""


def _ast_check(code: str) -> bool:
    return ast_check(code, _ALLOWED_MODULES)


def _parse_protocol(stdout: str) -> tuple[str, list[str], list[str]]:
    """解析 print 协议：(analysis, out_nodes, extras)。缺省值兜底。"""
    analysis, outs, extras = "", [], []
    for ln in stdout.splitlines():
        if ln.startswith("ANALYSIS:"):
            analysis = ln.split(":", 1)[1].strip()
        elif ln.startswith("OUT_NODES:"):
            outs = ln.split(":", 1)[1].split()
        elif ln.startswith("EXTRA:"):
            extras.append(ln.split(":", 1)[1].strip())
    if not analysis:
        analysis = "tran 10u 10m"
    if not outs:
        outs = ["v(1)"]  # 至少写一路，raw 才会存在
    return analysis, outs, extras


def build_netlist(code: str, timeout: int = 60) -> tuple[str | None, str]:
    """执行 SKiDL 代码，返回 (拼接好的完整网表, 错误信息)。成功时错误为空串。"""
    if not _ast_check(code):
        return None, "代码未通过 AST 白名单校验（只允许 import skidl/skidl.pyspice/math）"
    with tempfile.TemporaryDirectory(prefix="cp_skidl_") as td:
        tdp = Path(td)
        (tdp / "build.py").write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(tdp / "build.py")],
                cwd=tdp, capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace", env=minimal_env(),
            )
        except subprocess.TimeoutExpired:
            return None, f"SKiDL 代码执行超过 {timeout}s 超时"
        nets = sorted(tdp.glob("*.net"), key=lambda p: p.stat().st_mtime)
        if proc.returncode != 0 or not nets:
            err = "\n".join(l for l in proc.stderr.splitlines() if "WARNING" not in l)
            return None, err[-1500:] or "SKiDL 执行失败且无报错输出"
        netlist = nets[-1].read_text(encoding="utf-8", errors="replace").strip()
        if not re.search(r"^[A-Za-z]", netlist, re.MULTILINE):
            return None, "生成的网表没有元件行（电路为空）"

        analysis, outs, extras = _parse_protocol(proc.stdout)
        parts = [netlist]
        parts += [e for e in extras if e]
        parts.append(_CONTROL_TMPL.format(analysis=analysis, outs=" ".join(outs)))
        parts.append(".end")
        return "\n\n".join(parts) + "\n", ""
