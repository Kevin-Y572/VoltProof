"""PCB 任务编排：映射 → 造板 → 布线 → 回导 → 一致性/DRC → 制板导出。

方案 §6.1/§6.2 的任务层（同步内核版）：main.py 的 SSE 端点后续直接包
on_stage 回调即可流式转发；取消为【阶段间协作式】——cancel_check 只在
阶段边界生效，布线子进程由看门狗超时兜底（与主程序 chat 取消同语义，
方案 §6.2 原文即此设计）。

归档结构（§6.1，按本阶段简化）：
  <archive>/input/   netlist.txt、ports.json、rules.json
          /routing/  dsn、ses、freerouting.log
          /final/    board.kicad_pcb
          /checks/   connectivity.json、drc.json、mapping_report.json
          /fabrication/  Gerber + 钻孔
          /manifest.json  阶段日志、哈希、版本记录
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from ..circuit_ir import netlist_to_ir
from . import connectivity, kicad_backend, parts, router_backend, schema, versions

_STAGE_NAMES = ("mapping", "board", "routing", "import", "check", "fabrication")


def _bom_csv(mapped: dict) -> str:
    """BOM（方案 §7）：位号/数值/封装/数量/型号，同型号同数值合并一行。
    端子连接器按位号单列（数量汇总）。未提供采购料号的行明确标注。"""
    groups: dict[tuple, list[str]] = {}
    for m in mapped["report"]["mapped"]:
        key = (m.get("value", ""), m["footprint"], m["part_name"])
        groups.setdefault(key, []).append(m["ref"])
    for p in mapped["report"].get("ports", []):
        key = ("外部端子", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical", "排针 1x2")
        groups.setdefault(key, []).append(p["ref"])
    lines = ["位号,数值,封装,数量,型号,备注"]
    for (value, fp, name), refs in sorted(groups.items(), key=lambda kv: kv[1][0]):
        lines.append(f"\"{','.join(sorted(refs))}\",\"{value}\",\"{fp}\","
                     f"{len(refs)},\"{name}\",\"未提供采购料号——按型号选购\"")
    return "\n".join(lines) + "\n"


class Cancelled(Exception):
    """cancel_check 返回真（阶段边界触发）。"""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def run_pcb_job(netlist: str, ports: list[str], archive: Path, *,
                options: dict | None = None,
                ws=None, rules: dict | None = None,
                max_passes: int = 10, timeout_s: int = 180,
                cancel_check=None, on_stage=None, job_id: str | None = None,
                base_board: Path | None = None, parent_version: str | None = None,
                moves: dict | None = None) -> dict:
    """跑一次 PCB 任务。返回结果 dict（同时写 manifest.json 归档）。

    ws 提供时：检查通过 → 保存候选版本记录（不自动采用——采用是用户动作）。
    工具缺失/映射阻断 → FAILED（带原因），不抛异常。
    job_id 可由调用方指定（须与 archive 目录名一致——API 层用同一 id 轮询
    与导出；缺省自动生成）。
    base_board：布局调整路径——从既有板文件继续（跳过造板，先移动封装
    再布线）；netlist/ports 仍须提供（一致性对照与版本哈希用）。
    """
    archive = Path(archive)
    job_id = job_id or schema.new_job_id()
    t0 = time.monotonic()
    stages: list[dict] = []
    result = {"job_id": job_id, "ok": False, "state": schema.FAILED,
              "stages": stages, "blockers": [], "version_id": None}

    def stage(name: str, state_on_ok: str):
        """阶段装饰：登记、取消检查、计时。函数体抛 Cancelled/Exception 统一归档。"""
        def wrap(fn):
            def run(*a, **kw):
                if cancel_check and cancel_check():
                    result["state"] = schema.CANCELLED
                    raise Cancelled(f"在 {name} 阶段前取消")
                entry = {"stage": name, "ok": False, "elapsed": round(time.monotonic() - t0, 1)}
                stages.append(entry)
                if on_stage:
                    on_stage(name, "start", None)
                try:
                    detail = fn(*a, **kw)
                    entry.update(ok=True, detail=str(detail)[:500])
                    result["state"] = state_on_ok
                    if on_stage:
                        on_stage(name, "done", detail)
                    return detail
                except Cancelled as e:
                    entry.update(detail=str(e))
                    raise
                except Exception as e:  # noqa: BLE001 —— 任务层汇总一切失败
                    entry.update(detail=f"{type(e).__name__}: {e}"[:500])
                    result["state"] = schema.FAILED
                    if on_stage:
                        on_stage(name, "error", str(e))
                    raise
            return run
        return wrap

    (archive / "input").mkdir(parents=True, exist_ok=True)
    (archive / "input" / "netlist.txt").write_text(netlist, encoding="utf-8")
    (archive / "input" / "ports.json").write_text(
        json.dumps(ports, ensure_ascii=False), encoding="utf-8")
    rules = rules or schema.default_rules()
    (archive / "input" / "rules.json").write_text(
        json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")

    # 1. 映射（纯离线，先跑——网表自身的阻断问题优先暴露给用户）
    mapped: dict = {}
    ir: dict = {}

    @stage("mapping", schema.READY)
    def s_mapping():
        nonlocal mapped, ir
        ir = netlist_to_ir(netlist)
        mapped = parts.map_netlist(netlist, ports, options=options)
        (archive / "checks").mkdir(exist_ok=True)
        (archive / "checks" / "mapping_report.json").write_text(
            json.dumps(mapped["report"], ensure_ascii=False, indent=2), encoding="utf-8")
        if not mapped["ok"]:
            result["blockers"] = [b["reason"] for b in mapped["report"]["blocked"]]
            raise ValueError("映射存在阻断项：" + "；".join(result["blockers"][:3]))
        return f"{len(mapped['spec']['components'])} 个元件映射成功"

    try:
        s_mapping()
    except (Cancelled, Exception):  # noqa: BLE001 —— 明细已入 stages
        _finish(archive, result, netlist, mapped, rules, None, None, t0, ws, None, None)
        return result

    # 2. 工具在场性（缺失时给明确诊断，方案 §5.2 最后一问）
    kicad = kicad_backend.find_kicad()
    jar = router_backend.find_freerouting()
    if kicad is None or jar is None:
        missing = []
        if kicad is None:
            missing.append("KiCad 未找到（设 VOLTPROOF_KICAD 或安装 KiCad 10.x）")
        if jar is None:
            missing.append("Freerouting JAR 未找到（设 VOLTPROOF_FREEROUTING）")
        result["blockers"] = missing
        result["state"] = schema.FAILED
        result["stages"].append({"stage": "tools", "ok": False,
                                 "elapsed": round(time.monotonic() - t0, 1),
                                 "detail": "；".join(missing)})
        _finish(archive, result, netlist, mapped, rules, None, None, t0, ws, None, None)
        return result

    pcb = archive / "final" / "board.kicad_pcb"
    dsn = archive / "routing" / "board.dsn"
    ses = archive / "routing" / "board.ses"
    route_result = None

    @stage("board", schema.READY)
    def s_board():
        (archive / "final").mkdir(exist_ok=True)
        if base_board is not None:
            import shutil as _sh
            _sh.copyfile(base_board, pcb)
            base_pro = base_board.with_suffix(".kicad_pro")
            if base_pro.is_file():  # 制造规则随板走（F05：DRC 判据在伴生工程）
                _sh.copyfile(base_pro, pcb.with_suffix(".kicad_pro"))
            if moves:
                # 布局调整：先清掉旧走线（重布），再移动封装
                strip = ("import pcbnew\n"
                         f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
                         "n = 0\n"
                         "for t in list(b.GetTracks()):\n"
                         "    if t.IsLocked(): continue  # 用户锁定走线保留\n"
                         "    b.Remove(t); n += 1\n"
                         "print('STRIPPED:', n)\n"
                         f"print('STRIP:', pcbnew.SaveBoard({str(pcb)!r}, b))\n")
                (archive / "final" / "_strip.py").write_text(strip, encoding="utf-8")
                pr = subprocess.run([str(kicad.python), str(archive / "final" / "_strip.py")],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
                assert "STRIP: True" in pr.stdout, pr.stdout[-300:] + pr.stderr[-200:]
                ok, out = kicad_backend.move_footprints(kicad, pcb, moves, archive / "final")
                assert ok, out[:300]
            return f"沿用板 {pcb.name}（布局调整基板，{len(moves or {})} 处移动）"
        ok, out = kicad_backend.build_board(kicad, mapped["spec"], pcb, archive / "final",
                                            rules=rules)
        assert ok, out
        return f"board.kicad_pcb {_sha256(pcb)}"

    @stage("routing", schema.ROUTED)
    def s_routing():
        nonlocal route_result
        (archive / "routing").mkdir(exist_ok=True)
        ok, out = kicad_backend.export_dsn(kicad, pcb, dsn, archive / "routing")
        assert ok, out
        rr = router_backend.route(jar, dsn, ses, archive / "routing" / "router",
                                  max_passes=max_passes, timeout_s=timeout_s)
        route_result = rr
        if not rr.ok:
            detail = "; ".join(rr.problems) or rr.summary
            if rr.timed_out and rr.ses_written:
                result["state_hint_partial"] = True
            raise ValueError(f"布线未通过：{detail}")
        return rr.summary

    @stage("import", schema.ROUTED)
    def s_import():
        ok, out = kicad_backend.import_ses(kicad, pcb, ses, archive / "final",
                                           fill_zones=True)
        assert ok, out
        return next((ln for ln in out.splitlines() if ln.startswith("TRACKS:")), "?")

    check_result: dict = {}
    drc_result: dict = {}

    @stage("check", schema.CHECKED)
    def s_check():
        nonlocal drc_result, check_result
        part = connectivity.check_partition(ir, mapped["spec"], ports)
        assert part["ok"], "网络分区对照失败：" + "；".join(part["issues"][:3])
        # 板级对照（验收 F01）：从【实际板文件】读回焊盘网络做引脚级比对——
        # 板规格正确 + DRC 零违规都不证明板与源电路一致，板文件才是证据
        preview = kicad_backend.preview_from_pcb(pcb)
        assert preview is not None, "无法解析回导后的板文件（板级一致性对照缺失）"
        board_part = connectivity.board_partition_with_map(preview, ir, ports,
                                                            mapped["spec"])
        assert board_part["ok"], "板文件与源电路不一致：" + \
            "；".join(board_part["issues"][:3])
        drc = kicad_backend.drc(kicad, pcb, archive / "checks" / "drc.json")
        drc_result = drc
        assert drc["ok"] and drc["violations"] == 0 and drc["unconnected"] == 0, \
            f"DRC 未通过：错误 {drc['violations']} / 未连 {drc['unconnected']}"
        copper = connectivity.copper_verdict(route_result, drc)
        assert copper["ok"], "铜连接判定未通过：" + "；".join(copper["reasons"])
        check_result = {"partition": part, "board_partition": board_part,
                        "copper": copper,
                        "warnings": drc.get("warning_items", [])}
        (archive / "checks" / "connectivity.json").write_text(
            json.dumps(check_result, ensure_ascii=False, indent=2), encoding="utf-8")
        return (f"分区+板级对照通过；DRC 0 错误 0 未连"
                + (f"（{drc.get('violations_warning', 0)} 条警告已保留）"
                   if drc.get("violations_warning") else ""))

    @stage("fabrication", schema.CHECKED)
    def s_fab():
        fab = archive / "fabrication"
        ok, out = kicad_backend.export_fab(kicad, pcb, fab)
        assert ok, out[-300:]
        (fab / "BOM.csv").write_text(_bom_csv(mapped), encoding="utf-8-sig")
        files = sorted(p.name for p in fab.iterdir())
        assert any(f.endswith(".gbrjob") for f in files) and any(f.endswith(".drl") for f in files)
        return f"{len(files)} 个制板文件"

    try:
        s_board()  # mapping 已在上方工具预检前跑过（阻断/取消时已提前 return）
        s_routing()
        s_import()
        s_check()
        s_fab()
        result["ok"] = True
    except Cancelled:
        pass
    except Exception:  # noqa: BLE001 —— 阶段装饰器已把明细写进 stages
        pass
    if result.get("state_hint_partial") and result["state"] == schema.FAILED:
        result["state"] = schema.PARTIAL
    _finish(archive, result, netlist, mapped, rules, pcb, drc_result, t0, ws, kicad, jar,
            parent_version)
    return result


def _finish(archive: Path, result: dict, netlist: str, mapped: dict, rules: dict,
            pcb: Path | None, drc_result, t0: float, ws, kicad, jar,
            parent_version: str | None = None) -> None:
    result["elapsed_s"] = round(time.monotonic() - t0, 1)
    if mapped.get("ok") and result["state"] in (schema.CHECKED,):
        tool_versions = {"kicad": kicad.version() if kicad else "?",
                         "freerouting": jar.name if jar else "?"}
        source_version_id = None
        if ws is not None:  # 源电路修订号绑定（验收 F06：采用时双重校验用）
            try:
                from .. import project as _project
                source_version_id = _project.revision(ws.load_project())
            except Exception:  # noqa: BLE001 —— 绑定失败不阻断产候选，仅缺字段
                source_version_id = None
        version = schema.new_version(netlist=netlist, report=mapped["report"],
                                     rules=rules, tool_versions=tool_versions,
                                     job_id=result["job_id"],
                                     source_version_id=source_version_id)
        if parent_version:
            version["parent_version_id"] = parent_version
            result["parent_version_id"] = parent_version
        if pcb and pcb.is_file():
            version["board_hash"] = _sha256(pcb)
        version["status"] = schema.CHECKED
        result["version_id"] = version["pcb_version_id"]
        result["version"] = version
        if ws is not None:
            versions.save_candidate(ws, version)
    manifest = {k: v for k, v in result.items() if k != "version"}
    if result.get("version"):
        manifest["version"] = result["version"]
    (archive / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
