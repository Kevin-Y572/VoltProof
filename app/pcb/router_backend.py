"""Freerouting（JVM JAR）子进程适配器：自动布线 + 看门狗 + 日志判定。

Freerouting 定位：环境变量 VOLTPROOF_FREEROUTING（JAR 路径）> 仓库根/便携包
内 freerouting-*.jar。返回 None 表示不可用（PCB 为可选能力）。

阶段 A 实测结论（2026-10-23，重要——判定逻辑全部据此设计）：
- 只可用 JVM JAR。v2.5.0 原生 CLI（GraalVM）序列化白名单缺失，真实板
  布线一启动即崩且进程挂死不退出（上游 #957，修复 #966 在 master 未发版）。
- **退出码不可作成败判据**：内部超时（TIMED_OUT）+ 写出部分结果也返回 0；
  反之被外部强杀（1）也可能已得到可用 SES——一切以日志解析 + 文件存在性为准。
- 隔离四件套必带：--user_data_path=<job dir>（默认会写共享目录且带锁）、
  -da（否则启动即外呼遥测/版本检查）、-l en（日志锚点稳定可解析）、
  -Xmx（JVM 累计分配可达数十 GB，不限堆会挤占宿主）。
- 无内部取消命令；取消 = 杀进程（实测无残留、无锁污染）。
- 结果可复现：同输入同配置逐轮输出比特级一致（种子固定）。
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 官方帮助漏列但解析器接受的新参数名（-mp 已弃用会告警）
_MAX_PASSES_ARG = "--router.autorouter.max_passes"


class ToolError(RuntimeError):
    """工具缺失/无法启动（区别于布线业务失败）。"""


@dataclass
class RouteResult:
    ok: bool                 # 业务成功：布线任务 COMPLETED 且 0 未布线且 SES 已写出
    timed_out: bool          # 内部超时（可能带部分结果）
    unrouted: int | None     # 最终未连接数（日志解析）
    violations: int | None   # 最终间距违规数
    score: float | None      # 0–1000 布线评分
    ses_written: bool
    elapsed: float
    log: str
    problems: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (f"{'PASS' if self.ok else 'FAIL'} unrouted={self.unrouted} "
                f"violations={self.violations} score={self.score} "
                f"ses={'Y' if self.ses_written else 'N'} {self.elapsed:.1f}s")


def find_freerouting() -> Path | None:
    """定位 Freerouting JAR；找不到返回 None。结果缓存。"""
    return _find_freerouting_cached()


@lru_cache(maxsize=1)
def _find_freerouting_cached() -> Path | None:
    env = os.environ.get("VOLTPROOF_FREEROUTING")
    if env and Path(env).is_file():
        return Path(env)
    for pat in ("freerouting-*.jar",):
        for cand in sorted(_REPO_ROOT.glob(pat), reverse=True):
            if cand.is_file():
                return cand
        for cand in sorted((_REPO_ROOT / "freerouting").glob(pat), reverse=True):
            if cand.is_file():
                return cand
    return None


# —— 日志解析（锚点均来自真实运行日志，阶段 A 实测样本）——
_RE_STATE = re.compile(r"finished with state: (\w+)")
_RE_FINAL = re.compile(r"final score: ([\d.]+) \((\d+) unrouted and (\d+) violations\)")
_RE_PASS = re.compile(r"pass #(\d+) .*?score ([\d.]+) \((\d+) unrouted and (\d+) violations\)")
# 原生 CLI 的崩溃标记（JVM JAR 不受影响，但防御性识别避免误判为"正常失败"）
_CRASH_MARKERS = ("UnsupportedFeatureError", "Exception in thread", "VMError")


def parse_route_log(log: str) -> dict:
    """解析 Freerouting 日志 → {state, unrouted, violations, score, crash}。"""
    state = None
    m = _RE_STATE.search(log)
    if m:
        state = m.group(1)
    unrouted = violations = score = None
    for m in _RE_FINAL.finditer(log):
        score, unrouted, violations = float(m.group(1)), int(m.group(2)), int(m.group(3))
    if score is None:  # 极小板可能没有 stage 汇总行，退回最后一行 pass 行
        for m in _RE_PASS.finditer(log):
            score, unrouted, violations = float(m.group(2)), int(m.group(3)), int(m.group(4))
    crash = any(marker in log for marker in _CRASH_MARKERS)
    return {"state": state, "unrouted": unrouted, "violations": violations,
            "score": score, "crash": crash}


def route(jar: Path, dsn: Path, ses_out: Path, workdir: Path,
          max_passes: int = 10, timeout_s: int = 180,
          heap_mb: int = 2048, threads: int = 4) -> RouteResult:
    """跑一次自动布线。看门狗超时强杀（工具无内部取消；实测强杀无残留）。

    成败判定不看退出码：state=COMPLETED 且 unrouted=0 且 SES 落盘才算 ok。
    TIMED_OUT/部分结果返回 timed_out=True 交调用方决策（阶段 B 的任务层
    会把部分结果留作候选）。
    """
    java = os.environ.get("VOLTPROOF_JAVA", "java")
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "frdata").mkdir(exist_ok=True)
    if ses_out.exists():
        ses_out.unlink()
    # 实测坑：`--router.autorouter.max_passes 10` 两段式会被当未知参数
    # （日志 WARN Unknown command line argument: 10），必须 `=` 连写；
    # 优化器线程默认=核数-1（各持板副本）在 1GB 堆下 OOM——限 4 线程+2GB 堆。
    cmd = [java, f"-Xmx{heap_mb}m", "-jar", str(jar),
           "-l", "en", "-da", "--gui.enabled=false",
           f"--user_data_path={workdir / 'frdata'}",
           "-de", str(dsn), "-do", str(ses_out),
           f"--router.autorouter.max_passes={max_passes}",
           "-mt", str(threads)]
    log_path = workdir / "freerouting.log"
    t0 = time.monotonic()
    problems: list[str] = []
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s,
                              cwd=str(workdir), creationflags=subprocess.CREATE_NO_WINDOW)
        # Windows 下子进程异常退出时 stdout/stderr 可能为 None（实测偶发）
        log = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        killed = False
    except subprocess.TimeoutExpired as e:
        log = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes)
               else (e.stdout or "") + "\n" + ((e.stderr or b"").decode("utf-8", "replace")
                                               if isinstance(e.stderr, bytes) else (e.stderr or "")))
        log = log.strip() + "\n[watchdog] 超时强杀"
        killed = True
        problems.append(f"看门狗超时（{timeout_s}s）已强杀进程")
    except OSError as e:
        raise ToolError(f"无法启动 Freerouting（{java} 可用吗？）：{e}") from e
    elapsed = time.monotonic() - t0
    log_path.write_text(log, encoding="utf-8")

    parsed = parse_route_log(log)
    if parsed["crash"]:
        problems.append("检测到工具崩溃标记（UnsupportedFeatureError/Exception）——"
                        "若用的是原生 CLI，请改用 JVM JAR（阶段 A 结论 #1）")
    if killed:
        problems.append("进程被外部超时终止，结果不完整")
    ses_written = ses_out.is_file() and ses_out.stat().st_size > 0
    ok = (not killed and parsed["state"] == "COMPLETED"
          and parsed["unrouted"] == 0 and ses_written and not parsed["crash"])
    if not ok and not problems:
        if parsed["state"] is None:
            problems.append("日志中未找到任务终态行（finished with state）")
        elif parsed["state"] != "COMPLETED":
            problems.append(f"任务终态为 {parsed['state']} 而非 COMPLETED")
        elif parsed["unrouted"]:
            problems.append(f"仍有 {parsed['unrouted']} 条连接未布通")
        elif not ses_written:
            problems.append("SES 未写出")
    return RouteResult(ok=ok, timed_out=parsed["state"] == "TIMED_OUT" or killed,
                       unrouted=parsed["unrouted"], violations=parsed["violations"],
                       score=parsed["score"], ses_written=ses_written,
                       elapsed=elapsed, log=log, problems=problems)
