"""基准跑批：逐任务执行 pipeline，统计首次通过率/重试后通过率/耗时。

用法：
    python bench/run_bench.py [--quick] [--passes=N] [--backend=skidl] [--task=ID ...]
    --passes=N  每个任务独立跑 N 次（默认 1），输出 pass@1 与 pass@k 统计
                （无偏估计 pass@k = 1 - C(n-c,k)/C(n,k)），LLM 非确定性下
                这才是可比的通过率口径
    --task=ID   只跑指定任务（可重复），调试单个任务用
输出：bench/report_{backend}.md + 控制台摘要。通过率是核心质量指标。
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from math import comb
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import run_pipeline  # noqa: E402
from app.workspace import Workspace  # noqa: E402
from app import llm  # noqa: E402
from bench_validators import get_validators  # noqa: E402

BENCH = Path(__file__).resolve().parent


def pass_at_k(n: int, c: int, k: int) -> float:
    """无偏 pass@k：n 次独立运行通过 c 次，随机取 k 次至少一次通过的概率。"""
    if n <= 0 or k <= 0:
        return 0.0
    if k > n:
        k = n
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def main() -> None:
    argv = sys.argv[1:]
    passes = 1
    for a in argv:
        if a.startswith("--passes="):
            passes = max(1, int(a.split("=", 1)[1]))
    tasks = json.loads((BENCH / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    if "--quick" in argv:
        tasks = tasks[:4]
    only = {a.split("=", 1)[1] for a in argv if a.startswith("--task=")}
    if only:
        tasks = [t for t in tasks if t["id"] in only]
    backend = "skidl" if "--backend=skidl" in argv else "spice"
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + "-" + backend
    run_dir = BENCH / "runs" / run_id
    run_dir.mkdir(parents=True)
    workspace = Workspace((run_dir / "workspace").resolve())
    attempts = []

    rows = []
    for t in tasks:
        validators = get_validators(t)
        oks, retries_hist, elapsed_hist, first_oks = [], [], 0.0, []
        for _run in range(passes):
            t0 = time.monotonic()
            try:
                ev = run_pipeline(t["prompt"], backend=backend, validators=validators, workspace=workspace)
                ok = ev.ok and ev.checks and all(c["ok"] for c in ev.checks)
                retries = len([r for r in ev.retry_log if r["stage"] != "generate"])
                first_oks.append(bool(ev.provenance.get("first_attempt_passed")))
                attempts.append({"task":t["id"], "attempt":_run + 1, "evidence":ev.to_dict() |
                                 {"waveform_b64":None, "schematic_b64":None}})
                if _run == 0:  # 首轮打印明细，其余轮只计入统计（输出不刷屏）
                    for c in ev.checks:
                        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']} | {c['detail']}")
            except Exception as e:
                ok, retries = False, -1
                first_oks.append(False)
                attempts.append({"task":t["id"],"attempt":_run + 1,"error":str(e)})
                print(f"  [异常] {e}")
            oks.append(bool(ok))
            retries_hist.append(retries)
            elapsed_hist += time.monotonic() - t0
        n_ok = sum(oks)
        print(f"[{t['id']}] {n_ok}/{passes} 通过 | 平均 {elapsed_hist / passes:.1f}s")
        rows.append({
            "id": t["id"], "oks": oks, "n_ok": n_ok, "n": passes,
            "retries": max(retries_hist),
            "first_candidate_oks": first_oks,
            "elapsed": round(elapsed_hist / max(passes, 1), 1),
        })

    n_tasks = len(rows)
    agg_pass1 = sum(r["n_ok"] / r["n"] for r in rows) / max(n_tasks, 1)
    first_candidate = sum(sum(r["first_candidate_oks"]) / r["n"] for r in rows) / max(n_tasks, 1)
    k = min(passes, 3)
    agg_passk = sum(pass_at_k(r["n"], r["n_ok"], k) for r in rows) / max(n_tasks, 1)
    report = [
        f"# 基准报告（backend={backend}，指标级验收，passes={passes}）",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M')}",
        f"- 任务数：{n_tasks}（每任务独立运行 {passes} 次）",
        f"- 初次生成通过率（不含修复）：{first_candidate:.1%}",
        "- 下列 pass@1 为单次完整闭环任务成功率，每次内部最多修复 3 轮；pass@k 为独立运行口径。",
        f"- **pass@1：{agg_pass1:.1%}**　pass@{k}：{agg_passk:.1%}"
        if passes > 1 else
        f"- **pass@1：{agg_pass1:.1%}**（单次口径）",
        f"- 平均耗时：{sum(r['elapsed'] for r in rows) / max(n_tasks, 1):.1f}s/任务",
        "",
        "| 任务 | 通过 | 重试(最多) | 耗时(s/次) |" + (f" pass@{k} |" if passes > 1 else ""),
        "|---|---|---|---|" + ("---|" if passes > 1 else ""),
    ]
    for r in rows:
        line = (f"| {r['id']} | {r['n_ok']}/{r['n']} | {r['retries']} | {r['elapsed']} |")
        if passes > 1:
            line += f" {pass_at_k(r['n'], r['n_ok'], k):.0%} |"
        report.append(line)
    report.append(f"\n运行档案：runs/{run_id}/summary.json（完整指标、任务与独立运行记录）。")
    (run_dir / "report.md").write_text("\n".join(report), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps({"run_id":run_id, "backend":backend,
        "model":llm._MODEL, "base_url":llm.current_config().get("base_url"), "passes":passes,
        "first_candidate_pass_rate":first_candidate, "closed_loop_pass_at_1":agg_pass1,
        f"pass_at_{k}":agg_passk, "tasks":tasks, "rows":rows, "attempts":attempts},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (BENCH / f"report_{backend}.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\npass@1 {agg_pass1:.1%}"
          + (f" | pass@{k} {agg_passk:.1%}" if passes > 1 else "")
          + f"，报告已写入 bench/report_{backend}.md")
    sys.exit(0 if all(r["n_ok"] == r["n"] for r in rows) else 1)


if __name__ == "__main__":
    main()
