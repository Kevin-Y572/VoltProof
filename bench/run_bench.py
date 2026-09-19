"""基准跑批：逐任务执行 pipeline，统计首次通过率/重试后通过率/耗时。  [W2]

用法：python bench/run_bench.py [--quick]
输出：bench/report.md + 控制台摘要。通过率是每周要看的核心质量指标。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import run_pipeline  # noqa: E402

BENCH = Path(__file__).resolve().parent


def main() -> None:
    tasks = json.loads((BENCH / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    if "--quick" in sys.argv:
        tasks = tasks[:4]
    backend = "skidl" if "--backend=skidl" in sys.argv else "spice"

    rows = []
    for t in tasks:
        t0 = time.monotonic()
        try:
            ev = run_pipeline(t["prompt"], backend=backend)
            ok = ev.ok
            retries = len([r for r in ev.retry_log if r["stage"] != "generate"])
        except Exception as e:
            ok, retries = False, -1
            print(f"[{t['id']}] 异常: {e}")
        rows.append({
            "id": t["id"],
            "ok": ok,
            "retries": retries,
            "elapsed": round(time.monotonic() - t0, 1),
        })
        print(f"[{t['id']}] {'通过' if ok else '失败'} | 重试 {retries} 次 | {rows[-1]['elapsed']}s")

    passed = sum(r["ok"] for r in rows)
    avg_time = sum(r["elapsed"] for r in rows) / max(len(rows), 1)
    report = [
        f"# 基准报告（backend={backend}）",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M')}",
        f"- 通过：{passed}/{len(rows)}（{passed / len(rows):.0%}）",
        f"- 平均耗时：{avg_time:.1f}s",
        "",
        "| 任务 | 结果 | 重试次数 | 耗时(s) |",
        "|---|---|---|---|",
    ]
    report += [f"| {r['id']} | {'✅' if r['ok'] else '❌'} | {r['retries']} | {r['elapsed']} |" for r in rows]
    (BENCH / f"report_{backend}.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\n通过率 {passed}/{len(rows)}，报告已写入 bench/report_{backend}.md")
    sys.exit(0 if passed == len(rows) else 1)


if __name__ == "__main__":
    main()
