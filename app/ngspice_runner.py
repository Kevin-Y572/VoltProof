"""ngspice 子进程执行器。一律命令行调用（进程隔离，无 GPL 传染）。  [W1]

Windows 下若 ngspice 不在 PATH，设环境变量 CIRCUITPILOT_NGSPICE 指向 ngspice.exe。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_NGSPICE = os.environ.get("CIRCUITPILOT_NGSPICE", "ngspice")
_TIMEOUT = 60  # 秒，路线图要求端到端 ≤60s，仿真本身留 60s 上限


@dataclass
class SimResult:
    ok: bool
    stdout: str
    stderr: str
    raw_path: Path | None
    elapsed: float

    @property
    def error_snippet(self) -> str:
        """给 LLM 看的报错摘要：stderr + stdout 里含 error/warning 的行，限长。"""
        lines = []
        for src in (self.stderr, self.stdout):
            for ln in src.splitlines():
                low = ln.lower()
                if "error" in low or "warning" in low or "failed" in low or "singular" in low:
                    lines.append(ln)
        text = "\n".join(lines[-15:]) or (self.stderr or self.stdout)[-800:]
        return text[:2000]


def run_netlist(netlist_text: str, workdir: str | Path | None = None) -> SimResult:
    """写 .cir、跑 ngspice（batch 模式 -b），返回执行结果。"""
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="circuitpilot_"))
    workdir.mkdir(parents=True, exist_ok=True)
    cir = workdir / "circuit.cir"
    cir.write_text(netlist_text, encoding="utf-8")
    raw = workdir / "out.raw"

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [_NGSPICE, "-b", "-o", str(workdir / "ngspice.log"), str(cir)],
            capture_output=True, text=True, timeout=_TIMEOUT, cwd=workdir,
        )
        ok = proc.returncode == 0 and "error" not in proc.stderr.lower()
        return SimResult(
            ok=ok, stdout=proc.stdout, stderr=proc.stderr,
            raw_path=raw if raw.exists() else None,
            elapsed=time.monotonic() - t0,
        )
    except FileNotFoundError:
        return SimResult(False, "", f"找不到 ngspice 可执行文件（{_NGSPICE}）", None, 0.0)
    except subprocess.TimeoutExpired:
        return SimResult(False, "", f"仿真超过 {_TIMEOUT}s 超时", None, time.monotonic() - t0)


# TODO(W1): 验证 .control/write 命令与 -b batch 模式的配合；确认 raw 文件落盘路径。
# TODO(W3): 进程池/并发安全（多个会话同时跑仿真时隔离 workdir 已天然满足）。
