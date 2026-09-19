"""核心编排：生成 → 静态检查 → 仿真 → 失败重试 → 证据组装。  [W1 串联 / W2 重试 / W3 会话]

用法（W1 命令行验证）：
    python -m app.pipeline "设计一个截止频率1kHz的RC低通滤波器"

这是整个产品的灵魂模块："仿真在环"循环。
每次 LLM 的产出都要过检查器和 ngspice，失败就把真实报错喂回去修复，最多 max_retries 次。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import checks, llm, measure, prompts
from .ngspice_runner import run_netlist
from .render_schematic import render_schematic
from .render_wave import render_wave

MAX_RETRIES = 3
OUT_DIR = Path(__file__).resolve().parent.parent / "out"


@dataclass
class Evidence:
    """一次请求的完整证据包，序列化后就是 /chat 接口的响应体。"""

    ok: bool = False
    request: str = ""
    netlist: str = ""
    waveform_b64: str | None = None
    schematic_b64: str | None = None
    metrics: dict = field(default_factory=dict)
    interpretation: str = ""
    retry_log: list[dict] = field(default_factory=list)  # 每轮 {round, stage, problems}
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "request": self.request,
            "netlist": self.netlist,
            "waveform_b64": self.waveform_b64,
            "schematic_b64": self.schematic_b64,
            "metrics": self.metrics,
            "interpretation": self.interpretation,
            "retry_log": self.retry_log,
            "elapsed": round(self.elapsed, 1),
        }


def _b64_png(png: Path) -> str:
    return base64.b64encode(png.read_bytes()).decode()


def run_pipeline(request: str, previous_netlist: str | None = None,
                 max_retries: int = MAX_RETRIES) -> Evidence:
    ev = Evidence(request=request)
    t0 = time.monotonic()

    netlist = llm.extract_code_block(
        llm.chat(prompts.GENERATE_SYSTEM, prompts.generate_user(request, previous_netlist))
    )
    ev.retry_log.append({"round": 0, "stage": "generate", "problems": []})

    for rnd in range(max_retries + 1):
        # ---- 静态检查 ----
        chk = checks.run_checks(netlist)
        if not chk.ok:
            ev.retry_log.append({"round": rnd, "stage": "static-check", "problems": chk.errors})
            if rnd == max_retries:
                break
            netlist = _repair(netlist, chk.errors)
            continue

        # ---- 仿真 ----
        sim = run_netlist(netlist, workdir=OUT_DIR)
        if not sim.ok or sim.raw_path is None:
            ev.retry_log.append({"round": rnd, "stage": "simulate",
                                 "problems": [sim.error_snippet or "仿真失败且无报错文本"]})
            if rnd == max_retries:
                break
            netlist = _repair(netlist, [sim.error_snippet])
            continue

        # ---- 成功：解析、测指标、出图、解读 ----
        try:
            tr = measure.load_traces(sim.raw_path)
            ev.metrics = measure.extract_metrics(tr)
            png = render_wave(tr, OUT_DIR / "wave.png", title=request[:40])
            ev.waveform_b64 = _b64_png(png)
        except Exception as e:  # 波形解析失败不致命，证据卡降级
            ev.retry_log.append({"round": rnd, "stage": "measure", "problems": [f"波形解析失败: {e}"]})

        ev.netlist = netlist
        ev.ok = True
        ev.interpretation = llm.chat(
            prompts.INTERPRET_SYSTEM,
            prompts.interpret_user(request, netlist, ev.metrics or {"提示": "未解析到波形数据"}),
        )

        # 电路原理图（W4）：LLM 生成 schemdraw 代码，受限执行，失败静默降级为网表
        try:
            code = llm.extract_code_block(
                llm.chat(prompts.SCHEMATIC_SYSTEM, prompts.schematic_user(netlist))
            )
            png = render_schematic(code, OUT_DIR / "schematic.png")
            if png:
                ev.schematic_b64 = _b64_png(png)
        except Exception:
            pass
        break

    ev.elapsed = time.monotonic() - t0
    return ev


def _repair(netlist: str, problems: list[str]) -> str:
    fixed = llm.chat(prompts.REPAIR_SYSTEM, prompts.repair_user(netlist, problems))
    return llm.extract_code_block(fixed)


# ---------------------------------------------------------------------------
# 会话（W3）：多轮修改作用于同一电路
# ---------------------------------------------------------------------------

_SESSIONS: dict[str, dict] = {}


def get_session(session_id: str | None = None) -> dict:
    sid = session_id or uuid.uuid4().hex[:12]
    if sid not in _SESSIONS:
        _SESSIONS[sid] = {"id": sid, "netlist": None, "history": []}
    return _SESSIONS[sid]


def chat_with_session(session_id: str, message: str) -> dict:
    s = get_session(session_id)
    ev = run_pipeline(message, previous_netlist=s["netlist"])
    if ev.ok:
        s["netlist"] = ev.netlist  # 只有验证通过的电路才进入会话状态
    s["history"].append({"user": message, "ok": ev.ok})
    d = ev.to_dict()
    d["session_id"] = s["id"]  # 空入参时客户端也能拿到新建的会话 id
    return d


if __name__ == "__main__":
    import sys

    req = " ".join(sys.argv[1:]) or "设计一个截止频率1kHz的RC低通滤波器"
    result = run_pipeline(req)
    print(json.dumps({k: v for k, v in result.to_dict().items() if k != "waveform_b64"},
                     ensure_ascii=False, indent=2))
    if result.waveform_b64:
        (OUT_DIR / "wave.png").exists() and print(f"\n波形图: {OUT_DIR / 'wave.png'}")
    sys.exit(0 if result.ok else 1)
