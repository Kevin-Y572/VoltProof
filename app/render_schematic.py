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

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .sandbox import ast_check, minimal_env

_ALLOWED_MODULES = {"schemdraw", "schemdraw.elements", "math"}


def _ast_check(code: str) -> bool:
    return ast_check(code, _ALLOWED_MODULES)


_SAVE_RE = re.compile(r"\.save\(\s*['\"]schematic\.png['\"][^)]*\)", re.DOTALL)


def render_schematic(code: str, out_png: str | Path, timeout: int = 30) -> Path | None:
    """返回 PNG 路径；校验或执行失败返回 None（调用方降级显示网表）。"""
    if not _ast_check(code):
        return None
    # 注入白底参数：save 默认 transparent=True，深色 UI 上会渲染成黑图
    if not _SAVE_RE.search(code):
        return None  # 没有保存 schematic.png 的代码，必然无产物
    code = _SAVE_RE.sub(".save('schematic.png', dpi=150, transparent=False)", code)

    out_png = Path(out_png)
    with tempfile.TemporaryDirectory(prefix="cp_sch_") as td:
        tdp = Path(td)
        script = tdp / "schem.py"
        script.write_text(code, encoding="utf-8")
        # 强制 Agg + 最小环境（不含任何凭据，纵深防御）
        env = minimal_env()
        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(script)],
                cwd=tdp, capture_output=True, text=True, timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            return None
        png = tdp / "schematic.png"
        if proc.returncode == 0 and png.exists() and png.stat().st_size > 0 \
                and _has_content(png):
            out_png.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(png, out_png)
            return out_png
    return None


def _has_content(png: Path, min_ratio: float = 0.001) -> bool:
    """质量门：可见像素（非空白）占比过低视为空图。"""
    import matplotlib.image as mpimg

    arr = mpimg.imread(png)
    if arr.ndim == 3 and arr.shape[-1] == 4:
        visible = (arr[..., 3] > 0.05) | (arr[..., :3] < 0.9).any(axis=-1)
    else:
        visible = (arr < 0.9).any(axis=-1)
    return float(visible.mean()) >= min_ratio
