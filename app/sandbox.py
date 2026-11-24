"""LLM 生成代码的检查（schemdraw / SKiDL 共用）。

这不是操作系统沙箱。实际限制只有两层：
  1. AST：import 必须在白名单内；拒绝 open/eval 等危险名字；
     拒绝一切以双下划线开头的属性访问（含 __getattribute__ 这类内省入口），
     以及帧对象上的 f_globals / gi_frame 等非 dunder 内省属性。
  2. 子进程最小环境：只保留运行所需变量，不携带 *_API_KEY / SECRET / TOKEN。
正常的 schemdraw / SKiDL 代码不需要访问 dunder 属性。
进程隔离、seccomp 或容器不在这里。说明见 README 的 Notes / 补充说明。
"""

from __future__ import annotations

import ast
import os

# 帧 / 生成器 / 协程上的内省属性。它们不是 dunder，但能走到 globals 与 builtins。
_INTROSPECTION_ATTRS = {
    "f_globals", "f_locals", "f_builtins", "f_code", "f_back", "f_trace",
    "gi_frame", "gi_code", "gi_yieldfrom",
    "cr_frame", "cr_code", "cr_origin",
    "ag_frame", "ag_code",
    "tb_frame", "tb_next", "tb_lasti",
    "func_globals", "func_code", "func_closure",
}

_FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals",
                    "locals", "vars", "getattr", "setattr", "delattr",
                    "breakpoint", "input"}


def _is_dunder(name: str) -> bool:
    """双下划线开头的名字一律视为内省入口（__class__、__getattribute__ 等）。"""
    return name.startswith("__")


def ast_check(code: str, allowed_modules: set[str]) -> bool:
    """AST 白名单校验。语法错误、越权 import、危险名字或 dunder 属性一律拒绝。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name not in allowed_modules for a in node.names):
                return False
        elif isinstance(node, ast.ImportFrom):
            if node.module not in allowed_modules:
                return False
        elif isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES or _is_dunder(node.id):
                return False
        elif isinstance(node, ast.Attribute):
            if _is_dunder(node.attr) or node.attr in _INTROSPECTION_ATTRS:
                return False
            if isinstance(node.value, ast.Name) and node.value.id in (
                    "os", "sys", "subprocess", "builtins", "importlib"):
                return False
    return True


# 子进程最小环境：Windows Python/matplotlib 运行必需 + 绘图后端。
# 出于纵深防御不携带任何凭据类变量。
_ENV_KEYS = ("SYSTEMROOT", "TEMP", "TMP", "MPLBACKEND", "PATHEXT",
             "USERPROFILE", "LOCALAPPDATA", "PROGRAMDATA")


def minimal_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_KEYS if k in os.environ}
    env.setdefault("MPLBACKEND", "Agg")
    return env
