"""schemdraw 电路图渲染：LLM 生成 schemdraw 代码 → 受限执行 → PNG。  [W4，可裁剪]

安全注意：执行 LLM 生成的 Python 代码有风险。受限措施：
  - 只允许 import schemdraw / schemdraw.elements / math
  - 禁用 open/exec/eval/__import__/os/sys/subprocess 等（AST 白名单校验）
  - 独立子进程 + 超时，失败直接降级（返回 None，前端显示网表）
"""

from __future__ import annotations

import ast
from pathlib import Path

_ALLOWED_MODULES = {"schemdraw", "schemdraw.elements", "math"}
_FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars"}


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
        elif isinstance(node, (ast.Attribute,)):
            if isinstance(node.value, ast.Name) and node.value.id in ("os", "sys", "subprocess"):
                return False
    return True


def render_schematic(code: str, out_png: str | Path, timeout: int = 20) -> Path | None:
    """返回 PNG 路径；校验或执行失败返回 None（调用方降级显示网表）。"""
    if not _ast_check(code):
        return None
    # TODO(W4): 子进程执行 + 超时 + 工作目录隔离；成功后把 PNG 迁移到目标路径
    raise NotImplementedError("W4 任务：见路线图 2.4")
