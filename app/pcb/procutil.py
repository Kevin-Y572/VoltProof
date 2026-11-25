"""子进程启动参数。Windows 隐藏控制台；其它平台不传 Windows 专用标志。"""
from __future__ import annotations

import subprocess
import sys


def no_window_kwargs() -> dict:
    """交给 ``subprocess`` 的额外关键字。

    Windows 用 ``CREATE_NO_WINDOW`` 避免弹出黑色控制台。
    Linux 和 macOS 没有这个标志，直接访问会 ``AttributeError``，所以返回空字典。
    """
    if sys.platform != "win32":
        return {}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if not flag:
        return {}
    return {"creationflags": flag}
