"""wheel 打包内容检查：app.pcb 子包、器件库 JSON、静态资源必须随包分发。

构建 wheel 到临时目录并逐项断言（验收报告第 7 条建议的发布检查）。
运行：python scripts/check_wheel.py（需要可用的 pip/build 环境）
"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

REQUIRED_SUBSTRINGS = [
    "app/pcb/__init__.py",
    "app/pcb/kicad_backend.py",
    "app/pcb/router_backend.py",
    "app/pcb/parts.py",
    "app/pcb/jobs.py",
    "app/pcb/api.py",
    "app/pcb/schema.py",
    "app/pcb/versions.py",
    "app/pcb/connectivity.py",
    "app/pcb/placement.py",
    "app/pcb/part_catalog/catalog.json",
    "app/pcb/tests_data/pcb_cases.json",
    "app/static/index.html",
    "app/static/draw.html",
    "app/static/board.js",
    "app/static/board.css",
    "app/static/pcb.js",
    "app/static/pcb.css",
    "app/static/proof.js",
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    import subprocess
    with tempfile.TemporaryDirectory(prefix="vp_wheel_") as td:
        cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", td, str(REPO)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            print("[FAIL] wheel 构建失败：\n" + (proc.stdout or "")[-800:]
                  + (proc.stderr or "")[-800:])
            return 1
        wheels = [p for p in Path(td).glob("*.whl")]
        if not wheels:
            print("[FAIL] 未产出 wheel")
            return 1
        names = set(zipfile.ZipFile(wheels[0]).namelist())
    missing = [r for r in REQUIRED_SUBSTRINGS if not any(r in n for n in names)]
    if missing:
        print("[FAIL] wheel 缺少必需文件：")
        for m in missing:
            print("  -", m)
        return 1
    print(f"[PASS] {wheels[0].name}：{len(REQUIRED_SUBSTRINGS)} 项必需文件全部在包内"
          f"（共 {len(names)} 条目）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
