"""ngspice 子进程执行器。一律命令行调用（进程隔离，无 GPL 传染）。

若 ngspice 不在 PATH，设 VOLTPROOF_NGSPICE 指向可执行文件
（Linux 多为 /usr/bin/ngspice，Windows 多为 ngspice.exe）。

经验（ngspice-47 Windows 实测）：batch 模式下仿真失败往往退出码仍是 0、stdout 为空，
报错只写进 -o 指定的日志文件，而且失败电路也可能产出全零的 raw 文件。
所以成败判定必须扫日志里的致命标记，不能只看退出码。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path

from . import i18n


def _resolve_ngspice() -> str:
    """ngspice 可执行文件定位：环境变量 > PATH > 便携包自带（<包根>/ngspice/bin）。
    便携发行版里 ngspice 随包分发且不进系统 PATH，第三级让免安装包零配置可用。"""
    env = os.environ.get("VOLTPROOF_NGSPICE")
    if env:
        return env
    on_path = shutil.which("ngspice")
    if on_path:
        return on_path
    bundled_dir = Path(__file__).resolve().parent.parent / "ngspice" / "bin"
    for name in ("ngspice.exe", "ngspice"):
        bundled = bundled_dir / name
        if bundled.is_file():
            return str(bundled)
    return "ngspice"


_NGSPICE = _resolve_ngspice()
# 超时可覆盖；默认 30s：正常的 tran 远快于此，收敛死循环 30s 也救不回来，
# 卡满 60s 只是烧掉重试预算（曾有电路拖满 4×60s）
_TIMEOUT = int(os.environ.get("VOLTPROOF_SIM_TIMEOUT", "30"))

# 安全过滤在起 ngspice 之前完成。这里不是操作系统沙箱：
# 只拒绝已知会出目录、起进程或加载代码的网表写法。
# 以 "+" 开头的续行不会并进上一行（已知缺口，不在本次过滤里拼行）。
#
# 命令分类来自 ngspice 交互命令表（write/wrdata/fopen/codemodel/osdi 等）。
# 缩写按 ngspice 的前缀匹配处理：只要可能落到危险命令就拒绝。
# quit 只退出，无副作用，放行。
_OK_CMDS = (
    "let reshape define set setcs option options deftype bltplot plot display "
    "destroy setplot setcirc setscale setseed transpose asciiplot compose unlet "
    "remzerovec print sndprint sndparam esave eprint edisplay cross undefine op "
    "tf tran pss sp ac dc pz sens disto noise listing mc_source dump fft psd "
    "fourier spec meas show showmod sysinfo alter altermod alterparam resume "
    "state stop trace save iplot status delete step remcirc reset run jobs bug "
    "where newhelp tutorial help oldhelp removecirc quit shift unset unalias "
    "history echo rusage getcwd version diff rehash while repeat dowhile foreach "
    "if else end break continue label goto cdump mdump mrdump settype strcmp "
    "strstr strslice linearize cutout devhelp inventory optran check_ifparm"
).split()
# 起进程、改目录、执行脚本、任意写文件（hardcopy）
_SHELL_CMDS = (
    "alias aspice cd circbyline hardcopy rspice shell source system exec spice"
).split()
# 加载共享库 / 代码模型。相对路径同样拒绝：加载的是可执行代码。
_CODE_CMDS = ("codemodel osdi pre_osdi use").split()
# 通用文件读写。fread 的内容会进日志，不能只限制路径。
_FIO_CMDS = ("fopen fread fwrite fclose").split()
# 拉起外部程序（gnuplot/编辑器/python 绘图）
_EXT_CMDS = ("edit gnuplot pyplot").split()
# 仿真产物写入。相对路径留在仿真目录内则放行（write out.raw 是正常网表）。
_FILE_CMDS = ("eprvcd load snload snsave wrdata write wrnodev wrs2p").split()
_CMD_KIND = {}
for _name in _OK_CMDS:
    _CMD_KIND[_name] = "ok"
for _name in _SHELL_CMDS:
    _CMD_KIND[_name] = "shell"
for _name in _CODE_CMDS:
    _CMD_KIND[_name] = "code"
for _name in _FIO_CMDS:
    _CMD_KIND[_name] = "fio"
for _name in _EXT_CMDS:
    _CMD_KIND[_name] = "external"
for _name in _FILE_CMDS:
    _CMD_KIND[_name] = "file"
_DANGER_RANK = ("shell", "code", "fio", "external", "file")

# .include / .inc / .lib（含唯一前缀）。.ic 是初值，不能误伤。
_INC_RE = re.compile(
    r"""^\s*\.(?P<kind>inc\w*|lib\w*)\s+(?P<path>"[^"]*"|'[^']*'|\S+)""",
    re.IGNORECASE)
_INLINE_SRC = re.compile(r"(?i)\bin line\s+(.+)\s*$")
_NEXT_SRC = re.compile(r"(error on line|original line|new internal line)", re.IGNORECASE)
_SET_PATH = re.compile(r"\b(sourcepath|inputdir)\b", re.IGNORECASE)
_MAX_INC_DEPTH = 8
_MAX_INC_FILES = 32
_MAX_INC_BYTES = 256 * 1024


def _command_kind(token: str) -> str:
    """精确名优先；否则按前缀归类。混有危险命令时失败关闭。"""
    t = token.lower().lstrip(".")
    if not t:
        return "unknown"
    exact = _CMD_KIND.get(t)
    if exact:
        return exact
    kinds = {kind for name, kind in _CMD_KIND.items() if name.startswith(t)}
    if not kinds:
        return "unknown"
    for rank in _DANGER_RANK:
        if rank in kinds:
            return rank
    return "ok"


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def _is_inside(root: Path, candidate: Path) -> bool:
    root_s = os.path.normcase(str(root))
    cand_s = os.path.normcase(str(candidate))
    return cand_s == root_s or cand_s.startswith(root_s + os.sep)


def _path_escapes(raw: str, bases: list[Path], root: Path | None) -> bool:
    """绝对路径、~、$、盘符、UNC，或解析后离开 root 的相对路径。"""
    path = _unquote(raw).strip()
    if not path or "\x00" in path or "$" in path or path.startswith("~"):
        return True
    norm = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", norm) or norm.startswith("/") or norm.startswith("//"):
        return True
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if root is None:
        return any(p == ".." for p in parts)
    root_r = root.resolve()
    bases_r = bases or [root_r]
    for base in bases_r:
        try:
            cand = (base.resolve() / norm).resolve()
        except OSError:
            return True
        if not _is_inside(root_r, cand):
            return True
    return False


def _short(text: str, limit: int = 80) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _split_statements(line: str) -> list[str]:
    """按引号外的分号拆开。`$` 后接空白才是注释；`$inputdir` 这类变量要留下。"""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            nxt = line[i + 1] if i + 1 < len(line) else ""
            if not nxt or nxt.isspace():
                break
        if ch == ";":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _has_redirect(segment: str) -> bool:
    quote = ""
    for ch in segment:
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch == ">":
            return True
    return False


def _tokens(segment: str) -> list[str]:
    toks: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in segment:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
                toks.append("".join(buf))
                buf = []
            continue
        if ch in "'\"":
            if buf:
                toks.append("".join(buf))
                buf = []
            quote = ch
            buf.append(ch)
            continue
        if ch.isspace():
            if buf:
                toks.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        toks.append("".join(buf))
    return toks


def _is_vector(token: str) -> bool:
    raw = _unquote(token)
    low = raw.lower()
    return low in {"all", "none"} or "(" in raw


def _file_args_escape(tokens: list[str], bases: list[Path], root: Path | None) -> bool:
    for tok in tokens[1:]:
        if tok.startswith("-") and not tok.startswith("---"):
            continue
        if _is_vector(tok):
            continue
        if _path_escapes(tok, bases, root):
            return True
    return False


def _is_comment(line: str) -> bool:
    head = line.lstrip()
    return not head or head.startswith("*")


def check_netlist_safety(netlist_text: str, root: str | Path | None = None) -> list[str]:
    """返回安全问题列表（空列表=通过）。调用方必须在启动仿真器之前使用。

    root 是本次仿真目录。include 与 write 类命令解析后必须仍在该目录内。
    不传 root 时，绝对路径和含 .. 的相对路径直接拒绝。
    """
    base = Path(root).resolve() if root is not None else None
    problems: list[str] = []
    _scan_deck(netlist_text, base, [base] if base is not None else [], problems, set(), 0)
    return problems


def _scan_deck(text: str, root: Path | None, bases: list[Path], problems: list[str],
               seen: set[str], depth: int) -> None:
    in_control = False
    for ln in text.splitlines():
        if _is_comment(ln):
            continue
        s = ln.strip()
        low = s.lower()
        if low.startswith(".control"):
            in_control = True
            continue
        if low.startswith(".endc"):
            in_control = False
            continue
        inc = _INC_RE.match(s)
        if inc:
            path = inc.group("path")
            if _path_escapes(path, bases, root):
                problems.append(i18n.t_zh("api.safe.path", line=_short(s)))
            else:
                _follow_include(path, root, bases, problems, seen, depth)
        if not in_control:
            word = (_tokens(s)[:1] or [""])[0].lower().lstrip(".")
            if word in {"codemodel", "osdi", "pre_osdi"}:
                problems.append(i18n.t_zh("api.safe.code", cmd=word))
            continue
        for segment in _split_statements(s):
            _scan_control(segment, bases, root, problems)


def _scan_control(segment: str, bases: list[Path], root: Path | None, problems: list[str]) -> None:
    if segment.lstrip().startswith("!"):
        problems.append(i18n.t_zh("api.safe.shell", cmd="!"))
        return
    if _has_redirect(segment):
        problems.append(i18n.t_zh("api.safe.file", cmd=">"))
    toks = _tokens(segment)
    if not toks:
        return
    kind = _command_kind(toks[0])
    cmd = toks[0].lower().lstrip(".")
    if kind == "shell":
        problems.append(i18n.t_zh("api.safe.shell", cmd=cmd))
    elif kind == "external":
        problems.append(i18n.t_zh("api.safe.ext", cmd=cmd))
    elif kind == "code":
        problems.append(i18n.t_zh("api.safe.code", cmd=cmd))
    elif kind == "fio":
        problems.append(i18n.t_zh("api.safe.file", cmd=cmd))
    elif kind == "file" and _file_args_escape(toks, bases, root):
        problems.append(i18n.t_zh("api.safe.file", cmd=cmd))
    # set/option 改 sourcepath、inputdir 会把后续相对路径指到目录外
    if _SET_PATH.search(segment):
        problems.append(i18n.t_zh("api.safe.file", cmd=cmd))


def _follow_include(raw: str, root: Path | None, bases: list[Path], problems: list[str],
                    seen: set[str], depth: int) -> None:
    """已确认路径留在仿真目录内时，才读取并继续检查。不把文件正文写进报错。"""
    if root is None:
        return
    if depth + 1 > _MAX_INC_DEPTH or len(seen) >= _MAX_INC_FILES:
        problems.append(i18n.t_zh("api.safe.path", line=_short(raw)))
        return
    norm = _unquote(raw).strip().replace("\\", "/")
    found: list[Path] = []
    for base in bases or [root]:
        try:
            cand = (base.resolve() / norm).resolve()
        except OSError:
            problems.append(i18n.t_zh("api.safe.path", line=_short(raw)))
            return
        if not _is_inside(root.resolve(), cand):
            problems.append(i18n.t_zh("api.safe.path", line=_short(raw)))
            return
        if cand.is_file() and cand not in found:
            found.append(cand)
    for cand in found:
        key = os.path.normcase(str(cand))
        if key in seen:
            continue
        seen.add(key)
        try:
            if cand.stat().st_size > _MAX_INC_BYTES:
                problems.append(i18n.t_zh("api.safe.path", line=_short(raw)))
                continue
            body = cand.read_text(encoding="utf-8", errors="replace")
        except OSError:
            problems.append(i18n.t_zh("api.safe.path", line=_short(raw)))
            continue
        _scan_deck(body, root, [root, cand.parent], problems, seen, depth + 1)


def _source_in_netlist(src: str, known: set[str]) -> bool:
    text = src.strip()
    if text in known:
        return True
    # ngspice 常在回显的源码行末尾加一个感叹号
    return text.endswith("!") and text[:-1].strip() in known


def redact_foreign_source(text: str, netlist: str) -> str:
    """去掉报错里引用的、并不在提交网表中的源码行，避免 include 正文回流到接口或修复环。"""
    if not text:
        return text
    known = {ln.strip() for ln in netlist.splitlines() if ln.strip()}
    out: list[str] = []
    pending = False
    for ln in text.splitlines():
        if pending:
            if not ln.strip():
                out.append(ln)
                continue
            pending = False
            if not _source_in_netlist(ln, known):
                out.append("[已省略网表以外的源码行]")
                continue
            out.append(ln)
            continue
        redacted = ln
        match = _INLINE_SRC.search(ln)
        if match and not _source_in_netlist(match.group(1), known):
            redacted = ln[:match.start(1)] + "[已省略网表以外的源码行]"
        out.append(redacted)
        if _NEXT_SRC.search(ln):
            pending = True
    return "\n".join(out)

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
                # ngspice 把日志路径提示写进 stdout，句子里带 warnings，不能当成诊断。
                if "comments and warnings go to log-file" in low:
                    continue
                if ("error" in low or "warning" in low or "failed" in low
                        or "超时" in ln or "收敛死循环" in ln
                        or any(mk in low for mk in _FATAL_MARKERS)):
                    lines.append(ln)
        text = "\n".join(dict.fromkeys(lines[-15:]))  # 去重保序
        if not text.strip():
            # 找不到可执行文件/超时等报错不含关键字，直接透传原始输出尾部
            # （log 也要进兜底链：ngspice 静默空跑时 stdout/stderr 全空、
            # 诊断信息只在 -o 日志里——它缺席曾导致误判）
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


# 子进程只继承启动所需变量。API Key、HOME（.spiceinit）和 LD_PRELOAD 都不传。
_NGSPICE_ENV_KEYS = (
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "PATHEXT",
    "LANG", "LC_CTYPE",
)
_SECRET_KEY = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|PASSWD)", re.I)


def ngspice_env() -> dict[str, str]:
    """ngspice 子进程环境。不继承凭据，小数点固定为 '.'。"""
    env = {k: os.environ[k] for k in _NGSPICE_ENV_KEYS
           if k in os.environ and not _SECRET_KEY.search(k)}
    env["LC_ALL"] = "C"
    return env


def ngspice_argv(cir: Path, log_file: Path) -> list[str]:
    """-n 就是 --no-spiceinit：不加载当前目录或用户主目录里的 .spiceinit。"""
    return [_NGSPICE, "-b", "-n", "-o", str(log_file), str(cir)]


def _scrubbed(stdout: str, stderr: str, log: str, netlist: str) -> tuple[str, str, str]:
    return (redact_foreign_source(stdout, netlist),
            redact_foreign_source(stderr, netlist),
            redact_foreign_source(log, netlist))


def _run_capped(args: list, cwd: Path, timeout: int, cap: int = 8 * 1024 * 1024,
                env: dict | None = None):
    """带输出容量上限的子进程执行。

    subprocess.run 的 timeout 依赖 communicate 的读取线程——当子进程输出
    海量数据（如 .control 里 print/plot 把绘图数据打到 stdout）时读取
    线程会阻塞，超时机制随之失效、调用永久挂死（开源网表
    回归实测：combplot 类示例挂死进程）。改为独立线程计数读取，超上限
    或超时直接 kill。
    cwd 必须先 resolve 成绝对路径：Windows 的 CreateProcess 对相对
    lpCurrentDirectory 行为未定义——ngspice 实测要么挂死到超时、要么
    静默空跑（同一网表相对 cwd 必挂、绝对 cwd 0.5s）。
    子进程 stdin 必须显式关闭（DEVNULL）：ngspice-47 Windows 批处理模式
    会读继承来的 stdin——stdin 是永不 EOF 的管道（服务进程/后台调用）时
    进程挂死到超时（实测：终端交互正常、脚本内调用全部超时）。"""
    p = subprocess.Popen(args, cwd=str(Path(cwd).resolve()), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=ngspice_env() if env is None else env)
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
    if not t_out.is_alive():
        p.stdout.close()
    if not t_err.is_alive():
        p.stderr.close()
    text = lambda parts: b"".join(parts).decode("utf-8", errors="replace")  # noqa: E731
    return rc, text(buf["out"]), text(buf["err"]), exceeded.is_set(), timed_out


@lru_cache(maxsize=1)
def simulator_info():
    try:
        proc = subprocess.run([_NGSPICE, "--version"], capture_output=True, timeout=5,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              env=ngspice_env())
        match = re.search(r"ngspice[-\s]+(\d+(?:\.\d+)*)", proc.stdout + proc.stderr, re.I)
        version = match[1] if match else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        version = "unknown"
    return {"name":"ngspice", "version":version, "executable":_NGSPICE}


def run_netlist(netlist_text: str, workdir: str | Path | None = None) -> SimResult:
    """写 .cir、跑 ngspice（batch 模式 -b -n），返回执行结果。

    安全检查使用仿真目录作根：不通过则不会启动 ngspice。
    """
    # resolve 成绝对路径再派生子进程：Windows CreateProcess 会切换子进程
    # cwd，相对的 -o/网表参数随之失效（实测：静默空跑 + rc!=0，日志停在旧文件）
    workdir = (Path(workdir) if workdir
               else Path(tempfile.mkdtemp(prefix="voltproof_"))).resolve()
    safety = check_netlist_safety(netlist_text, root=workdir)
    if safety:
        return SimResult(False, "", "；".join(safety), "", None, 0.0)
    workdir.mkdir(parents=True, exist_ok=True)
    for old in workdir.glob("*.raw"):
        old.unlink()  # 防旧产物污染：workdir 复用时绝不能把上次的 raw 当本次结果
    cir = workdir / "circuit.cir"
    cir.write_text(netlist_text, encoding="utf-8")
    log_file = workdir / "ngspice.log"

    t0 = time.monotonic()
    try:
        rc, stdout, stderr, capped, timed_out = _run_capped(
            ngspice_argv(cir, log_file), workdir, _TIMEOUT, env=ngspice_env())
        if capped:
            stdout, _, _ = _scrubbed(stdout[:2000], "", "", netlist_text)
            return SimResult(False, stdout, "仿真输出超过 8MB 上限（疑似在 "
                             "stdout 打印绘图数据），已终止", "", None, time.monotonic() - t0)
        if timed_out:
            # 超时被杀的进程三路输出常为空——不点明原因，修复环只能对着
            # "日志均为空"盲改（CIDER 网表实测挂死 30s 一无所获）
            stderr = (f"仿真超时：{_TIMEOUT}s 内未完成已被终止（无输出产生）。常见原因："
                      "数值收敛死循环（理想受控源环路/高 Q 谐振/不支持的器件类型）；"
                      "建议给理想受控源串 RC 限幅、减小仿真时长或更换电路拓扑"
                      + (("；原 stderr：" + stderr) if stderr.strip() else ""))
        log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        fatal = _fatal_in(stderr, stdout, log)
        ok = rc == 0 and not fatal
        stdout, stderr, log = _scrubbed(stdout, stderr, log, netlist_text)
        if log_file.exists():
            try:
                log_file.write_text(log, encoding="utf-8")
            except OSError:
                pass
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
