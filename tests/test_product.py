"""产品业务回归：真实 ngspice + 固定 LLM，不使用网络或真实项目。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("VOLTPROOF_SETTINGS", str(Path(tempfile.gettempdir()) / "voltproof-product-unused-settings.json"))
from app import cache, draw_api, llm, measure, pipeline, project, prompts, specs, whatif
from app.workspace import Workspace

RC = """* RC 1kHz
V1 in 0 AC 1
R1 in out 1.59k
C1 out 0 100n
.control
set filetype=ascii
ac dec 100 10 100k
write out.raw v(out) v(in)
.endc
.end
"""
DIV = """* divider
V1 in 0 DC 10
R1 in out 1k
R2 out 0 1k
.control
set filetype=ascii
op
write out.raw v(out)
.endc
.end
"""


class ProductTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="vp-product-")
        self.ws = Workspace(Path(self.tmp.name))
        self.ws.ensure()
        self.cache_patch = patch.object(cache, "_ENABLED", False)
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def llm_for(netlist=RC):
        def call(system, user, *a, **kw):
            return "固定解读，无网络调用" if system == prompts.INTERPRET_SYSTEM else netlist
        return call

    def seed(self):
        result = draw_api.verify_drawn(DIV, "分压", self.ws)
        self.assertTrue(result["ok"], result)
        return self.ws.load_project()

    def test_wrong_target_cannot_pass_normal_chat_pipeline(self):
        with patch.object(llm, "chat", side_effect=self.llm_for()):
            ev = pipeline.run_pipeline("截止频率10kHz的RC低通，误差不超过5%", max_retries=0, workspace=self.ws)
        self.assertFalse(ev.ok)
        self.assertEqual(ev.verdict, "failed")
        self.assertTrue(900 < ev.checks[0]["measured"] < 1200)
        self.assertEqual(ev.checks[0]["target"], 10000)

    def test_target_failure_repairs_and_keeps_every_round(self):
        calls = []
        def fake(system, user, *a, **kw):
            calls.append((system, user))
            if system == prompts.TUNE_SYSTEM:
                return RC.replace("1.59k", "159")
            return self.llm_for()(system, user)
        with patch.object(llm, "chat", side_effect=fake):
            ev = pipeline.run_pipeline("截止频率10kHz的RC低通，误差5%", workspace=self.ws)
        self.assertTrue(ev.ok, ev.retry_log)
        self.assertEqual(ev.verdict, "passed")
        self.assertTrue(any("目标" in user for system, user in calls if system == prompts.TUNE_SYSTEM))
        for rnd in (1, 2):
            self.assertTrue((self.ws.sim_dir / ev.task_id / f"round-{rnd}" / "circuit.cir").is_file())
            self.assertTrue((self.ws.sim_dir / ev.task_id / f"round-{rnd}" / "checks.json").is_file())

    def test_parse_failure_never_passes_or_adopts(self):
        with patch.object(measure, "load_traces", side_effect=ValueError("broken raw")), patch.object(llm, "chat", side_effect=self.llm_for()):
            ev = pipeline.run_pipeline("验证", initial_netlist=RC, workspace=self.ws)
            drawing = draw_api.verify_drawn(DIV, "失败", self.ws)
        self.assertFalse(ev.ok)
        self.assertEqual(ev.verdict, "incomplete")
        self.assertEqual(ev.metrics, {})
        self.assertFalse(drawing["ok"])
        self.assertEqual(drawing["verdict"], "incomplete")
        self.assertFalse(self.ws.load_project().get("netlist"))

    def test_preview_does_not_commit_and_undo_restores_all(self):
        before = self.seed()
        with patch.object(llm, "chat", side_effect=self.llm_for()):
            result = list(draw_api.ai_draw_events("画RC低通", None, self.ws))[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(self.ws.load_project(), before)
        after = project.apply(self.ws, result["candidate_id"], result["base_revision"])
        self.assertIn("C1", after["netlist"])
        project.update_draft(self.ws, analysis={"type":"ac", "fstart":"100", "fstop":"1Meg"}, expected=after["revision"])
        after = self.ws.load_project()
        restored = project.restore(self.ws, before["version_id"], after["revision"])
        for key in ("netlist", "canvas", "analysis", "requirements", "evidence_id"):
            self.assertEqual(restored.get(key), before.get(key), key)

    def test_stale_candidate_cannot_overwrite_manual_edit(self):
        self.seed()
        with patch.object(llm, "chat", side_effect=self.llm_for()):
            result = list(draw_api.ai_draw_events("画RC低通", None, self.ws))[-1]
        project.update_draft(self.ws, canvas={"comps": [], "wires": [], "labels": []}, draft_netlist="", expected=result["base_revision"])
        with self.assertRaises(project.Conflict):
            project.apply(self.ws, result["candidate_id"], project.revision(self.ws.load_project()))

    def test_undo_preserves_pre_application_requirements(self):
        before = self.seed()
        pending = specs.normalize([{"metric": "mean", "target": 6}])
        restored = project.restore(self.ws, before["version_id"], before["revision"], pending)
        self.assertEqual(restored["requirements"], pending)
        self.assertTrue(restored["requirements_pending"])
        self.assertTrue(restored["dirty"])
        self.assertEqual(restored["draft_netlist"], before["netlist"])
        empty = project.restore(self.ws, "", restored["revision"], pending)
        self.assertEqual(empty["requirements"], pending)
        self.assertTrue(empty["requirements_pending"])
        self.assertFalse(empty["netlist"])
        cleared = project.restore(self.ws, "", empty["revision"], [])
        self.assertEqual(cleared["requirements"], [])
        self.assertFalse(cleared["requirements_pending"])

    def test_attachment_modify_executes_requested_change(self):
        with patch.object(llm, "chat", side_effect=self.llm_for(DIV.replace("R2 out 0 1k", "R2 out 0 3k"))) as mock:
            result = pipeline.chat_with_conversation(None, "把附件的R2改成3k", attachment={"filename":"divider.cir", "content":DIV}, workspace=self.ws)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "modify")
        self.assertIn("R2 out 0 3k", result["netlist"])
        self.assertAlmostEqual(result["metrics"]["v(out)_mean"], 7.5)
        self.assertIn(DIV, mock.call_args_list[0].args[1])

    def test_verify_only_does_not_repair(self):
        with patch.object(llm, "chat", side_effect=AssertionError("不应调用模型")):
            result = pipeline.chat_with_conversation(None, "只验证，输出电压为6V", attachment={"filename":"divider.cir", "content":DIV}, workspace=self.ws)
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "verify")
        self.assertEqual(result["netlist"], DIV)

    def test_explain_never_generates_or_changes_circuit(self):
        before = self.seed()
        with patch.object(llm, "chat", return_value="解释已有证据") as mock, patch.object(pipeline, "run_netlist", side_effect=AssertionError("不应重新仿真")):
            result = pipeline.chat_with_conversation(None, "解释这个电路为什么输出5V", workspace=self.ws)
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(result["verdict"], "explained")
        self.assertEqual(self.ws.load_project(), before)
        self.assertEqual(result["metrics"]["v(out)_mean"], 5)

    def test_inherited_requirements_survive_followup(self):
        with patch.object(llm, "chat", side_effect=self.llm_for()):
            first = pipeline.chat_with_conversation("thread", "设计截止频率1kHz的RC低通", workspace=self.ws)
            second = pipeline.chat_with_conversation("thread", "把C1改为100n，保持其余要求", workspace=self.ws)
        self.assertEqual(first["verdict"], "passed")
        self.assertEqual(second["requirements"][0]["target"], 1000)
        self.assertEqual(second["verdict"], "passed")

    def test_unsupported_quantified_goal_remains_unverified(self):
        with patch.object(llm, "chat", side_effect=self.llm_for()):
            ev = pipeline.run_pipeline("RC低通，功耗小于1W", workspace=self.ws)
        self.assertFalse(ev.ok)
        self.assertEqual(ev.verdict, "incomplete")
        self.assertIsNone(ev.checks[0]["ok"])

    def test_ambiguous_output_does_not_pick_first_signal(self):
        import numpy as np
        tr = measure.Traces(None, {"v(out1)":np.array([1.]), "v(out2)":np.array([2.])}, analysis="op")
        checks = specs.evaluate(tr, specs.normalize([{"metric":"mean","target":1}]))
        self.assertIsNone(checks[0]["ok"])

    def test_ac_phase_retained_and_frequency_not_fft_of_sweep(self):
        from app.ngspice_runner import run_netlist
        sim = run_netlist(RC, self.ws.sim_dir / "ac")
        tr = measure.load_traces(sim.raw_path)
        self.assertTrue(tr.complex_signals)
        self.assertFalse(any(k.endswith("freq_Hz") for k in measure.extract_metrics(tr)))

    def test_weighted_mean_handles_adaptive_time_steps(self):
        import numpy as np
        t=np.array([0,.01,.1,.4,1.])
        tr=measure.Traces(t,{"v(out)":t},analysis="tran")
        self.assertAlmostEqual(measure.extract_metrics(tr)["v(out)_mean"], .6)
        value, error=measure.compute_metric(tr,"mean")
        self.assertFalse(error)
        self.assertAlmostEqual(value,.6)

    def test_cancellation_does_not_adopt_generated_netlist(self):
        before=self.seed()
        cancelled=threading.Event()
        def fake(*args,**kw):
            cancelled.set()
            return RC
        with patch.object(llm,"chat",side_effect=fake):
            result=pipeline.chat_with_conversation(None,"设计RC低通",workspace=self.ws,cancelled=cancelled.is_set)
        self.assertEqual(result["verdict"],"cancelled")
        self.assertFalse(result["applied"])
        self.assertEqual(self.ws.load_project(),before)

    def test_canvas_ids_do_not_invalidate_verified_circuit(self):
        import copy
        p=self.seed()
        canvas=copy.deepcopy(p["canvas"])
        for i,c in enumerate(canvas["comps"]): c["id"]=i+100
        after=project.update_draft(self.ws,canvas=canvas,expected=p["revision"])
        self.assertFalse(after["dirty"])
        self.assertEqual(after["revision"],p["revision"])

    def test_api_dirty_scan_and_invalid_spec(self):
        from fastapi.testclient import TestClient
        from app import main
        self.seed()
        with patch.object(main,"_active_ws",self.ws), TestClient(main.app) as client:
            p=client.get("/api/project").json()
            result=client.put("/api/project/requirements",json={"requirements":[{"metric":"mean","target":5}],"expected_revision":p["revision"]})
            self.assertEqual(result.status_code,200)
            self.assertEqual(client.get("/api/whatif/components").status_code,409)
            result=client.post("/chat",json={"message":"设计电路","requirements":[{"metric":"gain","target":1,"tol":5}]})
            self.assertEqual(result.status_code,400)

    def test_dc_source_scan_and_per_point_archives(self):
        self.assertIn("V1",[c["name"] for c in whatif.scan_components(DIV)])
        result=whatif.run_scan(DIV,"V1",["8","10"],"mean",workdir=self.ws.sim_dir/"scan",
                               requirements=specs.normalize([{"metric":"mean","target":5}]))
        self.assertFalse(result["points"][0]["passed"])
        self.assertTrue(result["points"][1]["passed"])
        for i in (1,2): self.assertTrue((self.ws.sim_dir/"scan"/f"point-{i}"/"out.raw").exists())

    def test_literal_component_request_is_actually_verified(self):
        with patch.object(llm,"chat",side_effect=self.llm_for(DIV)):
            ev=pipeline.run_pipeline("把R2改成3k",workspace=self.ws,max_retries=0)
        self.assertFalse(ev.ok)
        self.assertEqual(ev.checks[0]["target"],3000)
        self.assertEqual(ev.checks[0]["measured"],1000)

    def test_cutoff_testbench_cannot_be_changed_by_generator(self):
        nl=RC.replace("1.59k","159").replace("ac dec 100 10 100k","ac dec 1 1e9 1.01e9")
        with patch.object(llm,"chat",side_effect=self.llm_for(nl)):
            ev=pipeline.run_pipeline("截止频率10kHz",workspace=self.ws,max_retries=0)
        self.assertTrue(ev.ok,ev.retry_log)
        self.assertEqual(ev.provenance["verification_plan"]["start_hz"],100)
        self.assertEqual(ev.provenance["verification_plan"]["stop_hz"],1000000)
        self.assertEqual(ev.provenance["netlist_hash"],ev.provenance["simulated_netlist_hash"])
        self.assertTrue((self.ws.sim_dir/ev.task_id/"round-1"/"source.cir").is_file())
        data=json.loads((self.ws.docs_dir/ev.task_id/"wave.json").read_text(encoding="utf-8"))
        self.assertTrue(data["phase"])
        self.assertLessEqual(len(data["x"]),1600)

    def test_configured_goals_apply_to_first_ai_design(self):
        from fastapi.testclient import TestClient
        from app import main
        with patch.object(main,"_active_ws",self.ws), TestClient(main.app) as client:
            r=client.put("/api/project/requirements",json={"requirements":[{"metric":"fc","target":10000}],"expected_revision":0})
            self.assertEqual(r.status_code,200)
        def fake(system,user,*a,**kw):
            return RC.replace("1.59k","159") if system==prompts.TUNE_SYSTEM else self.llm_for()(system,user)
        with patch.object(llm,"chat",side_effect=fake):
            result=pipeline.chat_with_conversation(None,"设计一个RC低通",workspace=self.ws)
        self.assertEqual(result["verdict"],"passed")
        self.assertEqual(result["checks"][0]["target"],10000)

    def test_compare_does_not_modify_and_uses_bound_evidence(self):
        with patch.object(llm,"chat",side_effect=self.llm_for()):
            pipeline.chat_with_conversation("compare-thread","设计截止频率1kHz的RC低通",workspace=self.ws)
        with patch.object(llm,"chat",side_effect=self.llm_for(RC.replace("1.59k","159"))):
            pipeline.chat_with_conversation("compare-thread","截止频率改成10kHz",workspace=self.ws)
        before=self.ws.load_project()
        with patch.object(llm,"chat",side_effect=AssertionError("比较不应生成电路")):
            result=pipeline.chat_with_conversation("compare-thread","和上一个版本比较",workspace=self.ws)
        self.assertEqual(result["action"],"compare")
        self.assertEqual(self.ws.load_project(),before)
        row=result["comparison"]["measurements"][0]
        self.assertGreater(row["right"],row["left"]*9)

    def test_scan_candidates_apply_and_appear_in_project_report(self):
        from fastapi.testclient import TestClient
        from app import main
        self.seed()
        with patch.object(main,"_active_ws",self.ws), TestClient(main.app) as client:
            p=client.get("/api/project").json()
            scan=client.post("/api/whatif",json={"component":"V1","values":["8","10"],"metric":"mean","expected_revision":p["revision"]}).json()
            self.assertIn("candidate_id",scan["points"][0])
            applied=client.post("/api/project/apply",json={"version_id":scan["points"][0]["candidate_id"],"expected_revision":scan["base_revision"]})
            self.assertEqual(applied.status_code,200)
            report=client.get("/api/project/report")
            self.assertEqual(report.status_code,200)
            self.assertIn("参数扫描",report.text)
            self.assertIn("项目实验报告",report.text)
            self.assertIn(self.ws.load_project()["version_id"],report.text)

    def test_clean_canvas_resimulation_uses_exact_adopted_netlist(self):
        from fastapi.testclient import TestClient
        from app import main
        p=self.seed()
        with patch.object(main,"_active_ws",self.ws), TestClient(main.app) as client:
            r=client.post("/api/draw/verify",json={"netlist":DIV.replace("DC 10","DC 20"),
                "canvas":p["canvas"],"use_current_version":True,"expected_revision":p["revision"]})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()["metrics"]["v(out)_mean"],5)
        self.assertEqual(self.ws.load_project()["netlist"],DIV)

    def test_filter_type_ignores_negation_and_contrast(self):
        cases = {
            "截止频率1kHz，电路不要做成高通": "lowpass",
            "截止频率1kHz的低通，并对比高通方案": "lowpass",
            "设计高通滤波器，截止频率1kHz": "highpass",
            "做一个1kHz高通": "highpass",
            "做一个1kHz低通": "lowpass",
        }
        for text, expected in cases.items():
            found = [s for s in specs.extract(text) if s["metric"] == "fc"]
            self.assertEqual(len(found), 1, text)
            self.assertEqual(found[0]["filter_type"], expected, text)
            self.assertEqual(found[0]["target"], 1000, text)

    def test_signed_highpass_gain_uses_passband(self):
        import numpy as np
        f = np.logspace(1, 5, 40)
        fc = 1000.0
        high = -2 * (1j * f / fc) / (1 + 1j * f / fc)
        tr = measure.Traces(time=f, signals={"v(out)": np.abs(high), "v(in)": np.ones_like(f)},
                            analysis="ac", complex_signals={"v(out)": high, "v(in)": np.ones_like(f) + 0j})
        signed, err = measure.compute_metric(tr, "gain", filter_type="highpass", signed=True)
        unsigned, _ = measure.compute_metric(tr, "gain", filter_type="highpass", signed=False)
        self.assertEqual(err, "")
        self.assertAlmostEqual(unsigned, 2, delta=0.05)
        self.assertAlmostEqual(signed, -2, delta=0.05)
        low = -2 / (1 + 1j * f / fc)
        tr_lp = measure.Traces(time=f, signals={"v(out)": np.abs(low), "v(in)": np.ones_like(f)},
                               analysis="ac", complex_signals={"v(out)": low, "v(in)": np.ones_like(f) + 0j})
        signed_lp, err_lp = measure.compute_metric(tr_lp, "gain", filter_type="lowpass", signed=True)
        self.assertEqual(err_lp, "")
        self.assertAlmostEqual(signed_lp, -2, delta=0.05)

    def test_nonliteral_component_stays_unverified(self):
        spec = {"metric": "component_value", "component": "R1", "target": 0, "tol": 0,
                "name": "R1 参数", "unit": "Ω"}
        missing = specs.evaluate(None, specs.normalize([spec]), "R1 in out {1k}\n")
        self.assertIsNone(missing[0]["ok"])
        self.assertEqual(missing[0]["status"], "unverified")
        self.assertIsNone(missing[0]["measured"])
        literal_zero = specs.evaluate(None, specs.normalize([spec]), "R1 in out 0\n")
        self.assertTrue(literal_zero[0]["ok"])
        self.assertEqual(literal_zero[0]["measured"], 0)
        literal = specs.evaluate(None, specs.normalize([{**spec, "target": 1000}]), "R1 in out 1k\n")
        self.assertTrue(literal[0]["ok"])
        self.assertEqual(literal[0]["measured"], 1000)

    def test_requirement_editor_round_trip(self):
        import subprocess
        proof = Path(__file__).resolve().parent.parent / "app" / "static" / "proof.js"
        script = r"""
const fs = require("fs");
const vm = require("vm");
const context = { console, document: { addEventListener() {} } };
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
const fn = context.VPProof.requirementFromEditor;
const component = fn({
  metric: "component_value", op: "near", target: 1000, tol: 0,
  out: "out", in: "in", filter_type: "lowpass",
  component: "R1", unit: "Ω", name: "R1 参数",
});
if (component.metric !== "component_value" || component.component !== "R1"
    || component.target !== 1000 || component.unit !== "Ω" || component.name !== "R1 参数") {
  throw new Error("component round-trip " + JSON.stringify(component));
}
const preserved = fn({ preserve: { metric: "带宽", target: 0, name: "带宽", unit: "" } });
if (preserved.metric !== "带宽") throw new Error("unknown metric became " + preserved.metric);
let rejected = false;
try { fn({ metric: "", target: 1000, tol: 0.05, op: "near" }); }
catch (e) { rejected = /指标/.test(e.message); }
if (!rejected) throw new Error("blank metric was accepted");
rejected = false;
try { fn({ metric: "not-a-metric", target: 1, tol: 0.05, op: "near" }); }
catch (e) { rejected = /指标/.test(e.message); }
if (!rejected) throw new Error("unknown metric was accepted as fc");
"""
        proc = subprocess.run(["node", "-e", script, str(proof)], text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

    def test_compare_demo_does_not_replace_project(self):
        from fastapi.testclient import TestClient
        from app import main
        self.seed()
        before = self.ws.load_project()
        net_before = before["netlist"]
        revision = project.revision(before)
        versions = set(p.name for p in (self.ws.root / ".cp" / "versions").glob("*.json"))
        with patch.object(main, "_active_ws", self.ws), patch.object(llm, "chat", side_effect=self.llm_for(RC)), TestClient(main.app) as client:
            page = client.get("/compare.html")
            self.assertEqual(page.status_code, 200)
            self.assertIn("/demo/verified", page.text)
            self.assertNotIn("fetch('/chat'", page.text)
            result = client.post("/demo/verified", json={"message": "设计截止频率1kHz的低通"})
        self.assertEqual(result.status_code, 200, result.text[:500])
        body = result.json()
        self.assertTrue(body.get("ok"), body.get("retry_log") or body)
        self.assertFalse(body.get("applied"))
        self.assertTrue(body.get("demo"))
        self.assertIn("没有替换当前项目电路", body.get("note", ""))
        after = self.ws.load_project()
        self.assertEqual(after.get("netlist"), net_before)
        self.assertEqual(project.revision(after), revision)
        self.assertEqual(set(p.name for p in (self.ws.root / ".cp" / "versions").glob("*.json")), versions)
        self.assertEqual(list(self.ws.convs_dir.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
