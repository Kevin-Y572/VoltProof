"""开源网表批量回归：ngspice 官方示例（BSD）灌进 仿真→解析→指标→渲染 全链路。

目的：用 444 个陌生网表压力测试测量层的兼容性——此前每个解析 bug
（符号/重复变量/编码）都是这类问题，真实多样网表的覆盖率远超手工用例。

适配注入：外部网表通常没有 .control/write（不出 raw），本脚本按其顶层
分析指令自动注入 .control 块（set filetype=ascii + 同参数分析 +
write out.raw all）——这也是产品"附件诊断"路径需要的通用适配器。

用法：python bench/regress_opensource.py [每个目录抽样数，默认 4]
输出：bench/regress_report.md + 控制台摘要
"""

from __future__ import annotations

import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.measure import extract_metrics, load_traces  # noqa: E402
from app.ngspice_runner import run_netlist  # noqa: E402
from app.render_wave import render_wave  # noqa: E402
from app.schematic_layout import render_netlist_schematic  # noqa: E402

EXAMPLES = Path(r"D:\Users\Lenovo\tools\ngspice-47\Spice64\examples")
OUT = Path(__file__).resolve().parent / "regress_out"

_ANALYSIS_RE = re.compile(r"^\s*\.(tran|ac|op|dc|noise|sp|pss|disto)\b(.*)$", re.I)


def ensure_control_block(netlist: str) -> str:
    """保证网表能产出 raw：无 .control 时注入完整块；已有 .control 但无
    write 时在 .endc 前补 write out.raw all（取最后一次分析的数据）。"""
    low = netlist.lower()
    if ".control" not in low:
        cmds = []
        for ln in netlist.splitlines():
            m = _ANALYSIS_RE.match(ln)
            if m:
                cmds.append(f"{m.group(1).lower()} {m.group(2).strip()}")
        if not cmds:
            cmds = ["op"]  # 无任何分析指令时至少跑工作点
        block = ".control\nset filetype=ascii\n" + cmds[0] + "\nwrite out.raw all\n.endc\n"
        lines = netlist.splitlines()
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip().lower() == ".end":
                lines.insert(i, block.rstrip())
                return "\n".join(lines) + "\n"
        return netlist + "\n" + block + ".end\n"
    # 已有 .control：最后一个 .endc 前补 write（若无）
    if "write" in low:
        return netlist
    lines = netlist.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().lower() == ".endc":
            lines.insert(i, "set filetype=ascii")
            lines.insert(i + 1, "write out.raw all")
            return "\n".join(lines) + "\n"
    return netlist


_INC_RE = re.compile(r"^\s*[.](include|lib)\s+[\"']?([^\"'\s]+)", re.I)


def copy_dependencies(src: Path, workdir: Path, netlist: str) -> None:
    """把网表相对 include 的依赖文件拷到 workdir（cwd 变化后相对路径会断）。"""
    for m in _INC_RE.finditer(netlist):
        dep = m.group(2)
        if re.match(r"^[a-z]:", dep, re.I) or dep.startswith(("\\\\", "/")):
            continue  # 绝对路径不动
        cand = src.parent / dep
        if cand.exists():
            target = workdir / dep
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(cand.read_bytes())


def main() -> None:
    per_dir = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    files: list[Path] = []
    by_dir: dict[str, list[Path]] = {}
    for f in sorted(EXAMPLES.rglob("*.cir")):
        by_dir.setdefault(f.parent.name, []).append(f)
    for d, fs in sorted(by_dir.items()):
        files.extend(fs[:per_dir])
    print(f"抽样 {len(files)} 个网表（{len(by_dir)} 个目录，每目录 ≤{per_dir}）")

    OUT.mkdir(exist_ok=True)
    stat: Counter[str] = Counter()
    fails: list[tuple[str, str]] = []
    t_all = time.time()
    for f in files:
        rel = str(f.relative_to(EXAMPLES))
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            stat["read_fail"] += 1
            fails.append((rel, f"读取失败 {e}"))
            continue
        nl = ensure_control_block(src)
        wd = OUT / f.stem
        wd.mkdir(parents=True, exist_ok=True)
        copy_dependencies(f, wd, src)
        sim = run_netlist(nl, workdir=wd)
        if not sim.ok:
            if "被禁止" in sim.stderr:
                stat["rejected"] += 1  # 安全过滤的合理拒绝（示例自带 shell/cd 等）
            else:
                stat["sim_fail"] += 1
                fails.append((rel, (sim.error_snippet or sim.stderr)[:120]))
            continue
        stat["sim_ok"] += 1
        if sim.raw_path is None:
            stat["no_raw"] += 1
            fails.append((rel, "仿真通过但无 raw"))
            continue
        try:
            tr = load_traces(sim.raw_path)
            if tr.signals:
                stat["parse_ok"] += 1
                m = extract_metrics(tr)
                stat["metrics_ok"] += 1 if m else 0
                render_wave(tr, wd / "wave.png")
                stat["wave_ok"] += 1
            else:
                stat["parse_empty"] += 1
        except Exception as e:
            stat["parse_fail"] += 1
            fails.append((rel, f"{type(e).__name__}: {e}"[:120]))
        try:
            if render_netlist_schematic(nl, wd / "sch.png"):
                stat["sch_ok"] += 1
        except Exception:
            stat["sch_fail"] += 1

    n = len(files)
    pct = lambda k: f"{stat[k]}/{n}（{stat[k] / n:.0%}）"  # noqa: E731
    lines = [
        "# 开源网表回归报告（ngspice 官方示例，BSD）",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M')}，抽样 {n} 个，总耗时 {time.time() - t_all:.0f}s",
        f"- 仿真通过：{pct('sim_ok')}（失败 {stat['sim_fail']}，无 raw {stat['no_raw']}）",
        f"- 波形解析成功：{pct('parse_ok')}（空信号 {stat['parse_empty']}，异常 {stat['parse_fail']}）",
        f"- 指标产出：{stat['metrics_ok']}，波形图：{stat['wave_ok']}，原理图：{stat['sch_ok']}",
        "",
        "## 失败明细（前 40）",
        *[f"- {r}: {msg}" for r, msg in fails[:40]],
    ]
    (Path(__file__).resolve().parent / "regress_report.md").write_text(
        "\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:8]))
    print(f"...完整报告 bench/regress_report.md（失败 {len(fails)} 项明细）")


if __name__ == "__main__":
    main()
