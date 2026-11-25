"""PCB 阶段 A 冒烟：方案 §11 贯穿案例（RC 低通）全链路可复现脚本。

链路：板规格 → pcbnew 造板 → DSN → Freerouting JAR 布线 → SES 回导
→ GND 铺铜回填 → kicad-cli DRC(JSON) → Gerber+钻孔 → manifest 归档。

运行前置（缺工具时退出码 2 并给修复指引，与 test_offline 的 ngspice
预检同风格）：
  - KiCad 10.x：Linux 认发行版前缀（/usr/bin/kicad-cli + pcbnew）或
    VOLTPROOF_KICAD=<根目录>；Windows 便携树为 bin/kicad-cli.exe 与 bin/python.exe
  - Freerouting：设 VOLTPROOF_FREEROUTING=<freerouting-*.jar 路径>
  - Java 21+（跑 JAR）

产物落 pcb/runs/<时间戳-id>/（gitignore），manifest.json 含工具版本、
各步耗时与结果——这就是"可复现脚本 + 真实输出板"的阶段 A 交付物。

运行：python scripts/pcb_smoke.py [--keep-failed]
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pcb import kicad_backend, parts, router_backend  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

# 方案 §11：1kHz RC 低通贯穿案例。J1 靠左（in/GND），J2 靠右（out/GND），
# R1=1.59k C1=100n 居中；数值属于电路层，此处只做连接拓扑与物理封装。
RC_BOARD = {
    "name": "rc_lowpass",
    "size_mm": [50.0, 30.0],
    "nets": ["GND", "IN", "OUT"],
    "components": [
        {"ref": "J1", "lib": "Connector_PinHeader_2.54mm",
         "footprint": "PinHeader_1x02_P2.54mm_Vertical", "x": 12.0, "y": 15.0,
         "pads": ["IN", "GND"]},
        {"ref": "R1", "lib": "Resistor_THT",
         "footprint": "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
         "x": 25.0, "y": 9.0, "pads": ["IN", "OUT"]},
        {"ref": "C1", "lib": "Capacitor_THT",
         "footprint": "C_Disc_D5.0mm_W2.5mm_P2.50mm", "x": 25.0, "y": 21.0,
         "pads": ["OUT", "GND"]},
        {"ref": "J2", "lib": "Connector_PinHeader_2.54mm",
         "footprint": "PinHeader_1x02_P2.54mm_Vertical", "x": 38.0, "y": 15.0,
         "pads": ["OUT", "GND"]},
    ],
    "zones": [{"net": "GND", "layer": "B.Cu",
               "outline_mm": [[7, 7], [43, 7], [43, 23], [7, 23]]}],
}

# 阶段 B 里程碑：同一 RC 电路以【网表】为源——经 CircuitIR 与审校器件库
# 映射成板规格（自动布局+端子+铺铜），证明"验证过的网表 → 制板文件"闭环。
RC_NETLIST = "\n".join([
    "RC lowpass 1kHz",
    "Vin in 0 SIN(0 1 1k)",
    "R1 in out 1.59k",
    "C1 out 0 100n",
    ".end",
])
RC_PORTS = ["in", "out"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    kicad = kicad_backend.find_kicad()
    fr = router_backend.find_freerouting()
    missing = []
    if kicad is None:
        missing.append("KiCad 未找到：安装 KiCad 10.x，或设 VOLTPROOF_KICAD=<根目录>"
                       "（Linux：bin/kicad-cli 且 python3 能 import pcbnew；"
                       "Windows：bin/kicad-cli.exe 与 bin/python.exe）")
    if fr is None:
        missing.append("Freerouting 未找到：设 VOLTPROOF_FREEROUTING=<freerouting-*.jar>"
                       "（JVM JAR；Java 21 用 2.1.0，不要用 v2.5.0 原生 CLI）")
    java_err = router_backend.java_blocker(router_backend.find_java())
    if java_err:
        missing.append(java_err)
    if missing:
        for m in missing:
            print(f"[SKIP] {m}")
        return 2

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = REPO / "pcb" / "runs" / run_id
    run_dir.mkdir(parents=True)
    manifest: dict = {"run_id": run_id, "kicad": kicad.version(), "freerouting": fr.name,
                      "cases": []}
    print(f"PCB 冒烟 {run_id}：KiCad {manifest['kicad']} + {fr.name}")

    def run_case(case_name: str, spec: dict, case_dir: Path) -> bool:
        manifest["cases"].append({"case": case_name, "steps": []})
        steps = manifest["cases"][-1]["steps"]

        def step(sname: str, fn) -> bool:
            t0 = time.monotonic()
            try:
                detail = fn()
                ok, extra = True, detail
            except Exception as e:  # noqa: BLE001 —— 冒烟脚本汇总一切失败
                ok, extra = False, f"{type(e).__name__}: {e}"
            entry = {"step": sname, "ok": ok, "elapsed": round(time.monotonic() - t0, 2)}
            if isinstance(extra, str):
                entry["detail"] = extra[:2000]
            steps.append(entry)
            print(f"  [{'PASS' if ok else 'FAIL'}] {sname} {entry['elapsed']}s"
                  + (f" | {entry.get('detail', '')}" if not ok else ""))
            return ok

        pcb = case_dir / "board.kicad_pcb"
        dsn = case_dir / "board.dsn"
        ses = case_dir / "board.ses"
        drc_json = case_dir / "drc.json"
        fab_dir = case_dir / "fab"

        def s_build():
            ok, out = kicad_backend.build_board(kicad, spec, pcb, case_dir)
            assert ok, out
            return f"{pcb.name} {pcb.stat().st_size}B"

        def s_dsn():
            ok, out = kicad_backend.export_dsn(kicad, pcb, dsn, case_dir)
            assert ok, out
            return f"{dsn.name} {dsn.stat().st_size}B"

        def s_route():
            rr = router_backend.route(fr, dsn, ses, case_dir / "router",
                                      max_passes=10, timeout_s=180)
            assert rr.ok, "; ".join(rr.problems) or rr.summary
            return rr.summary

        def s_ses():
            ok, out = kicad_backend.import_ses(kicad, pcb, ses, case_dir, fill_zones=True)
            assert ok, out
            return next((ln for ln in out.splitlines() if ln.startswith("TRACKS:")), "?")

        def s_drc():
            res = kicad_backend.drc(kicad, pcb, drc_json)
            assert res["ok"], res["stdout"] + res["stderr"]
            assert res["violations"] == 0 and res["unconnected"] == 0, \
                f"DRC 违规 {res['violations']} / 未连 {res['unconnected']}"
            return "0 违规 / 0 未连接"

        def s_fab():
            ok, out = kicad_backend.export_fab(kicad, pcb, fab_dir)
            assert ok, out[-300:]
            files = sorted(p.name for p in fab_dir.iterdir())
            assert any(f.endswith(".gbrjob") for f in files), "缺 .gbrjob"
            assert any(f.endswith(".drl") for f in files), "缺钻孔文件"
            assert any(f.endswith("-F_Cu.gtl") for f in files), "缺顶铜 Gerber"
            return f"{len(files)} 个制板文件"

        results = [step(n, f) for n, f in (
            ("造板", s_build), ("DSN 导出", s_dsn), ("Freerouting 布线", s_route),
            ("SES 回导+铺铜", s_ses), ("DRC", s_drc), ("制板导出", s_fab))]
        case_ok = all(results)
        for p in (pcb, dsn, ses):
            if p.is_file():
                manifest["cases"][-1].setdefault("hashes", {})[p.name] = sha256(p)
        return case_ok

    # 案例 1：手写板规格（方案 §11 贯穿案例——工具链冒烟）
    spec_dir = run_dir / "spec_case"
    spec_dir.mkdir()
    (spec_dir / "spec.json").write_text(
        json.dumps(RC_BOARD, ensure_ascii=False, indent=2), encoding="utf-8")
    ok1 = run_case("spec：手写板规格", RC_BOARD, spec_dir)

    # 案例 2：网表为源（阶段 B 里程碑——走任务编排层 jobs.run_pcb_job：
    # 映射→造板→布线→回导→分区对照/DRC→制板导出，§6.1 归档结构）
    from app.pcb import jobs as pcb_jobs
    net_dir = run_dir / "netlist_case"
    net_dir.mkdir()

    def on_stage(name: str, state: str, detail) -> None:
        if state == "start":  # 结果由下方阶段汇总打印，这里只演示流式回调
            print(f"    ▶ {name}")

    job = pcb_jobs.run_pcb_job(RC_NETLIST, RC_PORTS, net_dir, on_stage=on_stage)
    for s in job["stages"]:
        print(f"  [{'PASS' if s['ok'] else 'FAIL'}] jobs:{s['stage']} "
              f"{s['detail'][:90]}")
    ok2 = job["ok"]
    if not ok2:
        print(f"  任务失败：state={job['state']} blockers={job.get('blockers')}")
    manifest["cases"].append({"case": "netlist：jobs.run_pcb_job", "job": {
        k: v for k, v in job.items() if k != "stages"}})

    manifest["ok"] = ok1 and ok2
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(("通过" if manifest["ok"] else "失败")
          + f"——档案：pcb/runs/{run_id}/（manifest.json 含两案例全步耗时与哈希）")
    return 0 if manifest["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
