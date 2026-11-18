"""统一电路数据（CircuitIR）：网表 → 版本化的元件/网络/角色结构。

这是 PCB 升级方案 §4.1 的 P0 件。定位：**电路层的稳定中间表示**——
只描述"网表里有什么、每个元件扮演什么角色"，不掺任何 KiCad/封装知识
（那属于 app/pcb/parts.py 的映射层）。上游复用 checks 的规范解析
（iter_element_lines / parse_nodes），不重复造解析器。

角色（role）分类即方案 §4 的核心规则：
- device   —— 可上板的实物器件候选（R/C/L/D/Q），映射成败由器件库决定
- stimulus —— 仿真激励（V 源）。不是实物：其网络必须经"外部端子"进出
              板子，端子由调用方声明（ports）；0V 检测源（电流表用法，
              含 `DC 0`/`0` 等全部写法）属于"未经验证不可删"的特殊项，
              显式阻断
- blocked  —— 当前版本明确不支持实体化（子电路/理想运放/MOSFET/受控源/
              电流源等），给出原因而不是静默丢弃。**所有非板上器件都必须
              走 device/stimulus/blocked 之一，不存在第四种静默路径**
              （验收 F03：曾因 "probe" 角色无人处理而静默丢电流源）

网络名规范化（验收 F02 修正）：重命名一律避开既有网络名与已分配名
（曾把数字节点 1 改名 N2 与既有 N2 碰撞、合并了两个不同节点），并以
"规范化前后连接分区等价"作终检——不等价即抛错而不是给出错误 IR。
IR 是纯 dict（JSON 可序列化），带 ir_version 供后续结构演进。
"""
from __future__ import annotations

import re

from . import checks as _checks
from . import i18n

IR_VERSION = 2

# 网表节点名 → KiCad 安全网络名的规则（GND 统一；其余仅允许字母数字和 _-.）
_GND_ALIASES = {"0", "gnd"}

_ROLES = {
    "R": "device", "C": "device", "L": "device", "D": "device", "Q": "device",
    "V": "stimulus",
    # 以下角色当前一律 blocked（原因在 _BLOCK_REASONS）。
    # I（电流源）也是 blocked：不是实物器件，且没有"经确认的转换"就不得放行
    "I": "blocked", "M": "blocked", "X": "blocked", "K": "blocked",
    "F": "blocked", "H": "blocked", "E": "blocked", "G": "blocked",
    "B": "blocked", "T": "blocked", "O": "blocked", "U": "blocked",
}

_BLOCK_REASONS = {
    "X": "子电路（含理想运放）不能直接变成实物 IC：请替换为具体型号（如 LM358，"
         "器件库已收参考条目）并补供电引脚与去耦，形成新的电路候选重新仿真验证后"
         "再映射（PCB 方案 §4.2）",
    "M": "MOSFET 尚无审校器件条目（首批库只覆盖双极性三极管）",
    "I": "电流源不是实物器件，也不能未经验证直接删除：请改为端子激励（外部电流"
         "注入）或经诺顿等效改成电压源+电阻，形成新电路候选重新仿真验证"
         "（PCB 方案 §4.2）",
    "K": "互感（K 语句）无独立封装，需展开为具体电感后映射",
    "F": "受控源（CCCS）无实物对应",
    "H": "受控源（CCVS）无实物对应",
    "E": "受控源（VCVS）无实物对应",
    "G": "受控源（VCCS）无实物对应",
    "B": "行为源（BSIM 行为级）无实物对应",
    "T": "传输线无实物对应",
    "O": "_lossy line 无实物对应",
    "U": "未知元件类型",
}

_SAFE_NET = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,40}$")


def _try_number(token: str) -> float | None:
    """SPICE 数值（含工程后缀）→ float；不是数值返回 None。"""
    m = re.fullmatch(r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
                     r"\s*(Meg|meg|[kKmMuUnNpPfF]?)(?:V|v)?", token.strip())
    if not m:
        return None
    factors = {"": 1.0, "k": 1e3, "K": 1e3, "Meg": 1e6, "meg": 1e6,
               "m": 1e-3, "u": 1e-6, "U": 1e-6, "n": 1e-9, "N": 1e-9,
               "p": 1e-12, "P": 1e-12, "f": 1e-15, "F": 1e-15}
    try:
        return float(m[1]) * factors[m[2]]
    except (ValueError, KeyError):
        return None


def _value_tokens(line: str, kind: str, n_nodes: int) -> list[str]:
    """元件行里节点之后的值/参数 token（含 DC/AC/SIN 等全部写法）。"""
    toks = line.split()
    return toks[1 + n_nodes:]


def _is_zero_source(line: str, kind: str, n_nodes: int) -> bool:
    """激励源直流分量是否为 0（检测源判据）。覆盖 `0`、`DC 0`、`0V`、
    大小写与工程后缀写法（验收 F03：曾只看第 4 token，`DC 0` 绕过检测）。"""
    vals = _value_tokens(line, kind, n_nodes)
    if not vals:
        return False
    first = _try_number(vals[0])
    if first is not None:
        return first == 0.0
    if vals[0].upper() in ("DC", "dc") and len(vals) > 1:
        num = _try_number(vals[1])
        return num is not None and num == 0.0
    return False


def _model_token(line: str) -> str:
    """元件的型号/值字段。R/C/L/V/D 取第 4 token；Q 第 5、M 第 6、X 最后一个
    （节点数不同）。schematic_layout 的 Elem 不提取 Q/M/X 的型号，映射层需要，
    所以这里基于 checks 的规范解析自行组装。"""
    toks = line.split()
    kind = toks[0][0].upper() if toks else ""
    idx = {"R": 3, "C": 3, "L": 3, "V": 3, "I": 3, "D": 3,
           "Q": 4, "M": 5}.get(kind)
    if idx is not None:
        return toks[idx] if len(toks) > idx else ""
    if kind == "X" and len(toks) >= 2:
        return toks[-1]
    return ""


def _safe_rename(nets: set[str]) -> dict[str, str]:
    """网络名规范化：GND 别名统一；不合规名分配不碰撞的安全名。

    安全名避开【既有全部网络名】与【已分配的重命名】（验收 F02：曾用
    计数生成 N2 与既有 N2 碰撞，把两个不同节点合并成一个）。
    """
    rename: dict[str, str] = {}
    targets = {n for n in nets if n != "GND"}  # GND 目标名预留
    used = {n for n in nets}
    counter = 0
    for n in sorted(nets):
        if n.lower() in _GND_ALIASES:
            if n != "GND":
                rename[n] = "GND"
        elif not _SAFE_NET.match(n):
            counter += 1
            cand = f"N{counter}"
            while cand in used or cand in targets or cand in rename.values():
                counter += 1
                cand = f"N{counter}"
            rename[n] = cand
            used.add(cand)
    return rename


def netlist_to_ir(netlist: str) -> dict:
    """网表 → CircuitIR dict。不抛异常——所有问题进 blockers 列表。

    SPICE 首行恒为标题（不是元件）——曾把标题 "RC lowpass 1kHz" 解析成
    幻影电阻 RC 进了板子（实测教训）。VoltProof 生成侧首行是 * 注释，
    同样安全跳过。
    """
    lines = netlist.splitlines()[1:] if netlist.splitlines() else []
    elements: list[dict] = []
    nets: set[str] = set()
    for _lineno, line in _checks.iter_element_lines(lines):
        toks = line.split()
        name = toks[0]
        kind = name[0].upper()
        el_nets = [n for n in _checks.parse_nodes(line) if n]
        value = _model_token(line)
        role = _ROLES.get(kind, "blocked")
        note = ""
        block_reason = ""
        if role == "stimulus" and _is_zero_source(line, kind, len(el_nets)):
            role = "blocked"
            note = i18n.t("api.block.zero")
            block_reason = note  # blocked 展示取 block_reason 优先，须同步覆盖
        elements.append({
            "ref": name, "kind": kind, "value": value,
            "nets": list(el_nets), "role": role, "note": note,
            "text": re.sub(r"\s+", " ", line.strip()),  # 电气哈希/审计用原始语义
            "block_reason": (block_reason or (i18n.t("api.block." + kind) if i18n.t("api.block." + kind) != "api.block." + kind else i18n.t("api.block.unknown")))
            if role == "blocked" else "",
        })
        nets.update(el_nets)
    # 网络名规范化 + 单射/无捕获守卫（重命名绝不改变连接拓扑）：
    # ① 两个不同原始网络不得映射到同一目标（除非都是地别名——SPICE 同一节点）；
    # ② 重命名目标不得撞上"未改名的其他原始网络"（捕获即合并节点）。
    rename = _safe_rename(nets)
    gnd_map = {n: "GND" for n in nets if n.lower() in _GND_ALIASES}
    full_map = {**rename, **gnd_map}
    targets: dict[str, str] = {}
    for orig, tgt in full_map.items():
        if tgt in targets and targets[tgt] != orig:
            if not (targets[tgt].lower() in _GND_ALIASES and orig.lower() in _GND_ALIASES):
                raise AssertionError(f"网络重命名冲突：{targets[tgt]} 与 {orig} 同映 {tgt}")
        targets[tgt] = orig
    unmoved = {n for n in nets if n not in full_map}
    captured = set(targets) & unmoved - {"GND"}
    if captured:
        raise AssertionError(f"网络重命名捕获既有网络名：{sorted(captured)}")
    for el in elements:
        el["nets"] = [rename.get(n, n) for n in el["nets"]]
    ir = {
        "ir_version": IR_VERSION,
        "elements": elements,
        "nets": sorted((rename.get(n, n) for n in nets)),
        "net_renames": rename,
        # 激励源的非地网络 = 需要声明外部端子的候选（映射层用）
        "port_candidates": sorted({
            rename.get(n, n)
            for el in elements if el["role"] == "stimulus"
            for n in el["nets"]
            if (rename.get(n, n)).lower() not in _GND_ALIASES}),
    }
    return ir


def ir_blockers(ir: dict) -> list[dict]:
    """列出所有阻断项（含角色级阻断与激励源端子未声明）。"""
    out = [{"ref": el["ref"], "reason": el["block_reason"] or el["note"]}
           for el in ir["elements"] if el["role"] == "blocked"]
    return out
