"""网表静态检查：在不跑仿真的前提下拦住最典型的低级错误。  [W2]

四类检查（对应路线图 2.2）：
  1. 缺参考地（没有任何元件接到节点 0）
  2. 浮空节点（某节点在所有元件端点里只出现一次 = 悬空）
  3. 可疑数值（1M 这种毫/兆歧义写法直接禁止，要求 Meg；以及 0 阻值/容值）
  4. 缺模型（Q/D/M 元件没有对应 .model/.include）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# SPICE 前缀 -> 元件类别
_PREFIXES = {
    "R": "resistor", "C": "capacitor", "L": "inductor",
    "V": "source", "I": "source",
    "Q": "bjt", "D": "diode", "M": "mosfet",
}


@dataclass
class Netlist:
    lines: list[str] = field(default_factory=list)

    @property
    def element_lines(self) -> list[tuple[int, str]]:
        """返回 (行号, 内容)，只含元件行（字母开头，非点指令、非注释）。"""
        out = []
        for i, raw in enumerate(self.lines, start=1):
            line = raw.strip()
            if not line or line.startswith(("*", ".", "#")):
                continue
            if line.startswith("+"):  # 续行，忽略
                continue
            out.append((i, line))
        return out


@dataclass
class CheckResult:
    ok: bool = True
    errors: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)


def parse_nodes(line: str) -> list[str]:
    """取元件行的节点名（前两三个 token 视元件类型而定，粗取前 4 个 token 中的节点段）。"""
    tokens = line.split()
    if not tokens:
        return []
    name = tokens[0]
    if name[0] in "VCIL":       # V/C/L/I: name n1 n2 value...
        return tokens[1:3]
    if name[0] in "QM":         # Q: name c b e [sub]  M: name d g s b
        return tokens[1:5]
    if name[0] == "D":          # D: name anode cathode [model]
        return tokens[1:3]
    return tokens[1:3]


def run_checks(netlist_text: str) -> CheckResult:
    result = CheckResult()
    lines = [ln for ln in netlist_text.splitlines()]
    nl = Netlist(lines)
    elements = nl.element_lines

    node_count: dict[str, int] = {}
    used_models: set[str] = set()
    defined_models: set[str] = set()

    for lineno, line in elements:
        name = line.split()[0]
        for n in parse_nodes(line):
            node_count[n] = node_count.get(n, 0) + 1
        if name[0] in "QDM":
            m = re.split(r"\s+", line)
            if len(m) >= 4 and name[0] == "D":
                used_models.add(m[3])
            elif len(m) >= 5:
                used_models.add(m[-1])  # 最后一个 token 通常是模型名
        # 数值检查
        for tok in line.split()[1:]:
            if re.fullmatch(r"\d+(\.\d+)?[Mm](?![a-zA-Z])", tok):
                result.fail(f"第 {lineno} 行：'{tok}' 中 M/m 都按毫处理——兆必须写 Meg 或 1000k")
            if re.fullmatch(r"[0-9.]+(u)?F?", tok) and tok.rstrip("F") == "0":
                result.fail(f"第 {lineno} 行：元件值为 0，通常是笔误")

    for raw in lines:
        s = raw.strip()
        if s.lower().startswith(".model"):
            parts = s.split()
            if len(parts) >= 2:
                defined_models.add(parts[1].lower())
        if s.lower().startswith(".include") or s.lower().startswith(".lib"):
            # include 的模型名静态检查无法解析，跳过
            pass

    # 1. 缺地
    if "0" not in node_count and "gnd" not in {n.lower() for n in node_count}:
        result.fail("网表没有参考地（节点 0）——所有电压/电流都失去参考。")

    # 2. 浮空节点（出现次数 = 1 的节点；地除外）
    for n, c in node_count.items():
        if c < 2 and n not in ("0",) and n.lower() != "gnd":
            result.fail(f"节点 {n} 只被一个元件引用，疑似浮空（悬空节点会导致 singular matrix）。")

    # 3. 缺模型
    for m in used_models:
        if m.lower() not in defined_models:
            result.fail(f"元件引用了模型 '{m}'，但没有对应的 .model 定义或 .include。")

    return result


# TODO(W2): 报错信息给出"具体行 + 修法建议"的格式，让 LLM 修复循环命中率更高；
#            用 bench 里故意注入错误的网表做单元测试。
