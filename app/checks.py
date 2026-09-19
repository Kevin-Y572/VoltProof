"""网表静态检查：在不跑仿真的前提下拦住最典型的低级错误。  [W2]

四类检查（对应路线图 2.2）：
  1. 缺参考地（没有任何元件接到节点 0）
  2. 浮空节点（某节点在所有元件端点里只出现一次 = 悬空）
  3. 可疑数值（1M 大写兆歧义写法直接禁止，要求 Meg/1000k；R/C/L 值为 0）
  4. 缺模型（Q/D/M 元件没有对应 .model/.include）

解析纪律：.control/.endc 块内的是仿真命令不是元件；值检查只作用于元件的
值字段（R/C/L 第 4 列、V/I 第 4 列起），绝不把节点名当数值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 元件类别：首字母 -> (节点字段的取法, 最少 token 数)
# V/C/L/I/F/H: name n1 n2 value...
# Q: name c b e [sub] [model] [area]   M: name d g s b [model]
# D: name anode cathode [model]        X: name n1..nN subckt_name
# E/G: name out+ out- in+ in- [gain]（行为源时节点字段退化为前 2 个）


@dataclass
class CheckResult:
    ok: bool = True
    errors: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)


def iter_element_lines(lines: list[str]):
    """产出 (行号, 内容)，只含元件行：跳过注释、点指令、续行和 .control 块。"""
    in_control = False
    for i, raw in enumerate(lines, start=1):
        s = raw.strip()
        if not s or s.startswith(("*", "#")):
            continue
        low = s.lower()
        if low == ".control":
            in_control = True
            continue
        if low == ".endc":
            in_control = False
            continue
        if in_control or s.startswith((".", "+")):
            continue
        yield i, s


def parse_nodes(line: str) -> list[str]:
    tokens = line.split()
    if not tokens:
        return []
    name = tokens[0]
    kind = name[0].upper()
    if kind in "VCLIFH":
        return tokens[1:3]
    if kind == "Q":
        return tokens[1:4]
    if kind == "M":
        return tokens[1:5]
    if kind == "D":
        return tokens[1:3]
    if kind == "X":
        return tokens[1:-1]  # 最后一个 token 是子电路名
    if kind in "EG" and len(tokens) >= 5:
        return tokens[1:5]
    return tokens[1:3]


def value_tokens(line: str) -> list[str]:
    """元件的值字段（不含节点名）。只对 R/C/L 与 V/I 提取。"""
    tokens = line.split()
    kind = tokens[0][0].upper()
    if kind in "RCL":
        return tokens[3:4]
    if kind in "VI":
        return tokens[3:]
    return []


def _model_ref(line: str) -> str | None:
    """Q/D/M 元件引用的模型名；未写返回 None。取端子后最后一个字母开头的 token
    （跳过面积等纯数字参数）。"""
    tokens = line.split()
    kind = tokens[0][0].upper()
    min_terms = {"D": 3, "Q": 4, "M": 5}[kind]
    rest = tokens[min_terms:]
    alpha = [t for t in rest if t[0].isalpha()]
    return alpha[-1] if alpha else None


def run_checks(netlist_text: str) -> CheckResult:
    result = CheckResult()
    lines = netlist_text.splitlines()
    elements = list(iter_element_lines(lines))

    node_count: dict[str, int] = {}
    used_models: dict[str, int] = {}  # 模型名 -> 行号
    defined_models: set[str] = set()

    for lineno, line in elements:
        for n in parse_nodes(line):
            node_count[n] = node_count.get(n, 0) + 1

        # 数值检查：只看值字段
        kind = line.split()[0][0].upper()
        for tok in value_tokens(line):
            if re.fullmatch(r"\d+(\.\d+)?M", tok):
                result.fail(f"第 {lineno} 行：'{tok}' 的 M 会按毫处理——兆必须写 Meg 或 1000k")
            if kind in "RCL" and tok.rstrip("Ff") in ("0", "0.0"):
                result.fail(f"第 {lineno} 行：{kind} 元件值为 0，通常是笔误（0Ω/0F 无意义）")

        # 模型引用检查
        if kind in "QDM":
            m = _model_ref(line)
            if m is None:
                result.fail(f"第 {lineno} 行：{kind} 元件没有写模型名（应引用 .model 定义）")
            else:
                used_models[m] = lineno

    for raw in lines:
        s = raw.strip()
        if s.lower().startswith(".model"):
            parts = s.split()
            if len(parts) >= 2:
                defined_models.add(parts[1].lower())
        # .include/.lib 引入的模型静态检查无法解析，跳过

    # 1. 缺地
    if "0" not in node_count and "gnd" not in {n.lower() for n in node_count}:
        result.fail("网表没有参考地（节点 0）——所有电压/电流都失去参考。")

    # 2. 浮空节点（出现次数 = 1 的节点；地除外）
    for n, c in node_count.items():
        if c < 2 and n not in ("0",) and n.lower() != "gnd":
            result.fail(f"节点 {n} 只被一个元件引用，疑似浮空（悬空节点会导致 singular matrix）。")

    # 3. 缺模型
    for m, lineno in used_models.items():
        if m.lower() not in defined_models:
            result.fail(f"第 {lineno} 行：元件引用了模型 '{m}'，但没有对应的 .model 定义或 .include。")

    return result
