"""ngspice 子进程执行器。一律命令行调用（进程隔离，无 GPL 传染）。  [W1]

Windows 下若 ngspice 不在 PATH，设环境变量 CIRCUITPILOT_NGSPICE 指向 ngspice.exe。

经验（ngspice-47 Windows 实测）：batch 模式下仿真失败往往退出码仍是 0、stdout 为空，
报错只写进 -o 指定的日志文件，而且失败电路也可能产出全零的 raw 文件。
所以成败判定必须扫日志里的致命标记，不能只看退出码。
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

# 日志/stdout 里出现即判失败的标记（ngspice 手册 + 实测归纳）
_FATAL_MARKERS = (
    "singular matrix",
    "gmin stepping failed",
    "source stepping failed",
    "timestep too small",
    "analysis failed",
    "there aren't any circuits",
    "no ground node",
    "couldn't find",
    "could not find",
    "unknown model",
    "model.*used is undefined",
    "too many iterations",
    "simulation(s) aborted",
    "unknown parameter",
    "undefined subckt",
)


@dataclass
class SimResult:
    ok: bool
    stdout: str
    stderr: str
    log: str
    raw_path: Path | None
    elapsed: float

    @property
    def error_snippet(self) -> str:
        """给 LLM 看的报错摘要：各路输出里含 error/warning/致命标记的行，限长。"""
        lines = []
        for src in (self.stderr, self.stdout, self.log):
            for ln in src.splitlines():
                low = ln.lower()
                if ("error" in low or "warning" in low or "failed" in low
                        or any(mk in low for mk in _FATAL_MARKERS)):
                    lines.append(ln)
        text = "\n".join(dict.fromkeys(lines[-15:]))  # 去重保序
        if not text.strip():
            # 找不到可执行文件/超时等报错不含关键字，直接透传原始输出尾部
            text = (self.stderr or self.stdout or "仿真失败，且 stderr/stdout/日志均为空")[-800:]
        return text.strip()[:2000]


def _fatal_in(*sources: str) -> list[str]:
    """返回命中的致命标记（用于判失败）。"""
    hits: list[str] = []
    for src in sources:
        low = src.lower()
        for mk in _FATAL_MARKERS:
            if mk in low:
                hits.append(mk)
    return hits


def run_netlist(netlist_text: str, workdir: str | Path | None = None) -> SimResult:
    """写 .cir、跑 ngspice（batch 模式 -b），返回执行结果。"""
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="circuitpilot_"))
    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("*.raw"):
        old.unlink()  # 防旧产物污染：workdir 复用时绝不能把上次的 raw 当本次结果
    cir = workdir / "circuit.cir"
    cir.write_text(netlist_text, encoding="utf-8")
    log_file = workdir / "ngspice.log"

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [_NGSPICE, "-b", "-o", str(log_file), str(cir)],
            capture_output=True, text=True, timeout=_TIMEOUT, cwd=workdir,
        )
        log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        fatal = _fatal_in(proc.stderr, proc.stdout, log)
        ok = proc.returncode == 0 and not fatal
        # LLM 不一定遵守 out.raw 约名（会写 rc_lpf.raw 之类），按 mtime 取本次产物
        raws = sorted(workdir.glob("*.raw"), key=lambda p: p.stat().st_mtime)
        return SimResult(
            ok=ok, stdout=proc.stdout, stderr=proc.stderr, log=log,
            raw_path=raws[-1] if raws else None,
            elapsed=time.monotonic() - t0,
        )
    except FileNotFoundError:
        return SimResult(False, "", f"找不到 ngspice 可执行文件（{_NGSPICE}）", "", None, 0.0)
    except subprocess.TimeoutExpired:
        return SimResult(False, "", f"仿真超过 {_TIMEOUT}s 超时", "", None, time.monotonic() - t0)
