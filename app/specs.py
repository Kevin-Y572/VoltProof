"""需求规格与确定性验收。产品、MCP、扫描和基准共用此口径。"""
from __future__ import annotations

import copy
import math
import re

from . import i18n, measure

VERSION = "specs-1"
NAMES = {"fc": "截止频率", "freq": "主频", "gain": "增益", "vpp": "峰峰值",
         "ripple": "稳态纹波", "mean": "输出电压", "rms": "有效值", "thd": "总谐波失真",
         "component_value":"元件参数"}
UNITS = {"fc": "Hz", "freq": "Hz", "gain": "倍", "vpp": "V", "ripple": "V",
         "mean": "V", "rms": "V", "thd": "%"}
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
VALUE = rf"(?P<number>{NUMBER})\s*(?P<prefix>Meg|meg|[kKmMuUnNpPμµ]?)\s*(?:Hz|hz|赫兹|V|v|伏|倍)?"
OPS = r"(?:不超过|不大于|不高于|小于等于|不低于|不小于|至少|大于等于|小于|低于|大于|高于|<=|>=|≤|≥|<|>|约为|约|为|是|到|=|:|：|≈)?"


def number(text: str) -> float:
    m = re.fullmatch(rf"\s*({NUMBER})\s*(Meg|meg|[kKmMuUnNpPμµ]?)\s*", text)
    if not m:
        raise ValueError(i18n.t("api.spec_number", text=text))
    factors = {"": 1, "k": 1e3, "K": 1e3, "M": 1e6, "Meg": 1e6, "meg": 1e6,
               "m": 1e-3, "u": 1e-6, "U": 1e-6, "μ": 1e-6, "µ": 1e-6,
               "n": 1e-9, "N": 1e-9, "p": 1e-12, "P": 1e-12}
    value = float(m[1]) * factors[m[2]]
    if not math.isfinite(value):
        raise ValueError(i18n.t("api.spec_finite"))
    return value


def normalize(specs: list[dict]) -> list[dict]:
    if not isinstance(specs, list) or len(specs) > 24:
        raise ValueError(i18n.t("api.spec_array"))
    result = []
    def numeric(value):
        try:
            return float(value)
        except (ValueError, TypeError, OverflowError) as e:
            raise ValueError(i18n.t("api.spec_numeric")) from e
    for raw in specs:
        if not isinstance(raw, dict):
            raise ValueError(i18n.t("api.spec_object"))
        metric = str(raw.get("metric", ""))[:40]
        op = str(raw.get("op", "near"))
        if op not in ("near", "lt", "le", "gt", "ge", "range"):
            raise ValueError(i18n.t("api.spec_op"))
        target = numeric(raw.get("target", 0))
        tol = numeric(raw.get("tol", 0.05))
        if not math.isfinite(target) or not math.isfinite(tol) or not 0 <= tol <= 1:
            raise ValueError(i18n.t("api.spec_tol"))
        spec = {"metric": metric, "op": op, "target": target, "tol": tol,
                "name": str(raw.get("name") or NAMES.get(metric, metric))[:100],
                "unit": str(raw.get("unit") or UNITS.get(metric, ""))[:16],
                "out": str(raw.get("out", "out"))[:80], "in": str(raw.get("in", "in"))[:80],
                "explicit_nodes": bool(raw.get("explicit_nodes", False)),
                "source": str(raw.get("source", "用户设置"))[:300],
                "skip": numeric(raw.get("skip", 0.2)),
                "filter_type": str(raw.get("filter_type", "lowpass"))[:20]}
        if raw.get("component"):
            spec["component"] = str(raw["component"])[:32]
        if spec["filter_type"] not in ("lowpass","highpass"):
            raise ValueError(i18n.t("api.spec_filter"))
        if not math.isfinite(spec["skip"]) or not 0 <= spec["skip"] < 0.95:
            raise ValueError(i18n.t("api.spec_skip"))
        if op == "range":
            spec["upper"] = numeric(raw.get("upper"))
            if not math.isfinite(spec["upper"]) or spec["upper"] < target:
                raise ValueError(i18n.t("api.spec_upper"))
        result.append(spec)
    return result


def _filter_type(text: str, start: int = 0, end: int | None = None) -> str:
    """从目标短语判断低通/高通。否定和对比分句里的「高通」不把低通目标翻成高通。"""
    if end is None:
        end = start
    parts: list[tuple[int, int, str]] = []
    cursor = 0
    for mark in re.finditer(r"[，。；;\n]", text):
        parts.append((cursor, mark.start(), text[cursor:mark.start()]))
        cursor = mark.end()
    parts.append((cursor, len(text), text[cursor:]))

    def kind(clause: str) -> str | None:
        cleaned = re.sub(
            r"(?:不要做成|而不要|而不是|而非|不要|别|并非|不是|勿|而不能|不能|无需|不用|避免|非)"
            r".{0,12}(?:高通|低通)",
            "", clause)
        cleaned = re.sub(r"(?:对比|比较|相比|对照|比起).{0,12}(?:高通|低通)", "", cleaned)
        high = "高通" in cleaned
        low = "低通" in cleaned
        if high and not low:
            return "highpass"
        if low and not high:
            return "lowpass"
        if high and low:
            return "lowpass" if cleaned.find("低通") <= cleaned.find("高通") else "highpass"
        return None

    for begin, stop, clause in parts:
        if begin <= start < stop or begin < end <= stop or (start <= begin and stop <= end):
            local = kind(clause)
            if local:
                return local
            break
    for _begin, _stop, clause in parts:
        found = kind(clause)
        if found:
            return found
    return "lowpass"


def extract(text: str, inherited: list[dict] | None = None, previous_checks=None) -> list[dict]:
    """保守提取常见明确指标。未覆盖的量化目标留下未验证项，禁止默认为达标。"""
    result = copy.deepcopy(inherited or [])
    text=re.sub(rf"从\s*{NUMBER}\s*(?:Meg|[kKmMuUnNpPμµ])?\s*(?:Hz|赫兹|V|伏|倍|Ω|欧)?\s*(?:改成|改为|改到|调整到|提高到|降低到)","改到",text,flags=re.I)
    tol_m = re.search(r"(?:±|误差(?:不超过|小于|≤|<|为)?|容差|tolerance\s*)\s*(\d+(?:\.\d+)?)\s*%", text, re.I)
    tol = float(tol_m[1]) / 100 if tol_m else 0.05
    patterns = {
        "fc": r"(?:截止频率|cutoff(?:\s+frequency)?|\bfc\b)(?:改到|改成|改为|设为|为|是|约|of|to|\s)*",
        "gain": r"(?:增益|\bgain\b)(?:幅度|改成|改为|改到|设为|为|是|约|of|to|\s)*",
        "ripple": r"(?:纹波|\bripple\b)(?:峰峰值|电压|幅度|改成|改为|改到|为|to|\s)*",
        "vpp": r"(?:输出峰峰值|峰峰值|\bvpp\b)(?:改成|改为|改到|为|to|\s)*",
        "mean": r"(?:输出(?:直流)?(?:电压)?|output\s+voltage|稳压到|分出)(?:改成|改为|改到|设为|为|是|约|of|to|\s)*",
        "freq": r"(?:振荡频率|主频|\bfrequency\b)(?:改成|改为|改到|设为|为|是|约|of|to|\s)*",
        "thd": r"(?:THD|总谐波失真)(?:改成|改为|改到|为|to|\s)*",
    }
    found = []
    for metric, prefix in patterns.items():
        m = re.search(prefix + rf"(?P<op>{OPS})\s*{VALUE}", text, re.I)
        if not m:
            continue
        op_text = m["op"] or ""
        op = "near"
        if re.search(r"不超过|不大于|不高于|小于等于|<=|≤", op_text):
            op = "le"
        elif re.search(r"不低于|不小于|至少|大于等于|>=|≥", op_text):
            op = "ge"
        elif re.search(r"小于|低于|<", op_text):
            op = "lt"
        elif re.search(r"大于|高于|>", op_text):
            op = "gt"
        value = number(m["number"] + m["prefix"])
        found.append({"metric": metric, "op": op, "target": value, "tol": tol,
                      "source": m[0], "filter_type": _filter_type(text, m.start(), m.end())})
    if not any(s["metric"] == "fc" for s in found) and re.search(r"低通|高通", text):
        m = re.search(rf"{VALUE}\s*(?:的)?\s*(?:RC\s*)?(?:低通|高通)", text, re.I)
        if m:
            found.append({"metric": "fc", "target": number(m["number"] + m["prefix"]),
                          "tol": tol, "source": m[0], "filter_type": _filter_type(text, m.start(), m.end())})
    for keyword in ("功耗", "相位裕度", "带宽", "输入阻抗", "输出阻抗"):
        if re.search(re.escape(keyword) + rf"[^，。；\n]{{0,15}}{NUMBER}", text):
            found.append({"metric": keyword, "target": 0, "source": "此指标需要专用测量设置"})
    if re.search(r"纹波.*(?:一半|减半)", text):
        old = next((c for c in previous_checks or [] if c.get("metric") == "ripple" and c.get("measured") is not None), None)
        if old:
            found.append({"metric": "ripple", "op": "le", "target": old["measured"] / 2, "source": "上次实测纹波的一半"})
        else:
            found.append({"metric": "纹波相对目标", "target": 0, "source": "缺少上次纹波实测值，无法计算一半"})
    for item in found:
        result = [s for s in result if s["metric"] != item["metric"]] + [item]
    for m in re.finditer(rf"(?P<component>[RCLVI]\d+)\s*(?:改成|改为|设为|设置为|=|to)\s*{VALUE}", text, re.I):
        component=m["component"]
        result=[s for s in result if s.get("component", "").lower()!=component.lower()]
        result.append({"metric":"component_value", "component":component, "target":number(m["number"]+m["prefix"]),
                       "tol":0, "name":f"{component} 参数", "source":m[0],
                       "unit":{"R":"Ω","C":"F","L":"H","V":"V","I":"A"}[component[0].upper()]})
    return normalize(result)


def target_label(spec: dict) -> str:
    value = f"{spec['target']:g} {spec.get('unit', '')}".strip()
    op = spec.get("op", "near")
    if op == "near":
        return f"{value} ±{spec.get('tol', .05):.0%}"
    if op == "range":
        return f"{spec['target']:g}–{spec['upper']:g} {spec.get('unit', '')}"
    return {"lt": "<", "le": "≤", "gt": ">", "ge": "≥"}[op] + " " + value


def evaluate(traces, specs: list[dict], netlist: str = "") -> list[dict]:
    checks = []
    for spec in specs:
        if spec["metric"] == "component_value":
            from .tuner import _spice_val
            values=[]
            depth=0
            for line in netlist.splitlines():
                tokens=line.split()
                if not tokens:
                    continue
                if tokens[0].lower()==".subckt": depth+=1
                if tokens[0].lower()==".ends": depth=max(0,depth-1)
                if depth or tokens[0].lower()!=spec.get("component", "").lower() or len(tokens)<4:
                    continue
                idx=4 if tokens[3].lower()=="dc" else 3
                if idx >= len(tokens):
                    values.append(None)
                    continue
                token = tokens[idx]
                # 花括号和表达式不是字面量；_spice_val 对无法解析的文本返回 0，不能拿来判达标。
                if re.fullmatch(r"[-+0-9.eE]+[a-zA-Z]*", token) is None:
                    values.append(None)
                    continue
                try:
                    values.append(_spice_val(token))
                except (ValueError, IndexError, KeyError):
                    values.append(None)
            value,error=(values[0],"") if len(values)==1 and values[0] is not None else (None, i18n.t_zh("api.spec_comp"))
        else:
            value, error = measure.compute_metric(traces, spec["metric"], spec["out"], spec["in"],
                                              netlist=netlist, exact=spec.get("explicit_nodes", False),
                                              skip=spec["skip"], filter_type=spec["filter_type"],
                                              signed=spec["metric"] == "gain" and spec["target"] < 0)
        if value is not None and not math.isfinite(value):
            value, error = None, i18n.t_zh("api.spec_nonfinite")
        target, op = spec["target"], spec["op"]
        ok = None
        if value is not None:
            ok = {"near": lambda: abs(value - target) <= max(abs(target) * spec["tol"], 1e-12),
                  "lt": lambda: value < target, "le": lambda: value <= target,
                  "gt": lambda: value > target, "ge": lambda: value >= target,
                  "range": lambda: target <= value <= spec["upper"]}[op]()
        label = target_label(spec)
        if spec["metric"] == "component_value":
            window_key, window = "component", i18n.t_zh("window.component")
        elif traces and traces.analysis == "tran":
            window_key, window = "tran", i18n.t_zh("window.tran", pct=f"{spec['skip']:.0%}".rstrip("%"))
        else:
            window_key, window = "full", i18n.t_zh("window.full")
        kind = i18n.t_zh("detail.kind.netlist") if spec["metric"] == "component_value" else i18n.t_zh("detail.kind.measured")
        detail = error if value is None else i18n.t_zh("detail.line", kind=kind, value=f"{value:.6g}", unit=spec["unit"], target=label)
        checks.append({"name": spec["name"], "metric": spec["metric"], "ok": ok,
                       "status": "unverified" if ok is None else ("passed" if ok else "failed"),
                       "target": target, "target_label": label, "measured": value,
                       "unit": spec["unit"], "out": spec["out"], "in": spec["in"],
                       "window_key": window_key, "skip": spec["skip"],
                       "component": spec.get("component", ""),
                       "window": window, "detail": detail})
    return checks


def ac_verification_netlist(netlist: str, requirements: list[dict]):
    """截止频率使用固定扫频窗口和原始节点，生成器不能缩短范围或更换输出。"""
    cutoffs=[s for s in requirements if s["metric"]=="fc" and s["target"]>0]
    if not cutoffs or any(s["metric"] not in ("fc","gain","component_value") for s in requirements):
        return netlist, None
    from .ngspice_runner import check_netlist_safety
    if check_netlist_safety(netlist):
        return netlist, None  # 原始危险指令仍须由安全门拒绝，不能通过清洗绕过。
    low=min(s["target"] for s in cutoffs)/100
    high=max(s["target"] for s in cutoffs)*100
    if low<=0 or not math.isfinite(high):
        return netlist, None
    nodes=[]
    for s in requirements:
        if s["metric"]=="component_value": continue
        for key in ("out","in"):
            node=s[key]
            if not re.fullmatch(r"[A-Za-z0-9_]+",node): return netlist,None
            if node not in nodes: nodes.append(node)
    control=f".control\nset filetype=ascii\nac dec 100 {low:g} {high:g}\nwrite out.raw " + " ".join(f"v({n})" for n in nodes) + "\n.endc\n"
    lines=[];inside=False
    for line in netlist.splitlines():
        directive=line.strip().lower()
        if directive==".control": inside=True;continue
        if directive==".endc": inside=False;continue
        if inside or re.match(r"^\.(?:ac|tran|op)\b",directive): continue
        if directive==".end": continue
        lines.append(line)
    return "\n".join(lines)+"\n"+control+".end\n", {"analysis":"ac", "points_per_decade":100,
        "start_hz":low, "stop_hz":high, "nodes":nodes, "source":"固定需求验收设置"}


def validators(specs: list[dict]) -> list:
    def validate(ev, traces):
        return evaluate(traces, specs, ev.netlist)
    return [validate] if specs else []


def classify(text: str, has_circuit: bool = False, attachment: bool = False) -> str:
    if re.search(r"(?:不要|别|无需|不用)(?:修改|改动|修复)|只(?:验证|检查|仿真)|verify\s+only", text, re.I):
        return "verify"
    if re.search(r"改(?:成|为|到|一下|进|善|小|大)|修改|调整|增大|减小|降低|提高|重选|\b(?:change|modify|replace|tune)\b", text, re.I):
        return "modify"
    if re.search(r"对比|比较|\bcompare\b", text, re.I):
        return "compare"
    if re.search(r"解释|为什么|什么原理|解读|\b(?:explain|why)\b", text, re.I):
        return "explain"
    if re.search(r"修复|排查|报错|诊断|\b(?:repair|debug|fix)\b", text, re.I):
        return "diagnose"
    if re.search(r"重新设计|新建|从头|设计|\bdesign\b", text, re.I):
        return "design"
    if attachment or re.search(r"验证|检查|仿真|\bverify\b", text, re.I):
        return "verify" if has_circuit else "design"
    return "modify" if has_circuit else "design"
