"""PCB HTTP 端点（方案 §6.2 接口表，P1 接线）。

设计与 main.py 的 chat/runs 同风格：本地信任、全局活动工作区、
_runs 注册表做取消。任务跑在线程池（run_pcb_job 是同步阻塞内核，
FastAPI 的 def 端点自动进线程池，不阻塞事件循环）；进度走轮询
jobs/{id}（SSE 接线留待前端页签时一并做——on_stage 回调已就绪）。

语义要点（§6.2）：
- 所有改状态的请求带 expected_revision（采用/恢复时校验）；
- 迟到任务只能留候选：任务结果不直接改 current 指针；
- 工具缺失不报 5xx——capabilities 如实回答，preflight/jobs 给 400 带
  修复指引（PCB 是可选能力）。
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from ..circuit_ir import netlist_to_ir
from ..project import Conflict
from . import connectivity, jobs, kicad_backend, parts, router_backend, schema, versions

router = APIRouter(prefix="/api/pcb")


def _project_netlist(req: dict) -> str | JSONResponse:
    """netlist 直给或 use_project=True 从项目当前已验证网表取。
    PCB 只吃验证过的电路：画布有未验证修改（dirty）时明确拒绝。"""
    if req.get("use_project"):
        from .. import project as _project
        ws = _active_ws()
        p = ws.load_project()
        try:
            # verified=True：画布有未验证修改（dirty）时拒绝——PCB 只吃验证过的电路
            netlist = _project.active_netlist(p, verified=True)
        except ValueError as e:  # Conflict extends ValueError（含"先仿真"指引）
            return JSONResponse(status_code=400, content={"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": str(e)})
        if not netlist.strip():
            return JSONResponse(status_code=400,
                                content={"error": "项目里还没有已验证的电路——先在画布或对话里完成一次仿真"})
        return netlist
    return str(req.get("netlist") or "")[:200_000]

_jobs: dict[str, dict] = {}            # job_id -> {result|None, cancel_event}
_jobs_lock = threading.Lock()
_keep = 64                             # 最近完成的任务数（内存上限）


def _active_ws():
    # main 的全局活动工作区（导入循环由 main 挂载顺序保证：main 先建好再
    # include_router，这里只做运行期取值）
    from .. import main as _main
    return _main._active_ws


def _pcb_root(ws) -> Path:
    """PCB 任务归档根：<ws>/pcb/<job_id>/（jobs.run_pcb_job 的 archive）。"""
    return ws.root / "pcb"


# ---------------------------------------------------------------------------
# 能力与预检
# ---------------------------------------------------------------------------

def _java_available() -> bool:
    """java 在场判定：VOLTPROOF_JAVA > which > 实跑 `java -version` 探测。
    which 在部分 shell 的 PATH 解析不到 java（bench 环境实测），兜底实跑。"""
    import os
    import subprocess as _sp
    if os.environ.get("VOLTPROOF_JAVA"):
        return True
    if shutil.which("java") or shutil.which("java.exe"):
        return True
    try:
        pr = _sp.run(["java", "-version"], capture_output=True, timeout=15,
                     creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        return pr.returncode == 0
    except (OSError, _sp.TimeoutExpired):
        return False

@router.get("/capabilities")
def capabilities():
    k = kicad_backend.find_kicad()
    jar = router_backend.find_freerouting()
    java = _java_available()
    return {
        "available": bool(k and jar and java),
        "kicad": {"found": bool(k), "version": k.version() if k else None,
                  "hint": "设 VOLTPROOF_KICAD=<KiCad 根目录>（bin/ 含 kicad-cli.exe 与 python.exe）或安装 KiCad 10.x"},
        "freerouting": {"found": bool(jar), "version": jar.name if jar else None,
                        "hint": "设 VOLTPROOF_FREEROUTING=<freerouting-*.jar>；须用 JVM JAR（v2.5.0 原生 CLI 有已知缺陷）"},
        "java": {"found": java, "hint": "安装 Java 21+（JVM JAR 依赖）"},
        "catalog_entries": len(parts.load_catalog()["entries"]),
    }


@router.post("/preflight")
def preflight(req: dict):
    """绑定源电路网表做预检：角色分类、阻断项、端口候选、工具在场。
    netlist 直给，或 use_project=true 用项目当前已验证网表。"""
    netlist = _project_netlist(req)
    if isinstance(netlist, JSONResponse):
        return netlist
    ir = netlist_to_ir(netlist)
    # 第一次预检还没有用户勾选。auto_ports 与界面「激励源网络已自动勾选」同一口径。
    if req.get("auto_ports") and not req.get("ports"):
        ports = list(ir.get("port_candidates") or [])
    else:
        ports = req.get("ports") or []
    if not isinstance(ports, list) or len(ports) > 16:
        return JSONResponse(status_code=400, content={"error": "ports 须为 ≤16 项数组"})
    ports = [str(p)[:40] for p in ports]
    caps = capabilities()
    mapped = parts.map_netlist(netlist, ports, options=req.get("options"))
    stale = versions.is_stale(_active_ws(), netlist) if netlist else {"stale": False}
    return {
        "netlist_sha": schema.electrical_hash(netlist) if netlist else None,
        "elements": [{"ref": e["ref"], "kind": e["kind"], "role": e["role"]}
                     for e in ir["elements"]],
        "nets": ir["nets"],
        "port_candidates": ir["port_candidates"],
        "blockers": mapped["report"]["blocked"] if not mapped["ok"] else [],
        "warnings": mapped["report"].get("warnings", []),
        "tools_ready": caps["available"],
        "tool_hints": [h for sec in ("kicad", "freerouting", "java")
                       if not caps[sec]["found"] for h in [caps[sec]["hint"]]],
        "current_board_stale": stale["stale"],
        "current_board_reason": stale.get("reason"),
    }


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------

def _prune_jobs_locked() -> None:
    if len(_jobs) <= _keep:
        return
    finished = [jid for jid, st in _jobs.items() if st.get("result") is not None]
    for jid in finished[:len(_jobs) - _keep]:
        _jobs.pop(jid, None)


@router.post("/jobs")
def create_job(req: dict):
    """提交 PCB 任务。立即返回 job_id；结果轮询 GET /jobs/{id}。
    netlist 直给，或 use_project=true 用项目当前已验证网表。"""
    netlist = _project_netlist(req)
    if isinstance(netlist, JSONResponse):
        return netlist
    if not netlist.strip():
        return JSONResponse(status_code=400, content={"error": "netlist 不能为空"})
    ports = req.get("ports") or []
    if not isinstance(ports, list) or len(ports) > 16:
        return JSONResponse(status_code=400, content={"error": "ports 须为 ≤16 项数组"})
    ports = [str(p)[:40] for p in ports]
    max_passes = int(req.get("max_passes") or 10)
    timeout_s = int(req.get("timeout_s") or 180)

    job_id = schema.new_job_id()
    cancel_event = threading.Event()
    with _jobs_lock:
        _jobs[job_id] = {"result": None, "cancel": cancel_event,
                         "state": "running", "stages": []}
        _prune_jobs_locked()
    ws = _active_ws()
    archive = _pcb_root(ws) / job_id

    def worker():
        try:
            result = jobs.run_pcb_job(
                netlist, ports, archive, ws=ws, job_id=job_id,
                options=req.get("options"),
                max_passes=max_passes, timeout_s=timeout_s,
                cancel_check=cancel_event.is_set,
                on_stage=lambda name, state, detail:
                    _jobs[job_id]["stages"].append(
                        {"stage": name, "state": state,
                         "detail": str(detail or "")[:200]}))
        except Exception as e:  # noqa: BLE001 —— 兜底，任务层自身不抛
            result = {"job_id": job_id, "ok": False, "state": schema.FAILED,
                      "stages": [], "blockers": [str(e)]}
        with _jobs_lock:
            _jobs[job_id]["result"] = result

    threading.Thread(target=worker, daemon=True, name=f"pcb-job-{job_id}").start()
    return {"job_id": job_id, "status_url": f"/api/pcb/jobs/{job_id}"}


@router.get("/jobs/{job_id}")
def job_status(job_id: str):
    with _jobs_lock:
        st = _jobs.get(job_id)
        if st is None:  # 进程重启后内存丢失 → 尝试从归档 manifest 恢复
            manifest = _pcb_root(_active_ws()) / job_id / "manifest.json"
            if manifest.is_file():
                try:
                    return json.loads(manifest.read_text(encoding="utf-8")) | {"archived": True}
                except (json.JSONDecodeError, OSError):
                    pass
            return JSONResponse(status_code=404, content={"error": "任务不存在"})
        out = {"job_id": job_id, "done": st["result"] is not None,
               "stages": st["stages"]}
        if st["result"] is not None:
            out |= st["result"]
        return out


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with _jobs_lock:
        st = _jobs.get(job_id)
        if st is None:
            return JSONResponse(status_code=404, content={"error": "任务不存在"})
        if st["result"] is not None:
            return {"ok": True, "note": "任务已结束"}
        st["cancel"].set()
    return {"ok": True, "note": "已请求取消：在当前阶段完成后停止（布线子进程由看门狗兜底）"}


@router.get("/jobs/{job_id}/events")
def job_events(job_id: str):
    """SSE 进度（§6.2 接口表）：逐阶段 stage 事件 + 终态 done/error。
    与轮询共用同一内存注册表；任务未知立即推 error 收尾。"""
    def gen():
        sent = 0
        try:
            while True:
                with _jobs_lock:
                    st = _jobs.get(job_id)
                if st is None:
                    yield f'data: {json.dumps({"type": "error", "error": "任务不存在"}, ensure_ascii=False)}\n\n'
                    return
                for ev in list(st["stages"])[sent:]:
                    yield f'data: {json.dumps(ev, ensure_ascii=False)}\n\n'
                sent = len(st["stages"])
                if st["result"] is not None:
                    payload = {k: v for k, v in st["result"].items() if k != "stages"}
                    payload["type"] = "done"
                    yield f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
                    return
                time.sleep(0.6)
        except GeneratorExit:  # 客户端断开（前端切页签）——任务照常跑，轮询兜底
            return
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

@router.get("/versions")
def list_pcb_versions():
    ws = _active_ws()
    cur = versions.current(ws)
    return {"current": cur, "versions": versions.list_versions(ws)}


@router.get("/versions/{version_id}")
def pcb_version_detail(version_id: str):
    v = versions.get_version(_active_ws(), version_id)
    if v is None:
        return JSONResponse(status_code=404, content={"error": "版本不存在"})
    return v


@router.post("/apply")
def apply_version(req: dict):
    version_id = str(req.get("version_id") or "")
    expected = req.get("expected_revision")
    try:
        pointer = versions.adopt(_active_ws(), version_id,
                                 int(expected) if expected is not None else None)
    except Conflict as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})
    return {"ok": True, **pointer}


# ---------------------------------------------------------------------------
# 布局调整（§6.2：PUT placement —— 位置调整产生新候选，不直接改当前板）
# ---------------------------------------------------------------------------

@router.put("/versions/{version_id}/placement")
def update_placement(version_id: str, req: dict):
    moves = req.get("moves") or {}
    if not isinstance(moves, dict) or not moves or len(moves) > 64:
        return JSONResponse(status_code=400, content={"error": "moves 须为非空对象（≤64 项）"})
    clean: dict[str, dict] = {}
    for ref, m in moves.items():
        if not isinstance(ref, str) or len(ref) > 16:
            return JSONResponse(status_code=400, content={"error": f"位号非法：{ref!r}"})
        try:
            mm = float(m.get("x")), float(m.get("y"))
            if not (0 <= mm[0] <= 500 and 0 <= mm[1] <= 500):
                raise ValueError
            entry = {"x": mm[0], "y": mm[1]}
            if m.get("rot") is not None:
                entry["rot"] = float(m["rot"]) % 360
            clean[ref] = entry
        except (TypeError, ValueError, AttributeError):
            return JSONResponse(status_code=400, content={"error": f"{ref} 的 x/y/rot 非法（毫米/度）"})
    ws = _active_ws()
    src = versions.get_version(ws, version_id)
    if src is None:
        return JSONResponse(status_code=404, content={"error": "版本不存在"})
    src_board = _pcb_root(ws) / str(src.get("job_id")) / "final" / "board.kicad_pcb"
    if not src_board.is_file():
        return JSONResponse(status_code=410, content={"error": "基板文件缺失"})
    # 源网表/端口从该版本任务归档取（一致性对照同源）
    src_input = _pcb_root(ws) / str(src.get("job_id")) / "input"
    netlist = (src_input / "netlist.txt").read_text(encoding="utf-8") \
        if (src_input / "netlist.txt").is_file() else ""
    ports = []
    if (src_input / "ports.json").is_file():
        try:
            ports = json.loads((src_input / "ports.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            ports = []
    if not netlist:
        return JSONResponse(status_code=410, content={"error": "源任务的网表归档缺失"})
    kicad = kicad_backend.find_kicad()
    jar = router_backend.find_freerouting()
    if kicad is None or jar is None:
        return JSONResponse(status_code=400, content={"error": "PCB 工具不全，无法重布线"})
    job_id = schema.new_job_id()
    result = jobs.run_pcb_job(netlist, ports, _pcb_root(ws) / job_id, ws=ws,
                              options=req.get("options"),
                              job_id=job_id, base_board=src_board,
                              parent_version=version_id, moves=clean,
                              max_passes=int(req.get("max_passes") or 10))
    return {k: v for k, v in result.items() if k != "stages"} | {"stages": result["stages"]}


# ---------------------------------------------------------------------------
# 回导再检查（§3 步骤 8：KiCad 修改板文件后重跑同一套检查）
# ---------------------------------------------------------------------------

@router.post("/recheck")
def recheck(req: dict):
    """对已采用版本的归档板文件重跑 DRC + 网络分区对照（用户在 KiCad
    里修改后调用——原检查结论对修改后的板不再有效，须复核）。"""
    version_id = str(req.get("version_id") or "")
    ws = _active_ws()
    version = versions.get_version(ws, version_id)
    if version is None:
        return JSONResponse(status_code=404, content={"error": "版本不存在"})
    src_input = _pcb_root(ws) / str(version.get("job_id")) / "input"
    netlist = (src_input / "netlist.txt").read_text(encoding="utf-8") \
        if (src_input / "netlist.txt").is_file() else ""
    ports = []
    if (src_input / "ports.json").is_file():
        try:
            ports = json.loads((src_input / "ports.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            ports = []
    board = _pcb_root(ws) / str(version.get("job_id")) / "final" / "board.kicad_pcb"
    kicad = kicad_backend.find_kicad()
    if not board.is_file():
        return JSONResponse(status_code=410, content={"error": "板文件缺失"})
    if kicad is None:
        return JSONResponse(status_code=400, content={"error": "KiCad 不可用"})
    drc = kicad_backend.drc(kicad, board,
                            _pcb_root(ws) / str(version.get("job_id")) / "checks" / "drc_recheck.json")
    # 板级引脚对照（验收 F01）：复核必须读【当前板文件的真实焊盘】，
    # 而不是归档映射报告的转述——KiCad 里改了焊盘网络也会被发现
    issues: list[str] = []
    preview = kicad_backend.preview_from_pcb(board)
    spec = _spec_from_archive(ws, version)
    if netlist and preview is not None and spec is not None:
        ir = netlist_to_ir(netlist)
        board_part = connectivity.board_partition_with_map(preview, ir, ports, spec)
        issues.extend(board_part.get("issues", []))
    elif netlist:
        issues.append("无法从归档重建映射规格/板文件解析失败——板级一致性未证明")
    ok = bool(drc["ok"] and drc["violations"] == 0 and drc["unconnected"] == 0
              and not issues)
    # 复核结论回写版本记录（验收 F04：失败不得保持 checked 继续导出）
    versions.update_version_status(
        ws, version_id,
        status=schema.CHECKED if ok else schema.RECHECK_FAILED,
        last_recheck={"ok": ok, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "errors": drc.get("violations"), "unconnected": drc.get("unconnected"),
                      "partition_issues": issues[:8],
                      "board_hash": _sha256_file(board)})
    return {"ok": ok,
            "drc": drc, "partition_issues": issues,
            "board_hash_now": _sha256_file(board),
            "board_hash_adopted": version.get("board_hash"),
            "version_status": schema.CHECKED if ok else schema.RECHECK_FAILED,
            "note": "回导复核：哈希不一致说明板在 KiCad 中被修改过（正常流程）；"
                    "复核未过将阻断导出，重新生成制板包前须修复板文件"}


def _spec_from_archive(ws, version) -> dict | None:
    """从归档映射报告重建板规格（ref→part 映射 + 端子表）——板级对照用。"""
    rep = _pcb_root(ws) / str(version.get("job_id")) / "checks" / "mapping_report.json"
    try:
        r = json.loads(rep.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    comps = []
    for m in r.get("mapped", []):
        comps.append({"ref": m["ref"], "part": m["part"],
                      "pads": [d["net"] for _, d in sorted(m["pin_map"].items(),
                                                           key=lambda kv: int(kv[0]))]})
    for p in r.get("ports", []):
        comps.append({"ref": p["ref"], "part": "CONN_HDR_1X02",
                      "pads": [p["net"], "GND"]})
    return {"components": comps} if comps else None


def _sha256_file(path: Path) -> str | None:
    try:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


# ---------------------------------------------------------------------------
# 导出（冻结制板包：从已采用版本的归档目录直接打包，不重新生成）
# ---------------------------------------------------------------------------

@router.get("/preview/{version_id}")
def preview(version_id: str):
    """候选/已采用版本的板预览数据（板框/焊盘/走线/过孔，毫米坐标）。"""
    ws = _active_ws()
    version = versions.get_version(ws, version_id)
    if version is None:
        return JSONResponse(status_code=404, content={"error": "版本不存在"})
    board = _pcb_root(ws) / str(version.get("job_id")) / "final" / "board.kicad_pcb"
    if not board.is_file():
        return JSONResponse(status_code=410,
                            content={"error": "板文件缺失（工作区被移动或清理）"})
    data = kicad_backend.preview_from_pcb(board)
    if data is None:
        return JSONResponse(status_code=422, content={"error": "板文件解析失败"})
    data["version_id"] = version_id
    data["status"] = version.get("status")
    return data


@router.post("/export")
def export_package(req: dict):
    version_id = str(req.get("version_id") or "")
    ws = _active_ws()
    version = versions.get_version(ws, version_id)
    if version is None:
        return JSONResponse(status_code=404, content={"error": "版本不存在"})
    if version.get("status") != schema.CHECKED:
        return JSONResponse(status_code=400,
                            content={"error": "仅检查通过的版本可导出制板包"})
    cur = versions.current(ws)
    if cur is None or cur.get("version_id") != version_id:
        return JSONResponse(status_code=400,
                            content={"error": "该版本不是当前采用的板（导出须从当前版本冻结）"})
    fab_dir = _pcb_root(ws) / str(version.get("job_id")) / "fabrication"
    if not fab_dir.is_dir():
        return JSONResponse(status_code=410,
                            content={"error": "制板产物目录缺失（工作区被移动或清理）"})
    # —— 冻结验证（验收 F04：检查结论必须绑定同一份内容）——
    board = _pcb_root(ws) / str(version.get("job_id")) / "final" / "board.kicad_pcb"
    if not board.is_file():
        return JSONResponse(status_code=410, content={"error": "板文件缺失"})
    if _sha256_file(board) != version.get("board_hash"):
        return JSONResponse(status_code=409,
                            content={"error": "板文件已被修改（与检查时哈希不符）："
                                             "请在 KiCad 修改后执行回导再检查并重新导出"})
    files = sorted(p.name for p in fab_dir.iterdir())
    required = [f for f in files if f.endswith(".gbrjob")]
    drill = [f for f in files if f.endswith(".drl")]
    bom = [f for f in files if f == "BOM.csv"]
    missing = []
    if not required:
        missing.append(".gbrjob")
    if not drill:
        missing.append(".drl")
    if not bom:
        missing.append("BOM.csv")
    if missing:
        return JSONResponse(status_code=422,
                            content={"error": f"制板产物不完整（缺 {'、'.join(missing)}），"
                                             f"目录现有 {len(files)} 个文件"})
    # 源电路过期：默认拒绝；显式 history=true 返回历史版本标记（计划允许）
    from .. import project as _project
    try:
        now_netlist = _project.active_netlist(ws.load_project(), verified=True)
    except Exception:  # noqa: BLE001 —— 无项目/无净表（控制器测试替身等）：
        now_netlist = ""  # 源状态不可判定时不阻断，冻结项仍由哈希/状态把关
    historical = bool(now_netlist.strip() and schema.electrical_hash(now_netlist)
                      != version.get("source_netlist_hash"))
    if historical and not req.get("history"):
        return JSONResponse(status_code=409,
                            content={"error": "源电路已变化：该制板包对应旧电路。"
                                             "如需导出历史版本请带 history=true（将明确标记为历史产物）"})
    manifest = {f: _sha256_file(fab_dir / f) for f in files}
    return {"ok": True, "version_id": version_id,
            "board_hash": version.get("board_hash"),
            "status": "historical" if historical else "current",
            "files": files, "file_hashes": manifest,
            "download_dir": str(fab_dir)}
