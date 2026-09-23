"""ngspice 子进程执行器。一律命令行调用（进程隔离，无 GPL 传染）。  [W1]

Windows 下若 ngspice 不在 PATH，设环境变量 VOLTPROOF_NGSPICE 指向 ngspice.exe。

经验（ngspice-47 Windows 实测）：batch 模式下仿真失败往往退出码仍是 0、stdout 为空，
报错只写进 -o 指定的日志文件，而且失败电路也可能产出全零的 raw 文件。
所以成败判定必须扫日志里的致命标记，不能只看退出码。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_NGSPICE = os.environ.get("VOLTPROOF_NGSPICE", "ngspice")
# 超时可覆盖；默认 30s：正常的 tran 远快于此，收敛死循环 30s 也救不回来，
# 卡满 60s 只是烧掉重试预算（综合实验曾一电路拖满 4×60s）
_TIMEOUT = int(os.environ.get("VOLTPROOF_SIM_TIMEOUT", "30"))

# 安全过滤（2026-09-21 审查发现）：ngspice .control 块支持 shell 等系统命令，
# 用户/LLM 网表可借此执行任意系统命令（实测 PoC 成功）——一律拒绝。
# quit 只退出 ngspice 无副作用，放行（否则误伤正常网表习惯）。
_CTRL_DANGER = re.compile(r"^\s*(shell|system|alias|spice|exec|source|cd)\b",
                          re.IGNORECASE)
# .control 内启动外部程序的命令：gnuplot 拉起 wgnuplot 绘图前端、edit 拉起
# 外部编辑器——不执行任意命令但会在服务器桌面弹 GUI 窗口（2026-09-22 官方
# 示例 plot/combplot.cir 回归实测：未装 gnuplot 时弹"找不到文件 wgnuplot"）
_CTRL_EXTERNAL = re.compile(r"^\s*(gnuplot|edit)\b", re.IGNORECASE)
# .include/.lib 拒绝绝对/网络路径（相对 ../ 允许——ngspice 示例的惯用法，
# 且 include 内容不回显给用户，风险极低）
_BAD_INCLUDE = re.compile(r"^\s*[.](include|lib)\s+[\"']?([a-z]:|[\\\\]{2}|//)",
                          re.IGNORECASE)


def check_netlist_safety(netlist_text: str) -> list[str]:
    """返回安全问题列表（空列表=安全）。在写文件/起进程前调用。"""
    problems: list[str] = []
    in_control = False
    for ln in netlist_text.splitlines():
        s = ln.strip()
        low = s.lower()
        if low == ".control":
            in_control = True
            continue
        if low == ".endc":
            in_control = False
            continue
        if in_control and _CTRL_DANGER.match(s):
            problems.append(f"被禁止的系统命令：'{s.split()[0]}'（.control 内不允许执行系统命令）")
        if in_control and _CTRL_EXTERNAL.match(s):
            problems.append(f"被禁止的外部程序调用：'{s.split()[0]}'"
                            "（.control 内不允许启动 gnuplot/编辑器等外部程序）")
        if _BAD_INCLUDE.match(s):
            problems.append(f".include/.lib 只允许相对路径：'{s}'")
    return problems

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
            # （log 也要进兜底链：ngspice 静默空跑时 stdout/stderr 全空、
            # 诊断信息只在 -o 日志里——2026-09-22 调试时它缺席导致误判）
            text = (self.stderr or self.stdout or self.log
                    or "仿真失败，且 stderr/stdout/日志均为空")[-800:]
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


def _run_capped(args: list, cwd: Path, timeout: int, cap: int = 8 * 1024 * 1024):
    """带输出容量上限的子进程执行。

    subprocess.run 的 timeout 依赖 communicate 的读取线程——当子进程输出
    海量数据（如 .control 里 print/plot 把绘图数据打到 stdout）时读取
    线程会阻塞，超时机制随之失效、调用永久挂死（2026-09-21 开源网表
    回归实测：combplot 类示例挂死进程）。改为独立线程计数读取，超上限
    或超时直接 kill。
    cwd 必须先 resolve 成绝对路径：Windows 的 CreateProcess 对相对
    lpCurrentDirectory 行为未定义——ngspice 实测要么挂死到超时、要么
    静默空跑（2026-09-22 定位：同一网表相对 cwd 必挂、绝对 cwd 0.5s）。
    子进程 stdin 必须显式关闭（DEVNULL）：ngspice-47 Windows 批处理模式
    会读继承来的 stdin——stdin 是永不 EOF 的管道（服务进程/后台调用）时
    进程挂死到超时（2026-09-22 实测：终端交互正常、脚本内调用全部超时）。"""
    p = subprocess.Popen(args, cwd=str(Path(cwd).resolve()), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buf: dict[str, list] = {"out": [], "err": []}
    exceeded = threading.Event()

    def _read(fh, key):
        while True:
            chunk = fh.read(65536)
            if not chunk:
                return
            buf[key].append(chunk)
            if sum(len(b) for b in buf[key]) > cap:
                exceeded.set()
                p.kill()
                return

    t_out = threading.Thread(target=_read, args=(p.stdout, "out"), daemon=True)
    t_err = threading.Thread(target=_read, args=(p.stderr, "err"), daemon=True)
    t_out.start()
    t_err.start()
    try:
        rc = p.wait(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        rc = -9
        timed_out = True
    t_out.join(timeout=2)
    t_err.join(timeout=2)
    text = lambda parts: b"".join(parts).decode("utf-8", errors="replace")  # noqa: E731
    return rc, text(buf["out"]), text(buf["err"]), exceeded.is_set(), timed_out


def run_netlist(netlist_text: str, workdir: str | Path | None = None) -> SimResult:
    """写 .cir、跑 ngspice（batch 模式 -b），返回执行结果。"""
    safety = check_netlist_safety(netlist_text)
    if safety:
        return SimResult(False, "", "；".join(safety), "", None, 0.0)
    # resolve 成绝对路径再派生子进程：Windows CreateProcess 会切换子进程
    # cwd，相对的 -o/网表参数随之失效（实测：静默空跑 + rc!=0，日志停在旧文件）
    workdir = (Path(workdir) if workdir
               else Path(tempfile.mkdtemp(prefix="voltproof_"))).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("*.raw"):
        old.unlink()  # 防旧产物污染：workdir 复用时绝不能把上次的 raw 当本次结果
    cir = workdir / "circuit.cir"
    cir.write_text(netlist_text, encoding="utf-8")
    log_file = workdir / "ngspice.log"

    t0 = time.monotonic()
    try:
        rc, stdout, stderr, capped, timed_out = _run_capped(
            [_NGSPICE, "-b", "-o", str(log_file), str(cir)], workdir, _TIMEOUT)
        if capped:
            return SimResult(False, stdout[:2000], "仿真输出超过 8MB 上限（疑似在 "
                             "stdout 打印绘图数据），已终止", "", None, time.monotonic() - t0)
        if timed_out:
            # 超时被杀的进程三路输出常为空——不点明原因，修复环只能对着
            # "日志均为空"盲改（2026-09-22 CIDER 网表实测挂死 30s 一无所获）
            stderr = (f"仿真超时：{_TIMEOUT}s 内未完成已被终止（无输出产生）。常见原因："
                      "数值收敛死循环（理想受控源环路/高 Q 谐振/不支持的器件类型）；"
                      "建议给理想受控源串 RC 限幅、减小仿真时长或更换电路拓扑"
                      + (("；原 stderr：" + stderr) if stderr.strip() else ""))
        log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        fatal = _fatal_in(stderr, stdout, log)
        ok = rc == 0 and not fatal
        # LLM 不一定遵守 out.raw 约名（会写 rc_lpf.raw 之类），按 mtime 取本次产物
        raws = sorted(workdir.glob("*.raw"), key=lambda p: p.stat().st_mtime)
        return SimResult(
            ok=ok, stdout=stdout, stderr=stderr, log=log,
            raw_path=raws[-1] if raws else None,
            elapsed=time.monotonic() - t0,
        )
    except FileNotFoundError:
        return SimResult(False, "", f"找不到 ngspice 可执行文件（{_NGSPICE}）", "", None, 0.0)
    except subprocess.TimeoutExpired:
        return SimResult(False, "", f"仿真超过 {_TIMEOUT}s 超时", "", None, time.monotonic() - t0)
