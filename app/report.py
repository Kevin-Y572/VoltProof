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
    parts = [f'<h2>第 {idx} 轮 · {_e(request[:80])}</h2>']
    if rejected:
        parts.append('<p class="verdict-fail">超出能力范围（前置拒绝，未消耗生成与仿真）</p>')
        parts.append(f'<div class="interp">{_e(ev.get("interpretation", ""))}</div>')
        return "\n".join(parts)
    verdict = "✅ 仿真验证通过" if ok else "⚠️ 自动修复后仍未通过"
    cls = "verdict-ok" if ok else "verdict-fail"
    retries = [r for r in ev.get("retry_log") or []
               if r.get("stage") not in ("generate", "attach")]
    parts.append(f'<p class="{cls}">{verdict}'
                 f'<span class="dim"> · 耗时 {_e(ev.get("elapsed"))}s · 修复环 {len(retries)} 轮</span></p>')
    checks = ev.get("checks") or []
    if checks:
        rows = "".join(
            f"<tr><td>{'✓' if c.get('ok') else '✗'}</td><td>{_e(c.get('name'))}</td>"
            f"<td>{_e(c.get('detail'))}</td></tr>" for c in checks)
        parts.append(f"<table><tr><th>判定</th><th>检查项</th><th>实测</th></tr>{rows}</table>")
    metrics = ev.get("metrics") or {}
    if metrics:
        rows = "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>"
                       for k, v in list(metrics.items())[:14])
        parts.append(f"<table><tr><th>实测指标</th><th>数值</th></tr>{rows}</table>")
    if docs:
        sch = _img_tag(docs / "schematic.png")
        if sch:
            parts.append(f'<p class="dim">原理图（自动布局）</p>{sch}')
        wave = _img_tag(docs / "wave.png")
        if wave:
            parts.append(f'<p class="dim">仿真波形</p>{wave}')
    interp = ev.get("interpretation") or ""
    if interp:
        parts.append(f'<div class="interp">{_e(interp)}</div>')
    netlist = ev.get("netlist") or ""
    if netlist:
        parts.append(f'<p class="dim">网表（ngspice，可直接复现）</p><pre>{_e(netlist)}</pre>')
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
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
        f"<title>{_e(conv.get('title') or 'VoltProof 实验报告')}</title>",
        f"<style>{_STYLE}</style></head><body>",
        "<h1>VoltProof 实验报告</h1>",
        f'<div class="meta">课题：{_e(conv.get("title") or "（无标题）")} · '
        f"{len(rounds)} 轮设计/验证 · 通过 {n_ok} 轮 · "
        f"导出时间 {_t.strftime('%Y-%m-%d %H:%M')}</div>",
        '<p class="dim">报告中的全部指标由 ngspice 仿真实测计算得出；每轮附完整网表，'
        "可在任何 SPICE 环境复现。</p>",
    ]
    body = [_round_html(ws, i + 1, req, ev) for i, (req, ev) in enumerate(rounds)]
    return "\n".join(head + body + ["</body></html>"])
