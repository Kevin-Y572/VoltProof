"""PCB 固定案例集真实布线统计（方案 §10 验收 / 验收报告第 6 条建议）。

对 pcb_cases.json 的全部支持案例逐个跑完整任务链（映射→造板→布线→回导→
分区+板级对照→DRC→制板导出），统计成功率与耗时；失败案例保留完整阶段
明细与原因。缺真实工具时退出码 2（与 pcb_smoke 同约定：跳过≠通过）。

运行：python scripts/pcb_bench.py [--mount=tht|smd] [--limit=N] [--passes=1]
产物：pcb/runs/bench-<id>/（逐案例子目录 + summary.json / report.md）
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pcb import jobs, kicad_backend, router_backend  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CASES = REPO / "app" / "pcb" / "tests_data" / "pcb_cases.json"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    mount = "tht"
    limit = 0
    only: set[str] = set()
    for a in argv:
        if a.startswith("--mount="):
            mount = a.split("=", 1)[1]
        elif a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        elif a.startswith("--only="):
            only = {x for x in a.split("=", 1)[1].split(",") if x}
    if kicad_backend.find_kicad() is None or router_backend.find_freerouting() is None:
        print("[SKIP] 需要 KiCad 与 Freerouting（VOLTPROOF_KICAD / VOLTPROOF_FREEROUTING）")
        return 2

    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = [c for c in data["supported"] if not c.get("skip")]
    if only:
        cases = [c for c in cases if c["id"] in only]
    if limit:
        cases = cases[:limit]
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-bench-" + uuid.uuid4().hex[:6]
    out_root = REPO / "pcb" / "runs" / run_id
    out_root.mkdir(parents=True)

    rows = []
    t_all = time.monotonic()
    for c in cases:
        t0 = time.monotonic()
        r = jobs.run_pcb_job(c["netlist"], c["ports"], out_root / c["id"],
                             options={"mount": mount}, max_passes=10, timeout_s=180)
        elapsed = time.monotonic() - t0
        failed_stage = next((s["stage"] for s in r["stages"] if s["ok"] is False), "")
        rows.append({"id": c["id"], "ok": r["ok"], "state": r["state"],
                     "elapsed": round(elapsed, 1), "failed_stage": failed_stage,
                     "blockers": (r.get("blockers") or [])[:2],
                     "version_id": r.get("version_id")})
        why = f"{failed_stage}" + (f": {rows[-1]['blockers'][0][:80]}"
                                   if rows[-1]["blockers"] else "")
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {c['id']} {elapsed:.1f}s"
              + (f" @ {why}" if failed_stage else ""))

    n = len(rows)
    n_ok = sum(r["ok"] for r in rows)
    oks_t = sorted(r["elapsed"] for r in rows if r["ok"])
    med = oks_t[len(oks_t) // 2] if oks_t else 0.0
    p95 = oks_t[int(len(oks_t) * 0.95)] if oks_t else 0.0
    summary = {
        "run_id": run_id, "mount": mount, "cases": n, "passed": n_ok,
        "success_rate": round(n_ok / n, 3) if n else 0,
        "elapsed_total": round(time.monotonic() - t_all, 1),
        "ok_median_s": med, "ok_p95_s": p95,
        "kicad": kicad_backend.find_kicad().version(),
        "freerouting": router_backend.find_freerouting().name,
        "rows": rows,
    }
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        f"# PCB 固定案例真实布线统计（{run_id}，mount={mount}）",
        f"- 工具：KiCad {summary['kicad']} + {summary['freerouting']}",
        f"- 案例：{n}　通过：{n_ok}（{summary['success_rate']:.0%}）",
        f"- 成功案例耗时：中位数 {med:.1f}s / P95 {p95:.1f}s；总耗时 {summary['elapsed_total']}s",
        "",
        "| 案例 | 结果 | 耗时 | 失败阶段/原因 |", "|---|---|---|---|",
    ]
    for r in rows:
        why = (f"{r['failed_stage']}: {r['blockers'][0][:60]}" if r["failed_stage"]
               and r["blockers"] else r["failed_stage"] or "")
        report.append(f"| {r['id']} | {'✓' if r['ok'] else '✗'} | {r['elapsed']}s | {why} |")
    (out_root / "report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\n{n_ok}/{n} 通过（{summary['success_rate']:.0%}）；成功中位 {med:.1f}s / P95 {p95:.1f}s"
          f"——档案 pcb/runs/{run_id}/")
    return 0 if n_ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
