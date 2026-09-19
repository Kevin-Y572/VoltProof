"""综合实验（信号波形合成实验）能力测试跑批。

任务集：bench/exp_tasks.json（提示词逐条来自《综合实验要求.docx》）。
执行方式与真实用户一致：T1 全新设计，T2-T4 走会话链（在上一轮验证通过的
网表上继续修改），每任务留存完整证据到 bench/exp/<task_id>/。

用法：python bench/run_exp.py
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import run_pipeline  # noqa: E402
from exp_validators import get_validators  # noqa: E402

BENCH = Path(__file__).resolve().parent
OUT = BENCH / "exp"


def main() -> None:
    tasks = json.loads((BENCH / "exp_tasks.json").read_text(encoding="utf-8"))["tasks"]
    summary = []
    prev_netlist: str | None = None  # 会话链：上一轮验收通过的网表

    for t in tasks:
        tdir = OUT / t["id"]
        tdir.mkdir(parents=True, exist_ok=True)
        print(f"\n===== [{t['id']}] {t['part']} =====")
        print(f"prompt: {t['prompt']}")
        t0 = time.monotonic()
        try:
            ev = run_pipeline(t["prompt"], previous_netlist=prev_netlist,
                              validators=get_validators(t["id"]))
        except Exception as e:
            print(f"[{t['id']}] 管道异常: {e}")
            summary.append({"id": t["id"], "ok": False, "error": str(e),
                            "elapsed": round(time.monotonic() - t0, 1)})
            continue
        checks_ok = ev.ok and ev.checks and all(c["ok"] for c in ev.checks)
        if checks_ok:
            prev_netlist = ev.netlist  # 只有验收通过的电路才进入会话链

        # 留存证据：网表 / raw / 波形 / 证据 JSON
        (tdir / "netlist.cir").write_text(ev.netlist, encoding="utf-8")
        app_out = BENCH.parent / "out"
        for f in app_out.glob("*.raw"):
            shutil.copy2(f, tdir / f.name)
        for f in app_out.glob("wave.png"):
            shutil.copy2(f, tdir / "wave.png")
        for f in app_out.glob("schematic.png"):
            shutil.copy2(f, tdir / "schematic.png")
        evd = ev.to_dict()
        (tdir / "evidence.json").write_text(
            json.dumps(evd, ensure_ascii=False, indent=2), encoding="utf-8")

        retries = len([r for r in ev.retry_log if r["stage"] != "generate"])
        summary.append({"id": t["id"], "ok": checks_ok, "sim_ok": ev.ok, "retries": retries,
                        "elapsed": ev.elapsed, "metrics": ev.metrics,
                        "checks": ev.checks})
        print(f"[{t['id']}] {'验收通过' if checks_ok else ('仿真通过但验收未达标' if ev.ok else '失败')}"
              f" | 重试 {retries} | {ev.elapsed:.1f}s")
        for c in ev.checks:
            print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']} | {c['detail']}")

    passed = sum(1 for s in summary if s["ok"])
    print(f"\n===== 摘要: 验收通过 {passed}/{len(summary)} =====")
    for s in summary:
        print(f"  {s['id']}: {'ok' if s['ok'] else 'FAIL'} retries={s.get('retries')} "
              f"elapsed={s.get('elapsed')}s")
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
