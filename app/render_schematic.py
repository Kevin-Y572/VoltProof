"""schemdraw 电路图渲染：LLM 生成 schemdraw 代码 → 受限执行 → PNG。  [W4，可裁剪]

安全注意：执行 LLM 生成的 Python 代码有风险。受限措施（三层）：
  1. AST 白名单校验：只允许 import schemdraw / schemdraw.elements / math，
     禁用 open/exec/eval/__import__/os/sys/subprocess 等名字
  2. 独立子进程 + python -I 隔离模式（忽略环境变量/用户 site）+ 20s 超时
  3. 任何失败（校验不过/执行报错/无产物）返回 None，前端降级显示格式化网表

局限（Phase 0 接受）：AST 校验挡不住刻意构造的逃逸；真实防线是它只跑在
演示服务器上、无敏感凭据，且输出必须是 schematic.png 才会被采用。
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ALLOWED_MODULES = {"schemdraw", "schemdraw.elements", "math"}
_FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals",
                    "vars", "getattr", "setattr", "delattr", "breakpoint", "input"}


def _ast_check(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name not in _ALLOWED_MODULES for a in node.names):
                return False
        elif isinstance(node, ast.ImportFrom):
            if node.module not in _ALLOWED_MODULES:
                return False
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES:
                return False
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in ("os", "sys", "subprocess"):
                return False
    return True


def render_schematic(code: str, out_png: str | Path, timeout: int = 20) -> Path | None:
    """返回 PNG 路径；校验或执行失败返回 None（调用方降级显示网表）。"""
    if not _ast_check(code):
        return None
    out_png = Path(out_png)
    with tempfile.TemporaryDirectory(prefix="cp_sch_") as td:
        tdp = Path(td)
        script = tdp / "schem.py"
        script.write_text(code, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script)],
                cwd=tdp, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return None
        png = tdp / "schematic.png"
        if proc.returncode == 0 and png.exists() and png.stat().st_size > 0:
            out_png.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(png, out_png)
            return out_png
    return None
