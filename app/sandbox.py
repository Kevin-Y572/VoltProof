"""LLM 生成代码的沙箱公共层（schemdraw / SKiDL 共用）。

安全审查发现：原 AST 检查只拦 os/sys 直属属性与 open/eval 等名字，
`catch_warnings → __builtins__` dunder 链完全绕过（PoC 实测从"沙箱"内
读出了环境变量里的 API Key）。加固两层：
  1. AST：拦截逃逸常用的 dunder 属性（__class__/__subclasses__/__globals__/
     __builtins__/__code__/__loader__ 等——正常绘图代码不需要它们）
  2. 子进程最小环境：只传系统运行必需变量，绝不携带任何 *_API_KEY/SECRET/
     TOKEN——即使出现新的逃逸路径，偷不到凭据（纵深防御）
仍非 OS 级隔离：公网部署前应加容器/AppContainer（见 README 安全章节）。
"""

from __future__ import annotations

import ast
import os

# 逃逸链必需的属性名（正常 schemdraw/SKiDL 绘图代码不出现）
_DUNDER_DENY = {
    "__class__", "__base__", "__bases__", "__mro__", "__subclasses__",
    "__globals__", "__builtins__", "__import__", "__code__", "__closure__",
    "__loader__", "__spec__", "__dict__", "__self__", "__func__",
    "f_globals", "f_locals", "f_builtins", "gi_frame", "cr_frame",
}

_FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals",
                    "locals", "vars", "getattr", "setattr", "delattr",
                    "breakpoint", "input"}


def ast_check(code: str, allowed_modules: set[str]) -> bool:
    """AST 白名单校验：import 白名单 + 危险名字 + dunder 逃逸属性。"""
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
            if node.id in _FORBIDDEN_NAMES:
                return False
        elif isinstance(node, ast.Attribute):
            if node.attr in _DUNDER_DENY:
                return False
            if isinstance(node.value, ast.Name) and node.value.id in ("os", "sys", "subprocess"):
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
