"""实验报告导出：一段对话 → 单文件 HTML（图片全部内嵌 base64）。

浏览器打开后 Ctrl+P 即得 PDF——刻意不引入 PDF 依赖：报告是给学生看的，
打印样式用最朴素的黑白排版即可。所有数字来自 evidence.json（实测），
本模块只做排版，不产生任何新数值。
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

from . import i18n
from .workspace import Workspace

_STYLE = """\
body { font: 13px/1.7 "Segoe UI", "Microsoft YaHei", sans-serif; color: #111;
       max-width: 860px; margin: 24px auto; padding: 0 16px; }
h1 { font-size: 20px; border-bottom: 2px solid #333; padding-bottom: 6px; }
h2 { font-size: 15px; margin: 26px 0 8px; border-left: 4px solid #444; padding-left: 8px; }
.meta { color: #555; font-size: 12px; margin-bottom: 14px; }
table { border-collapse: collapse; margin: 8px 0; font-size: 12.5px; }
th, td { border: 1px solid #999; padding: 3px 10px; text-align: left; }
.verdict-ok { color: #0a7d32; font-weight: 600; }
.verdict-fail { color: #b35900; font-weight: 600; }
img { max-width: 100%; border: 1px solid #ccc; margin: 6px 0; }
pre { background: #f5f5f5; border: 1px solid #ddd; padding: 8px;
      font: 11.5px/1.5 Consolas, monospace; overflow-x: auto; white-space: pre-wrap; }
.interp { background: #f9f9f0; border-left: 3px solid #999; padding: 6px 10px; margin: 8px 0; }
.dim { color: #777; font-size: 12px; }
@media print { body { margin: 0; } }
"""


def _e(v) -> str:
    """HTML 转义（None 转空串）。"""
    return html.escape(str(v if v is not None else ""))


def _img_tag(p: Path) -> str:
    try:
        if p.is_file() and p.stat().st_size > 0:
            b64 = base64.b64encode(p.read_bytes()).decode()
            return f'<img src="data:image/png;base64,{b64}">'
    except OSError:
        pass
    return ""


def _archived_evidence(ws: Workspace, task_id: str) -> dict | None:
    try:
        f = ws.resolve(Path("docs") / task_id / "evidence.json")
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError, json.JSONDecodeError, PermissionError):
        return None


def _round_html(ws: Workspace, idx: int, request: str, ev: dict) -> str:
    task_id = ev.get("task_id") or ""
    docs = ws.resolve(Path("docs") / task_id) if task_id else None
    ok = bool(ev.get("ok"))
    rejected = bool(ev.get("rejected"))
    parts = [f'<h2>{_e(i18n.t("report.round", n=idx, req=request[:80]))}</h2>']
    if rejected:
        parts.append(f'<p class="verdict-fail">{_e(i18n.t("report.rejected"))}</p>')
        parts.append(f'<div class="interp">{_e(ev.get("interpretation", ""))}</div>')
        return "\n".join(parts)
    verdict_key = {"passed":"verdict.passed", "simulated":"verdict.simulated",
                   "incomplete":"verdict.incomplete", "failed":"verdict.failed",
                   "cancelled":"verdict.cancelled", "explained":"verdict.explained"}.get(ev.get("verdict"))
    verdict = i18n.t(verdict_key) if verdict_key else i18n.t("report.legacy")
    cls = "verdict-ok" if ok else "verdict-fail"
    retries = [r for r in ev.get("retry_log") or []
               if r.get("stage") not in ("generate", "attach")]
    parts.append(f'<p class="{cls}">{_e(verdict)}'
                 f'<span class="dim">{_e(i18n.t("report.elapsed", s=ev.get("elapsed"), n=len(retries)))}</span></p>')
    checks = ev.get("checks") or []
    if checks:
        rows = "".join(
            f"<tr><td>{'✓' if c.get('ok') else '未验证' if c.get('ok') is None else '✗'}</td><td>{_e(c.get('name'))}</td>"
            f"<td>{_e(c.get('detail'))}<br>{_e(c.get('window'))}</td></tr>" for c in checks)
        parts.append(f"<table><tr><th>{_e(i18n.t('report.judge'))}</th><th>{_e(i18n.t('report.check'))}</th><th>{_e(i18n.t('check.col.measured'))}</th></tr>{rows}</table>")
    metrics = ev.get("metrics") or {}
    if metrics:
        rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>"
                       for k, v in list(metrics.items())[:14])
        parts.append(f"<table><tr><th>{_e(i18n.t('chat.metrics'))}</th><th>{_e(i18n.t('report.value'))}</th></tr>{rows}</table>")
    if docs:
        sch = _img_tag(docs / "schematic.png")
        if sch:
            parts.append(f'<p class="dim">{_e(i18n.t("report.schematic"))}</p>{sch}')
        wave = _img_tag(docs / "wave.png")
        if wave:
            parts.append(f'<p class="dim">{_e(i18n.t("report.wave"))}</p>{wave}')
    interp = ev.get("interpretation") or ""
    if interp:
        parts.append(f'<div class="interp">{_e(interp)}</div>')
    netlist = ev.get("netlist") or ""
    if netlist:
        parts.append(f'<p class="dim">{_e(i18n.t("report.netlist"))}</p><pre>{_e(netlist)}</pre>')
    if ev.get("version_id"):
        parts.append(f'<p class="dim">{_e(i18n.t("report.version", id=ev["version_id"], task=task_id))}</p>')
    for warning in ev.get("warnings", []):
        parts.append(f'<p class="verdict-fail">{_e(warning)}</p>')
    if ev.get("provenance"):
        parts.append(f'<details><summary>{_e(i18n.t("report.provenance"))}</summary><pre>{_e(json.dumps(ev["provenance"], ensure_ascii=False, indent=2))}</pre></details>')
    comparison=ev.get("comparison") or {}
    if comparison:
        rows=''.join(f'<tr><td>{_e(m.get("name"))}</td><td>{_e(m.get("left"))}</td><td>{_e(m.get("right"))}</td><td>{_e(m.get("delta"))}</td></tr>' for m in comparison.get("measurements", []))
        parts.append(f'<p class="dim">{_e(i18n.translate(comparison.get("note") or ""))}</p><table><tr><th>{_e(i18n.t("check.col.metric"))}</th><th>{_e(i18n.t("report.before"))}</th><th>{_e(i18n.t("report.after"))}</th><th>{_e(i18n.t("report.delta"))}</th></tr>{rows}</table>')
    if ev.get("changes"):
        parts.append(f'<details><summary>{_e(i18n.t("report.changes"))}</summary><pre>{_e(ev["changes"])}</pre></details>')
    return "\n".join(parts)


def build_report_html(conv: dict, ws: Workspace) -> str:
    """对话 → 完整报告 HTML。证据从任务档案（docs/<task_id>）重建，
    图片内嵌——单文件即可分发，打印即 PDF。"""
    import time as _t

    messages = conv.get("messages") or []
    rounds: list[tuple[str, dict]] = []
    last_user = ""
    for m in messages:
        if m.get("role") == "user":
            last_user = m.get("text", "")
            continue
        ev = m.get("inline")
        if not ev and m.get("task_id"):
            ev = _archived_evidence(ws, m["task_id"])
        if ev is None:
            ev = {"ok": m.get("ok", False), "rejected": m.get("rejected", False),
                  "interpretation": "（该次结果的档案已清理）"}
        rounds.append((last_user, ev))

    n_ok = sum(1 for _r, ev in rounds if ev.get("ok") and not ev.get("rejected"))
    head = [
        f"<!doctype html><html lang=\"{_e(i18n.current())}\"><head><meta charset=\"utf-8\">",
        f"<title>{_e(conv.get('title') or i18n.t('report.title'))}</title>",
        f"<style>{_STYLE}</style></head><body>",
        f"<h1>{_e(i18n.t('report.title'))}</h1>",
        f'<div class="meta">{_e(i18n.t("report.meta", title=conv.get("title") or i18n.t("report.untitled"), rounds=len(rounds), ok=n_ok, when=_t.strftime("%Y-%m-%d %H:%M")))}</div>',
        f'<p class="dim">{_e(i18n.t("report.blurb"))}</p>',
    ]
    body = [_round_html(ws, i + 1, req, ev) for i, (req, ev) in enumerate(rounds)]
    return "\n".join(head + body + ["</body></html>"])


def build_project_report(ws: Workspace, version_id=None) -> str:
    from . import project
    p = ws.load_project()
    version_id = version_id or p.get("version_id")
    selected = project.read_version(ws, version_id) if version_id else None
    title = p.get("name") or i18n.t("report.unnamed")
    parts = [f'<!doctype html><html lang="{_e(i18n.current())}"><meta charset="utf-8"><title>{_e(i18n.t("report.projectTitle", title=title))}</title>',
             f'<style>{_STYLE}</style><body><h1>{_e(i18n.t("report.project"))}</h1><p>{_e(title)}</p>']
    if p.get("dirty"):
        parts.append(f'<p class="verdict-fail">{_e(i18n.t("report.dirty"))}</p>')
    if selected:
        parts.append(f'<p>选择的电路版本：{_e(selected["id"])}；来源：{_e(selected.get("source"))}</p>')
        ev = _archived_evidence(ws, selected.get("evidence_id", ""))
        if ev:
            parts.append(_round_html(ws, 1, ev.get("request", "当前版本"), {**ev, "version_id":selected["id"]}))
        else:
            parts.append(f'<p class="dim">该版本没有完整仿真档案，保留网表供复现。</p><pre>{_e(selected.get("netlist"))}</pre>')
    if not version_id:
        parts.append('<p>项目还没有已采用的电路版本。</p>')
    parts.append('<h2>项目实验记录</h2>')
    n = 1
    for task in reversed(ws.list_tasks()):
        if task.get("source") == "whatif-point" or (selected and task.get("task_id") == selected.get("evidence_id")):
            continue
        ev = _archived_evidence(ws, task["task_id"])
        if ev:
            n += 1
            parts.append(_round_html(ws, n, ev.get("request", "实验"), ev))
    for folder in sorted(ws.docs_dir.iterdir()) if ws.docs_dir.is_dir() else []:
        scan = folder / "scan.json"
        if not scan.is_file():
            continue
        try:
            d = json.loads(scan.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        parts.append(f'<h2>参数扫描：{_e(d.get("component"))}</h2><p class="dim">基于版本 {_e(d.get("version_id"))}</p>')
        rows = ''.join(f'<tr><td>{_e(pt.get("value"))}</td><td>{_e(pt.get("metric"))}</td><td>{_e(i18n.t("report.hit") if pt.get("passed") is True else i18n.t("report.miss") if pt.get("passed") is False else i18n.t("report.notarget"))}</td></tr>' for pt in d.get("points", []))
        parts.append(f'<table><tr><th>参数</th><th>{_e(d.get("metric_name"))}</th><th>目标验收</th></tr>{rows}</table>')
        parts.append(_img_tag(folder / "whatif.png"))
    parts.append(_pcb_section(ws))
    parts.append('</body></html>')
    return '\n'.join(parts)


def _pcb_section(ws: Workspace) -> str:
    """PCB 段：当前采用的板版本 + 候选历史（证据边界内的只读汇总）。"""
    try:
        from .pcb import schema as pcb_schema
        from .pcb import versions as pcb_versions
    except ImportError:
        return ""
    cur = pcb_versions.current(ws)
    if cur is None:
        return ""
    v = pcb_versions.get_version(ws, cur["version_id"])
    if not v:
        return ""
    state_txt = {"checked": "检查通过（DRC 0 违规 0 未连）", "routed": "已布线（检查未完成）"}.get(
        v.get("status"), v.get("status", "?"))
    rows = "".join(
        f"<tr><td>{_e(item.get('stage'))}</td><td>{'✓' if item.get('ok') else '✗'}</td>"
        f"<td>{_e(str(item.get('detail', ''))[:120])}</td><td>{item.get('elapsed', '')}s</td></tr>"
        for item in _pcb_job_stages(ws, v.get("job_id")))
    return (f'<h2>PCB 制板记录</h2>'
            f'<p>当前采用板版本：{_e(v["pcb_version_id"])}（{state_txt}，采用于 {_e(cur.get("adopted_at", ""))}）</p>'
            f'<p class="dim">工具：{_e(str(v.get("tools", {}).get("kicad", "?")))} + '
            f'{_e(str(v.get("tools", {}).get("freerouting", "?")))}；'
            f'绑定电路电气内容哈希 {_e(str(v.get("source_netlist_hash", "")))}'
            f'（电路变更将使本板标记过期）</p>'
            + (f'<table><tr><th>阶段</th><th>结果</th><th>说明</th><th>耗时</th></tr>{rows}</table>' if rows else "")
            + '<p class="dim">DRC 通过不等于实物已验证：寄生参数、热性能与装配效果需实测（见 PCB 方案 §3）。</p>')


def _pcb_job_stages(ws: Workspace, job_id: str | None) -> list[dict]:
    if not job_id:
        return []
    manifest = ws.root / "pcb" / str(job_id) / "manifest.json"
    try:
        d = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    stages = d.get("stages") or []
    # mapping 阶段在早期任务里可能重复记录（已修），报告只展示一次
    seen, out = set(), []
    for s in stages:
        key = s.get("stage")
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
