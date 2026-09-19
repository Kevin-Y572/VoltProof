"""单任务诊断：跑一次 pipeline，展示每轮失败原因和最终网表。

用法：python tests/diag_task.py "任务描述"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CIRCUITPILOT_NGSPICE",
                      "D:/Users/Lenovo/tools/ngspice-47/Spice64/bin/ngspice.exe")

from app.pipeline import run_pipeline  # noqa: E402


def main() -> None:
    req = " ".join(sys.argv[1:]) or "设计一个截止频率1kHz的RC低通滤波器"
    ev = run_pipeline(req)
    print("ok:", ev.ok, "| elapsed:", round(ev.elapsed, 1))
    for r in ev.retry_log:
        if r["stage"] != "generate":
            print(f"round {r['round']} {r['stage']}:")
            for p in r["problems"]:
                print("  *", p[:250])
    print("metrics:", json.dumps(dict(list(ev.metrics.items())[:8]), ensure_ascii=False))
    print("--- 最终网表 ---")
    print(ev.netlist)


if __name__ == "__main__":
    main()
