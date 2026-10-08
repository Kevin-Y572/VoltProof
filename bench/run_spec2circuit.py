"""外部公开 benchmark 接入：skidl-spec2circuit（代码 MIT / 数据 CC-BY-4.0）。

来源 https://github.com/Kevin1289/skidl-spec2circuit —— 经 cdn.jsdelivr.net
原样下载到 bench/external/skidl-spec2circuit/（该目录不入库，见 .gitignore；
下载时连同 LICENSE 与 LICENSE-DATA 一并放置，不改动任何 benchmark 文件）。

选它的理由：判分哲学与 VoltProof 同源。它的 verifier 注释原话是——错误电路
（R/C 互换、Rf/Rg 互换、MOSFET 漏源接反）**能仿真通过并返回数字**，所以
"跑通了"毫无用处，只有对 golden FOM 做容差比对才抓得住。6 个任务每个埋一个
LLM 常犯的错，正好是 VoltProof "指标级验收" 主张的第三方佐证。

判分纪律：**完全复用 benchmark 自己的 loader/measure/runner/verifiers**，
本脚本不自实现任何判分逻辑，也不采信 VoltProof 自报的 ok/metrics。
VoltProof 只负责产出电路；判分用的 .control 块（扫描范围 + wrdata）从
benchmark 的参考电路里原样取出，保证与它 CI 的口径一致。

用法：
    python bench/run_spec2circuit.py --selfcheck              # 判它自带的 correct/wrong 候选，零 API 消耗
    python bench/run_spec2circuit.py --gen                    # VoltProof 真实生成 + benchmark 判分
    python bench/run_spec2circuit.py --gen --task 03-sallenkey-lpf --backend skidl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# benchmark 的 loader.py / netlist_sanity 读文件时用 read_text() **不带 encoding**，
# Windows 默认 GBK 码页下解析它自己 UTF-8 的 task.json（含 ≈ / — 等字符）会直接
# UnicodeDecodeError——该 benchmark 在 Windows 上开箱即坏。用解释器级 UTF-8 模式
# 解决（顺带修好本脚本的中文控制台输出），不改动任何 benchmark 文件。
if not sys.flags.utf8_mode:
    os.environ["PYTHONUTF8"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])

REPO = Path(__file__).resolve().parent.parent
EXT = REPO / "bench" / "external" / "skidl-spec2circuit"
OUT = REPO / "bench" / "external" / "out_spec2circuit"
REPORT = REPO / "bench" / "external" / "report_spec2circuit.md"

# 基准完整性：禁用 VoltProof 的结果缓存，否则重跑会拿到上一次的证据而不是真实生成
os.environ["VOLTPROOF_CACHE"] = "0"

SIM_TIMEOUT = int(os.environ.get("VOLTPROOF_SIM_TIMEOUT", "60"))


def ensure_ngspice() -> str:
    """benchmark 的 measure.run_ngspice 走 shutil.which("ngspice") + 裸命令名，
    所以必须让它出现在 PATH 上（只在 VOLTPROOF_NGSPICE 里配是不够的）。"""
    found = shutil.which("ngspice")
    if found:
        return found
    cand = Path(os.environ.get("VOLTPROOF_NGSPICE") or "")
    if not cand.is_file():
        sys.exit("找不到 ngspice：PATH 上没有，且未设 VOLTPROOF_NGSPICE。\n"
                 "请把 ngspice 加入 PATH，或设 VOLTPROOF_NGSPICE 指向 ngspice.exe")
    os.environ["PATH"] = f"{cand.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ.setdefault("VOLTPROOF_NGSPICE", str(cand))
    return str(cand)


def patch_sim_timeout() -> None:
    """benchmark 的 run_ngspice 调 subprocess.run 时**没有传 timeout**——
    一个收敛不了的网表会把整轮基准永久挂住（VoltProof 自己踩过这个坑）。
    只补 timeout，不改任何判分行为。"""
    original = measure.subprocess.run

    def guarded(*args, **kwargs):
        kwargs.setdefault("timeout", SIM_TIMEOUT)
        return original(*args, **kwargs)

    measure.subprocess.run = guarded


NGSPICE = ensure_ngspice()
sys.path.insert(0, str(EXT))
sys.path.insert(0, str(REPO))

from spec2circuit import loader, measure, runner  # noqa: E402
from spec2circuit.verifiers.spec_compliance import _within  # noqa: E402  复用同一套容差语义

patch_sim_timeout()

_CTRL_RE = re.compile(r"\.control\b.*?\.endc\b", re.IGNORECASE | re.DOTALL)
_END_RE = re.compile(r"^\s*\.end\s*$", re.IGNORECASE | re.MULTILINE)
_NODE_RE = re.compile(r"\bv(?:db|p|ri)?\(\s*([^)\s]+)\s*\)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 判分网表构造：VoltProof 的电路 + benchmark 的 .control（口径对齐）
# ---------------------------------------------------------------------------

def ref_control_block(task) -> str | None:
    """从 benchmark 参考电路里原样取出 .control 块。"""
    p = task.dir / "candidates" / "correct.cir"
    if not p.is_file():
        return None
    m = _CTRL_RE.search(p.read_text(encoding="utf-8"))
    return m.group(0) if m else None


def to_grading_netlist(circuit: str, ctrl: str) -> str:
    """换掉 VoltProof 自己的 .control（它写 out.raw 供产品出图），
    换上 benchmark 的 wrdata 块（它读 mag.txt 算 FOM）。电路本体一字不改。"""
    body = _END_RE.sub("", _CTRL_RE.sub("", circuit)).strip()
    return f"{body}\n{ctrl}\n.end\n"


def missing_interface_nodes(circuit: str, ctrl: str) -> list[str]:
    """benchmark 的 wrdata 按名取节点（vdb(out) 等）； VoltProof 若没按题面
    约定的节点名画图，ngspice 会报错。这里先静态识别，好把"接口没对上"
    与"电路指标不达标"区分开——两者的根因和结论完全不同。"""
    tokens = set(re.findall(r"^\s*\w+\s+(.+)$", circuit, re.MULTILINE))
    text = " ".join(tokens).lower()
    return sorted({n for n in _NODE_RE.findall(ctrl)
                   if n.lower() not in ("0", "gnd") and not re.search(rf"[\s]{re.escape(n.lower())}[\s]", text)})


def grade(task, netlist_text: str, topology_text: str | None = None):
    """用 benchmark 自己的代码判分。返回 (verdict, produced_foms, error)。

    topology_text：供 netlist_sanity 检查的**纯电路本体**（已剥掉 .control）。
    benchmark 的悬空节点检查器按首字母猜端子数、只跳过 `*`/`.` 开头的行，
    于是 `.control` 块里的 `set filetype=ascii`、`write out.raw v(in) v(out)`、
    `tran 1u 200u` 会被当元件行解析出 'filetype=ascii'、'out.raw' 等假悬空节点。
    它自带的参考电路没有 .control 块所以不受影响——那是仿真脚手架，不是拓扑。
    """
    names = {s["name"] for s in task.verifier_specs}
    try:
        produced: dict = {}
        if "spec_compliance" in names:
            produced.update(measure.simulate_and_extract(netlist_text, task.expected))
        if "netlist_sanity" in names:
            produced["netlist"] = topology_text if topology_text is not None else netlist_text
    except subprocess.TimeoutExpired:
        return None, {}, f"ngspice 超时（>{SIM_TIMEOUT}s）"
    except Exception as exc:
        return None, {}, f"{type(exc).__name__}: {str(exc)[:300]}"
    return runner.grade_produced(task, produced), produced, None


def spec_lens(task, produced: dict):
    """第二把尺子：拿题面 spec（task.spec 里写的目标值）判，而不是 golden。

    这两者理论上应该一致，实测并不总是——03-sallenkey-lpf 题面写"-3dB 截止
    近 1kHz"，golden 却是 411.4Hz（参考电路 R1=R2=10k/C1=22n/C2=11n 的
    unity-gain Sallen-Key Q=0.5，-3dB 点本就远低于 ω0）。也就是说该任务
    **真正满足题面的电路会被 benchmark 判失败**。两把尺子都报，不偷偷改容差。
    """
    tolerances: dict = {}
    for s in task.verifier_specs:
        tolerances.update(s.get("params", {}).get("tolerances", {}))
    fails, checked = [], 0
    for key, exp in (task.spec or {}).items():
        if not isinstance(exp, (int, float)):
            continue
        checked += 1
        if key not in produced:
            fails.append(f"{key}: 未测到")
            continue
        tol = tolerances.get(key, {"rel": 0.05})
        if not _within(float(produced[key]), float(exp), tol):
            fails.append(f"{key}: 实测 {produced[key]:.4g} vs 题面 {exp:.4g}（tol {tol}）")
    if not checked:
        return None, []
    return not fails, fails


# ---------------------------------------------------------------------------
# 模式一：自校验——判 benchmark 自带的 correct/wrong 候选（零 API 消耗）
# ---------------------------------------------------------------------------

def run_selfcheck(task_ids: list[str] | None) -> list[dict]:
    print(f"ngspice: {NGSPICE}")
    print("自校验：用 benchmark 自己的 harness 判它自带的候选电路\n")
    rows = []
    for task, cands in loader.load_all():
        if task_ids and task.id not in task_ids:
            continue
        for c in cands:
            t0 = time.monotonic()
            v = runner.run_live(task, c)
            dt = time.monotonic() - t0
            expect_pass = c.label == "correct"
            rows.append({
                "task": task.id, "candidate": c.name, "label": c.label,
                "failure_class": c.failure_class, "passed": v.passed,
                "score": round(v.score, 3), "detail": v.detail,
                "harness_agrees": v.passed == expect_pass, "elapsed": round(dt, 2),
            })
            mark = "✅" if v.passed else "❌"
            agree = "" if v.passed == expect_pass else "  ← 与 label 矛盾！"
            print(f"  {mark} {task.id:22} {c.name:22} label={c.label:8} "
                  f"score={v.score:.2f} ({dt:.1f}s){agree}")
            if not v.passed == expect_pass or not v.passed:
                print(f"      {v.detail[:160]}")
    bad = [r for r in rows if not r["harness_agrees"]]
    print(f"\n自校验结论：{len(rows) - len(bad)}/{len(rows)} 与 benchmark 标注一致"
          + (f"，{len(bad)} 项矛盾：{[r['candidate'] for r in bad]}" if bad else "（harness 在本机可复现）"))
    return rows


# ---------------------------------------------------------------------------
# 模式二：VoltProof 真实生成 → benchmark 判分
# ---------------------------------------------------------------------------

def run_gen(task_ids: list[str] | None, backend: str) -> list[dict]:
    from app.pipeline import run_pipeline
    from app.workspace import Workspace

    ws = OUT / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(ws)

    print(f"ngspice: {NGSPICE}")
    print(f"后端: {backend}｜缓存: 已禁用｜判分: benchmark 原生 harness\n")

    rows = []
    for task, _cands in loader.load_all():
        if task_ids and task.id not in task_ids:
            continue
        d = OUT / task.id
        d.mkdir(parents=True, exist_ok=True)
        print(f"── {task.id}（{task.difficulty}/{task.category}）")
        print(f"   题面: {task.prompt[:110]}")

        gen_err = ""
        t0 = time.monotonic()
        try:
            ev = run_pipeline(task.prompt, backend=backend, workspace=workspace)
        except Exception as exc:
            ev = None
            gen_err = f"{type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - t0

        row: dict = {
            "task": task.id, "difficulty": task.difficulty, "category": task.category,
            "backend": backend, "elapsed": round(elapsed, 1),
            "self_ok": bool(ev and ev.ok), "self_metrics": (ev.metrics if ev else {}) or {},
            "retries": len([r for r in (ev.retry_log if ev else []) if r.get("problems")]),
            "gen_error": gen_err,
        }

        netlist = (ev.netlist if ev else "") or ""
        if not netlist.strip():
            row.update(verdict="no_netlist", detail=gen_err or "VoltProof 未产出网表")
            rows.append(row)
            print(f"   ❌ 未产出网表（{elapsed:.0f}s）\n")
            continue

        (d / "voltproof.cir").write_text(netlist, encoding="utf-8")
        needs_sim = any(s["name"] == "spec_compliance" for s in task.verifier_specs)
        ctrl = ref_control_block(task)
        if needs_sim and ctrl is None:
            row.update(verdict="no_reference_control", detail="benchmark 无参考 .control 块")
            rows.append(row)
            continue

        # 06-divider 这类任务只用 netlist_sanity 静态判分，本身不仿真、
        # 参考电路里也没有 .control 块——直接把网表文本交给判分器即可。
        iface = missing_interface_nodes(netlist, ctrl) if ctrl else []
        grading = to_grading_netlist(netlist, ctrl) if ctrl else netlist
        topology = _END_RE.sub("", _CTRL_RE.sub("", netlist)).strip()
        (d / "grading.cir").write_text(grading, encoding="utf-8")

        verdict, produced, err = grade(task, grading, topology_text=topology)
        spec_ok, spec_fails = spec_lens(task, produced)
        foms = {k: v for k, v in produced.items() if isinstance(v, (int, float))}
        row.update({
            "produced_foms": foms,
            "golden": task.golden if isinstance(task.golden, dict) else {},
            "spec_targets": task.spec,
            "interface_nodes_missing": iface,
            "sim_error": err or "",
            "verdict": ("pass" if verdict.passed else "fail") if verdict else "sim_error",
            "score": round(verdict.score, 3) if verdict else 0.0,
            "detail": (verdict.detail if verdict else err) or "",
            "spec_lens_pass": spec_ok, "spec_lens_fails": spec_fails,
            "voltproof_interpretation": (ev.interpretation if ev else "")[:400],
        })
        (d / "verdict.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=1), encoding="utf-8")

        mark = {"pass": "✅", "fail": "❌", "sim_error": "💥"}.get(row["verdict"], "❓")
        print(f"   VoltProof 自评 ok={row['self_ok']}｜benchmark 判分 {mark} {row['verdict']}"
              f"（score={row['score']:.2f}，{elapsed:.0f}s，重试 {row['retries']} 轮）")
        if iface:
            print(f"   ⚠ 题面约定节点缺失: {iface}")
        if foms:
            print(f"   实测 FOM: {json.dumps({k: round(v, 4) for k, v in foms.items()}, ensure_ascii=False)}")
            print(f"   golden  : {json.dumps({k: round(float(v), 4) for k, v in row['golden'].items() if isinstance(v, (int, float))}, ensure_ascii=False)}")
        if row["detail"]:
            print(f"   判分: {row['detail'][:200]}")
        if spec_ok is not None and spec_ok != (row["verdict"] == "pass"):
            print(f"   ⚠ 两把尺子结论不一致｜按题面 spec 判: {'通过' if spec_ok else '不通过'} {spec_fails}")
        rows.append(row)
        print()

    return rows


def write_report(selfcheck_rows: list[dict], gen_rows: list[dict], backend: str) -> None:
    lines = [
        "# 外部公开 benchmark 测试报告：skidl-spec2circuit",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M')}",
        f"- benchmark：[skidl-spec2circuit](https://github.com/Kevin1289/skidl-spec2circuit)"
        "（代码 MIT / 数据 CC-BY-4.0，经 jsdelivr CDN 原样下载，未改动）",
        f"- ngspice：`{NGSPICE}`",
        f"- 判分：**全部由 benchmark 自带的 loader/measure/runner/verifiers 执行**，"
        "本仓库未自实现判分逻辑，也不采信 VoltProof 自报的 ok/metrics",
        f"- VoltProof 结果缓存已禁用（`VOLTPROOF_CACHE=0`），每条都是真实生成",
        "",
    ]

    if selfcheck_rows:
        agree = sum(1 for r in selfcheck_rows if r["harness_agrees"])
        lines += [
            "## 一、harness 自校验（判 benchmark 自带候选，零 API 消耗）",
            "",
            f"目的：先证明判分链路在本机可复现——`correct` 必须过、`wrong` 必须不过。"
            f"结果 **{agree}/{len(selfcheck_rows)} 与标注一致**。",
            "",
            "| 任务 | 候选 | 标注 | 判定 | score | 埋的错 |",
            "|---|---|---|---|---|---|",
        ]
        for r in selfcheck_rows:
            lines.append(f"| {r['task']} | `{r['candidate']}` | {r['label']} | "
                         f"{'✅ pass' if r['passed'] else '❌ fail'} | {r['score']:.2f} | "
                         f"{r['failure_class'] or '—'} |")
        lines += ["", "<details><summary>判分明细</summary>", ""]
        lines += [f"- `{r['task']}/{r['candidate']}`: {r['detail'][:200]}" for r in selfcheck_rows]
        lines += ["", "</details>", ""]

    if gen_rows:
        n = len(gen_rows)
        passed = sum(1 for r in gen_rows if r.get("verdict") == "pass")
        spec_passed = sum(1 for r in gen_rows if r.get("spec_lens_pass") is True)
        self_ok = sum(1 for r in gen_rows if r.get("self_ok"))
        avg = sum(r.get("elapsed", 0) for r in gen_rows) / n if n else 0
        lines += [
            f"## 二、VoltProof 真实生成 → benchmark 判分（backend={backend}）",
            "",
            f"- **benchmark golden 口径：{passed}/{n} 通过**",
            f"- 题面 spec 口径（第二把尺子）：{spec_passed}/{n} 通过",
            f"- VoltProof 自评 ok：{self_ok}/{n}（与外部判分的差值即"
            f"{'存在虚报' if self_ok > passed else '无虚报'}）",
            f"- 平均耗时：{avg:.0f}s/任务",
            "",
            "| 任务 | 难度 | VoltProof 自评 | benchmark 判定 | score | 题面 spec 判 | 耗时(s) | 重试 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for r in gen_rows:
            sl = r.get("spec_lens_pass")
            lines.append(
                f"| {r['task']} | {r.get('difficulty', '')} | "
                f"{'ok' if r.get('self_ok') else '—'} | "
                f"{'✅ pass' if r.get('verdict') == 'pass' else '❌ ' + str(r.get('verdict'))} | "
                f"{r.get('score', 0):.2f} | "
                f"{'—' if sl is None else ('✅' if sl else '❌')} | "
                f"{r.get('elapsed', 0):.0f} | {r.get('retries', 0)} |")
        lines += ["", "### 逐任务实测 FOM vs golden", ""]
        for r in gen_rows:
            lines += [f"**{r['task']}**", ""]
            if r.get("sim_error"):
                lines += [f"- 仿真/提取失败：`{r['sim_error'][:200]}`"]
            if r.get("interface_nodes_missing"):
                lines += [f"- ⚠ 题面约定节点缺失：`{r['interface_nodes_missing']}`"
                          "（电路可能没错，但没按题面给的节点名接口，benchmark 取不到量）"]
            if r.get("produced_foms"):
                lines += ["", "| 量 | VoltProof 实测 | golden | 题面 spec |", "|---|---|---|---|"]
                golden = r.get("golden") or {}
                spec = r.get("spec_targets") or {}
                for k, v in r["produced_foms"].items():
                    lines.append(f"| `{k}` | {float(v):.4g} | "
                                 f"{golden.get(k, '—') if not isinstance(golden.get(k), (int, float)) else format(float(golden[k]), '.4g')} | "
                                 f"{spec.get(k, '—') if not isinstance(spec.get(k), (int, float)) else format(float(spec[k]), '.4g')} |")
                lines += [""]
            if r.get("detail"):
                lines += [f"- 判分：{r['detail'][:300]}"]
            if r.get("spec_lens_fails"):
                lines += [f"- 题面 spec 不达标项：{'; '.join(r['spec_lens_fails'])[:300]}"]
            lines += [""]

    OUT.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    (OUT / "results.json").write_text(
        json.dumps({"selfcheck": selfcheck_rows, "gen": gen_rows,
                    "ngspice": NGSPICE, "backend": backend,
                    "ts": time.strftime("%Y-%m-%d %H:%M")},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告：{REPORT}")
    print(f"原始数据：{OUT / 'results.json'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfcheck", action="store_true", help="判 benchmark 自带候选（零 API 消耗）")
    ap.add_argument("--gen", action="store_true", help="VoltProof 真实生成后判分（消耗 API）")
    ap.add_argument("--task", action="append", dest="tasks", help="只跑指定任务 id，可重复")
    ap.add_argument("--backend", default="spice", choices=["spice", "skidl"])
    args = ap.parse_args()

    if not EXT.is_dir():
        sys.exit(f"未找到 benchmark：{EXT}\n请先下载到该目录（含 LICENSE/LICENSE-DATA）")
    if not args.selfcheck and not args.gen:
        args.selfcheck = True

    sc = run_selfcheck(args.tasks) if args.selfcheck else []
    gen = run_gen(args.tasks, args.backend) if args.gen else []
    write_report(sc, gen, args.backend)


if __name__ == "__main__":
    main()
