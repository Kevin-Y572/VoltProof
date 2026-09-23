"""信号波形合成实验能力测试跑批。

任务集：bench/exp_tasks.json（提示词逐条来自实验任务书）。
执行方式与真实用户一致：T1 全新设计，T2-T4 走会话链（在上一轮验证通过的
网表上继续修改），每任务留存完整证据到 bench/exp/<task_id>/。

用法：python bench/run_exp.py [--backend=skidl]
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
    backend = "skidl" if "--backend=skidl" in sys.argv else "spice"
    outdir = OUT.parent / f"exp_skidl" if backend == "skidl" else OUT
    outdir.mkdir(parents=True, exist_ok=True)
    summary = []
    prev_netlist: str | None = None  # 会话链：上一轮验收通过的网表

    for t in tasks:
        tdir = outdir / t["id"]
        tdir.mkdir(parents=True, exist_ok=True)
        # 清掉上一轮残留：独立验收器取目录内 raw，跨轮残留会读到旧数据
        for old in tdir.glob("*"):
            old.unlink()
        print(f"\n===== [{t['id']}] {t['part']} (backend={backend}) =====")
        print(f"prompt: {t['prompt']}")
        t0 = time.monotonic()
        try:
            # 实验电路（振荡+分频+滤波链）比 bench 任务重：放宽重试预算
            ev = run_pipeline(t["prompt"], previous_netlist=prev_netlist,
                              max_retries=5, validators=get_validators(t["id"]),
                              backend=backend)
        except Exception as e:
            print(f"[{t['id']}] 管道异常: {e}")
            summary.append({"id": t["id"], "ok": False, "error": str(e),
                            "elapsed": round(time.monotonic() - t0, 1)})
            continue
        checks_ok = ev.ok and ev.checks and all(c["ok"] for c in ev.checks)
        if checks_ok:
            prev_netlist = ev.netlist  # 只有验收通过的电路才进入会话链

        # 留存证据：网表 / raw / 波形 / 证据 JSON。产物已按任务落进工作区
        # sim/<task_id>/ 与 docs/<task_id>/（ev.task_dir 即 docs 目录），
        # 不再依赖共享 out/ 的平铺布局
        (tdir / "netlist.cir").write_text(ev.netlist, encoding="utf-8")
        if ev.task_dir:
            doc_dir = Path(ev.task_dir)
            sim_dir = doc_dir.parent.parent / "sim" / doc_dir.name
            for f in doc_dir.glob("*.png"):
                shutil.copy2(f, tdir / f.name)
            for f in sim_dir.glob("*.raw"):
                shutil.copy2(f, tdir / f.name)
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
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
