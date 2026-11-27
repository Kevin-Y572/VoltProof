"""布局代理第一阶段：约束、合法化、模拟模型、LM358 候选。不需要 KiCad 或 API key。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("VOLTPROOF_SETTINGS", str(
    Path(__file__).resolve().parent / f"_llm_settings_agent_{os.getpid()}.json"))

from fastapi.testclient import TestClient  # noqa: E402

from app import main as app_main  # noqa: E402
from app import project  # noqa: E402
from app.circuit_ir import netlist_to_ir  # noqa: E402
from app.pcb import agent, connectivity, constraints, jobs, kicad_backend, legalizer  # noqa: E402
from app.pcb import opamp_realize, parts, router_backend, schema  # noqa: E402
from app.workspace import Workspace  # noqa: E402

FAILURES: list[str] = []

RC = "\n".join([
    "RC lowpass 1kHz",
    "Vin in 0 SIN(0 1 1k)",
    "R1 in out 1.59k",
    "C1 out 0 100n",
    ".end",
])
IDEAL = "\n".join([
    "* inverting -- VoltProof",
    "V1 in 0 SIN(0 0.1 1k)",
    "Rin in ninv 10k",
    "Rf out ninv 100k",
    "XU1 ninv 0 out OPAMP",
    ".subckt OPAMP nn np vout",
    "E1 vout 0 np nn 200k",
    ".ends",
    ".control",
    "set filetype=ascii",
    "tran 10u 1m",
    "write out.raw v(out) v(ninv) v(in)",
    ".endc",
    ".end",
])


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def _xy(spec: dict) -> dict:
    return {c["ref"]: (c["x"], c["y"], c.get("rot", 0)) for c in spec["components"]}


def _pads(spec: dict) -> list[dict]:
    entries = {e["id"]: e for e in parts.load_catalog()["entries"]}
    pads = []
    for comp in spec["components"]:
        entry = entries[comp["part"]]
        for pad_no, net in zip(sorted(entry["pad_functions"], key=int), comp.get("pads") or []):
            pads.append({"ref": comp["ref"], "pad_no": pad_no, "net": net,
                         "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})
    return pads


def _route(ok: bool, unrouted: int, problems: list[str]):
    return router_backend.RouteResult(
        ok=ok, timed_out=False, unrouted=unrouted, violations=0, score=1.0,
        ses_written=ok, elapsed=0.1, log="", problems=problems)


def test_constraints_fail_closed() -> None:
    print("约束：校验失败即闭合，不删硬约束")
    ir = netlist_to_ir(RC)
    mapped = parts.map_netlist(RC, ["in", "out"])
    assert mapped["ok"]
    rules = constraints.build(ir, mapped["spec"], schema.default_rules())
    check("RC 约束含地网络类且线宽不低于底线",
          any(c["name"] == "Ground" and c["track_mm"] >= rules["track_mm"] for c in rules["net_classes"])
          and rules["track_mm"] == 0.2)
    check("连接器在靠边约束里",
          {c["ref"] for c in mapped["spec"]["components"] if c["part"] == "CONN_HDR_1X02"}
          <= {e["ref"] for e in rules["edge"]})
    try:
        constraints.build(ir, mapped["spec"], {**schema.default_rules(), "mystery": 1})
        check("未识别字段被拒绝", False)
    except constraints.ConstraintError as exc:
        check("未识别字段被拒绝", any(i["code"] == "unknown" for i in exc.issues))
    try:
        constraints.build(ir, mapped["spec"], {**schema.default_rules(), "net_classes": [
            {"name": "A", "nets": ["GND"], "track_mm": 0.2, "clearance_mm": 0.2, "priority": 1, "pour": True},
            {"name": "B", "nets": ["GND"], "track_mm": 0.2, "clearance_mm": 0.2, "priority": 1, "pour": False},
        ]})
        check("同一网络分属两个类被拒绝", False)
    except constraints.ConstraintError as exc:
        check("同一网络分属两个类被拒绝", any(i["code"] == "net_class_conflict" for i in exc.issues),
              str(exc.issues)[:240])
    try:
        constraints.build(ir, mapped["spec"], {**schema.default_rules(), "net_classes": [
            {"name": "Tight", "nets": ["in"], "track_mm": 0.05, "clearance_mm": 0.2, "priority": 1, "pour": False},
        ]})
        check("低于制造底线的线宽被拒绝", False)
    except constraints.ConstraintError as exc:
        check("低于制造底线的线宽被拒绝", any(i["code"] == "relax" for i in exc.issues))
    try:
        constraints.build(ir, mapped["spec"], {**schema.default_rules(), "proximity": [
            {"ref": "R1", "parent": "C1", "max_mm": 1.0},
        ]})
        check("近距小于庭院的约束被拒绝", False)
    except constraints.ConstraintError as exc:
        check("近距小于庭院的约束被拒绝", any(i["code"] == "impossible" for i in exc.issues),
              str(exc)[:200])


def test_legalizer() -> None:
    print("合法化：重叠/出板可预测，信号流网格保持不动")
    ir = netlist_to_ir(RC)
    mapped = parts.map_netlist(RC, ["in", "out"])
    rules = constraints.build(ir, mapped["spec"], None)
    before = _xy(mapped["spec"])
    placed = legalizer.legalize(mapped["spec"], rules)
    check("信号流布局已经合法", placed["ok"] and not placed["repairs"], str(placed["errors"])[:300])
    if placed["ok"]:
        check("信号流坐标不被推开", _xy(placed["spec"]) == before, str(_xy(placed["spec"])))
        again = legalizer.legalize(mapped["spec"], rules)
        check("同一输入两次合法化一致", _xy(again["spec"]) == _xy(placed["spec"]))

    spec = {
        "name": "board", "size_mm": [80.0, 50.0], "nets": ["GND", "A"],
        "components": [
            {"ref": "R1", "part": "R_THT_0207", "footprint": "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
             "x": 30.0, "y": 20.0, "rot": 0, "pads": ["A", "GND"]},
            {"ref": "R2", "part": "R_THT_0207", "footprint": "R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
             "x": 30.0, "y": 20.0, "rot": 0, "pads": ["A", "GND"]},
        ],
        "keepouts": [], "zones": [],
    }
    bare = schema.default_rules()
    fixed = legalizer.legalize(spec, bare)
    check("重叠被推开", fixed["ok"] and legalizer._overlap_amount(
        next(c for c in fixed["spec"]["components"] if c["ref"] == "R1"),
        next(c for c in fixed["spec"]["components"] if c["ref"] == "R2")) is None,
          str(fixed["errors"])[:200])
    fixed2 = legalizer.legalize(spec, schema.default_rules())
    check("重叠修复可复现", fixed["ok"] and _xy(fixed["spec"]) == _xy(fixed2["spec"]))

    outside = json.loads(json.dumps(spec))
    outside["components"] = [outside["components"][0]]
    outside["components"][0]["x"] = -20
    clamped = legalizer.legalize(outside, schema.default_rules())
    check("出板的未锁定器件被收回板内",
          clamped["ok"] and clamped["spec"]["components"][0]["x"] > 0, str(clamped["errors"])[:200])
    locked = schema.default_rules()
    locked["locked"] = [{"ref": "R1", "x": -5.0, "y": 20.0, "rot": 0}]
    outside["components"][0]["x"] = -5
    denied = legalizer.legalize(outside, locked)
    check("锁定在板外的器件被拒绝", not denied["ok"] and any(
        e["code"] in {"out_of_board", "locked_moved"} for e in denied["errors"]),
          str(denied["errors"])[:240])


def _mock_tools(spec_holder: dict):
    def fake_build(k, spec, out_pcb, workdir, rules=None):
        spec_holder["rules"] = rules
        spec_holder["spec"] = spec
        out_pcb.parent.mkdir(parents=True, exist_ok=True)
        out_pcb.write_text("board", encoding="utf-8")
        return True, "fixture"

    def fake_fab(k, pcb, out_dir, timeout=120):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "b.gbrjob").write_text("{}", encoding="utf-8")
        (out_dir / "b.drl").write_text("x", encoding="utf-8")
        return True, "fixture"

    k = SimpleNamespace(version=lambda: "mock", python="x", cli="x", footprints=Path("."))
    return k, fake_build, fake_fab


def test_mocked_agent_and_repair() -> None:
    print("模拟模型：提案、回退、一轮修复、不放宽制造规则")
    ir_spec = parts.map_netlist(RC, ["in", "out"])
    assert ir_spec["ok"]
    holder: dict = {}
    k, fake_build, fake_fab = _mock_tools(holder)
    calls = {"route": 0, "propose": 0}
    mode = {"route": "ok"}

    def propose(ctx):
        calls["propose"] += 1
        assert "json" in (ctx["system"] + ctx["user"]).lower()
        return {"tracks": [[1, 2, 3]]}

    def route(*_a, **_k):
        calls["route"] += 1
        if mode["route"] == "flaky" and calls["route"] == 1:
            return _route(False, 3, ["仍有 3 条连接未布通"])
        if mode["route"] == "fail":
            return _route(False, 3, ["仍有 3 条连接未布通"])
        return _route(True, 0, [])

    with tempfile.TemporaryDirectory(prefix="vp_agent_") as td, \
            patch.object(kicad_backend, "find_kicad", return_value=k), \
            patch.object(router_backend, "find_freerouting", return_value=Path("m.jar")), \
            patch.object(router_backend, "java_blocker", return_value=None), \
            patch.object(kicad_backend, "build_board", side_effect=fake_build), \
            patch.object(kicad_backend, "export_dsn", return_value=(True, "DSN")), \
            patch.object(router_backend, "route", side_effect=route), \
            patch.object(kicad_backend, "import_ses", return_value=(True, "TRACKS: 1")), \
            patch.object(kicad_backend, "preview_from_pcb", return_value={"pads": _pads(ir_spec["spec"])}), \
            patch.object(kicad_backend, "drc", return_value={"ok": True, "violations": 0, "unconnected": 0}), \
            patch.object(kicad_backend, "export_fab", side_effect=fake_fab):
        bad = jobs.run_pcb_job(RC, ["in", "out"], Path(td) / "bad",
                               agent_opts={"enabled": True, "propose": propose, "max_repairs": 1})
        record = json.loads((Path(td) / "bad" / "agent" / "placement.json").read_text(encoding="utf-8"))
        check("非法提案回退到信号流且任务仍完成",
              bad["ok"] and record["fallback"] == "proposal_invalid" and record["source"] == "signal_flow"
              and record["provider"]["provider"] == "mock" and record["prompt_sha256"],
              str(bad.get("state")) + str(record.get("fallback")))
        check("回退档案留下原始提案", record["raw_proposal"] == {"tracks": [[1, 2, 3]]})

        calls["propose"] = 0

        def quiet(_ctx):
            raise AssertionError("opt-out 不应调用模型")

        off = jobs.run_pcb_job(RC, ["in", "out"], Path(td) / "off",
                               agent_opts={"enabled": False, "propose": quiet})
        off_rec = json.loads((Path(td) / "off" / "agent" / "placement.json").read_text(encoding="utf-8"))
        check("关闭代理时不调用模型且来源是信号流",
              off["ok"] and off_rec["source"] == "signal_flow" and off_rec["provider"] is None
              and calls["propose"] == 0, str(off.get("state")))

        def repair(_ctx):
            return {"edits": [{"op": "enlarge_board", "w": 120, "h": 80}]}

        calls["route"] = 0
        mode["route"] = "flaky"
        fixed = jobs.run_pcb_job(RC, ["in", "out"], Path(td) / "fix", agent_opts={
            "enabled": True, "max_repairs": 1, "propose": propose, "repair": repair})
        # propose still returns invalid, so placement falls back; the failure is the first route.
        round_dir = Path(td) / "fix" / "agent" / "round-1"
        applied = json.loads((Path(td) / "fix" / "input" / "rules.json").read_text(encoding="utf-8"))
        check("失败后做一轮放大并再次布通", fixed["ok"] and calls["route"] == 2 and round_dir.is_dir(),
              str(fixed.get("state")) + str(calls))
        check("制造底线没有被放宽", applied["track_mm"] == 0.2 and applied["clearance_mm"] == 0.2,
              str(applied.get("track_mm")))
        result_txt = (round_dir / "result.json").read_text(encoding="utf-8")
        check("修复轮档案含提案、校验和不能复用旧走线的说明",
              (round_dir / "proposal.json").is_file() and (round_dir / "validation.json").is_file()
              and (round_dir / "evidence.json").is_file() and "Freerouting" in result_txt)

        def relax(_ctx):
            return {"edits": [{"op": "net_class", "name": "Ground", "nets": ["GND"],
                               "track_mm": 0.05, "clearance_mm": 0.05, "priority": 1, "pour": True}],
                    "clearance_mm": 0.01}

        calls["route"] = 0
        mode["route"] = "fail"
        denied = jobs.run_pcb_job(RC, ["in", "out"], Path(td) / "relax", agent_opts={
            "enabled": True, "max_repairs": 1, "propose": lambda _c: {"placements": []}, "repair": relax})
        denied_rules = json.loads((Path(td) / "relax" / "input" / "rules.json").read_text(encoding="utf-8"))
        denied_val = json.loads((Path(td) / "relax" / "agent" / "round-1" / "validation.json").read_text(encoding="utf-8"))
        check("放宽线宽/净距的修复被拒绝且没有第二次布线",
              not denied["ok"] and calls["route"] == 1 and denied_val["applied"] is False
              and denied_rules["track_mm"] == 0.2 and any(e.get("code") == "relax" for e in denied_val["errors"]),
              str(denied_val["errors"])[:300])


def test_lm358_candidate() -> None:
    print("理想运放 → LM358 候选（新版本，不改已采用电路）")
    blocked = parts.map_netlist("opamp deck\nXU1 in fb out OPAMP\n.end", ["in"])
    check("三脚理想运放仍然阻断", not blocked["ok"] and any(
        "理想运放" in b["reason"] for b in blocked["report"]["blocked"]))
    missing = opamp_realize.realize_netlist("x\nXU1 in fb out OPAMP\n.end\n")
    check("没有子电路时给出可操作阻断", not missing["ok"] and any(
        b["code"] == "unparsed_subckt" for b in missing["blockers"]), str(missing["blockers"])[:200])
    single = "\n".join([
        "single",
        "VCC vcc 0 DC 12",
        "XU1 0 ninv out OPAMP",
        ".subckt OPAMP nn np vout",
        "E1 vout 0 np nn 100k",
        ".ends",
        ".op",
        ".end",
    ])
    one = opamp_realize.realize_netlist(single)
    check("只有正电源时阻断而不是半块板", not one["ok"] and any(
        b["code"] == "single_rail" for b in one["blockers"]), str(one["blockers"])[:240])

    built = opamp_realize.realize_netlist(IDEAL)
    check("替换写出 LM358、正负电源和去耦",
          built["ok"] and "XU1 0 ninv vcc vee out LM358" in built["netlist"]
          and "VCCSRC vcc 0 DC 12" in built["netlist"]
          and "VEESRC vee 0 DC -12" in built["netlist"]
          and "100n" in built["netlist"] and "200k" in built["netlist"],
          built["netlist"][:400] if built["ok"] else str(built["blockers"]))
    if not built["ok"]:
        return
    ir = netlist_to_ir(built["netlist"])
    roles = {el["ref"]: el["role"] for el in ir["elements"]}
    check("LM358 成为可映射器件", roles.get("XU1") == "device", str(roles))
    ports = list(ir["port_candidates"])
    mapped = parts.map_netlist(built["netlist"], ports)
    check("补供电后的候选可以映射", mapped["ok"],
          "; ".join(b["reason"] for b in mapped["report"]["blocked"]))
    if mapped["ok"]:
        part = connectivity.check_partition(ir, mapped["spec"], ports)
        check("引脚对照通过（含电源脚，未用通道不进 SPICE 对照）", part["ok"], str(part["issues"])[:300])
        xu = next(c for c in mapped["spec"]["components"] if c["ref"] == "XU1")
        check("未用通道端接到地和本地位号网络",
              "GND" in xu["pads"] and "XU1_UB" in xu["pads"] and "XU1_UB" in mapped["spec"]["nets"],
              str(xu["pads"]))
        placed = legalizer.legalize(mapped["spec"], constraints.build(ir, mapped["spec"], None))
        check("LM358 布局能被合法化", placed["ok"], str(placed["errors"])[:300])

    with tempfile.TemporaryDirectory(prefix="vp_lm358_") as td:
        ws = Workspace(td)
        ws.ensure()
        ws.save_project({"netlist": IDEAL, "revision": 3, "requirements": []})
        saved = opamp_realize.reverify_candidate(
            ws, built["netlist"], title="lm358", changes=built["changes"],
            simulate=lambda _n: {"ok": True, "verdict": "simulated", "task_id": "sim"})
        check("重验证只新增候选",
              saved["ok"] and saved["applied"] is False and saved["version_id"]
              and ws.load_project().get("netlist") == IDEAL, str(saved)[:240])
        if saved["ok"]:
            stored = project.read_version(ws, saved["version_id"])
            check("候选网表是 LM358 而不是旧的理想运放",
                  "LM358" in stored["netlist"] and stored["source"] == "opamp-realize")

        if shutil.which("ngspice") or os.environ.get("VOLTPROOF_NGSPICE"):
            live = opamp_realize._simulate_real(ws, built["netlist"], [])
            check("本机 ngspice 能重新验证 LM358 候选", live["ok"], str(live.get("blockers"))[:300])
        else:
            print("  [SKIP] 本机没有 ngspice，真实重仿真由模拟路径覆盖")

        old_ws = app_main._active_ws
        app_main._active_ws = ws
        try:
            client = TestClient(app_main.app, raise_server_exceptions=False)
            with patch.object(opamp_realize, "_simulate_real",
                              return_value={"ok": True, "verdict": "simulated", "task_id": "api"}):
                resp = client.post("/api/pcb/realize-opamp", json={"netlist": IDEAL})
            body = resp.json()
            check("接口返回新候选且不改项目网表",
                  resp.status_code == 200 and body.get("ok") and body.get("applied") is False
                  and ws.load_project().get("netlist") == IDEAL, str(body)[:240])
            pre = client.post("/api/pcb/preflight", json={"netlist": IDEAL, "ports": ["in"]}).json()
            check("预检标出可以替换理想运放", pre.get("opamp_realize", {}).get("needed") is True
                  and any("理想运放" in b["reason"] for b in pre["blockers"]))
        finally:
            app_main._active_ws = old_ws


def test_project_rules_keep_floor() -> None:
    print("网络类写入工程文件时不放宽全局最小线宽")
    with tempfile.TemporaryDirectory(prefix="vp_rules_") as td:
        board = Path(td) / "b.kicad_pcb"
        rules = schema.default_rules()
        rules["net_classes"] = [{
            "name": "Power", "nets": ["VCC"], "track_mm": 0.5, "clearance_mm": 0.3,
            "priority": 10, "pour": False,
        }]
        kicad_backend.write_project_rules(board, rules)
        data = json.loads(board.with_suffix(".kicad_pro").read_text(encoding="utf-8"))
        ds = data["board"]["design_settings"]
        names = [c["name"] for c in ds["net_settings"]["classes"]]
        power = next(c for c in ds["net_settings"]["classes"] if c["name"] == "Power")
        check("工程文件含电源类且全局最小线宽仍是 0.2",
              "Power" in names and ds["rules"]["min_track_width"] == 0.2
              and power["track_width"] >= 0.2 and power["clearance"] >= 0.2)
        script = kicad_backend._netclass_apply_source(rules)
        check("DSN 导出脚本带网络类且失败可跳过",
              "NETCLASS_APPLIED" in script and "NETCLASS_SKIPPED" in script and "VCC" in script)


def test_agent_offline_hook() -> None:
    print("无工具钩子：pcb_smoke --agent-offline")
    import subprocess
    repo = Path(__file__).resolve().parent.parent
    proc = subprocess.run([sys.executable, str(repo / "scripts" / "pcb_smoke.py"), "--agent-offline"],
                          cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    check("离线钩子退出码 0", proc.returncode == 0, (proc.stdout + proc.stderr)[-400:])


def main() -> int:
    for fn in (test_constraints_fail_closed, test_legalizer, test_mocked_agent_and_repair,
               test_lm358_candidate, test_project_rules_keep_floor, test_agent_offline_hook):
        print(f"\n== {fn.__name__} ==")
        fn()
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
