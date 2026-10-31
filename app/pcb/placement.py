"""PCB 初始布局：按信号流次序铺放（方案 §5.3 / §9 P1）。

替代早期网格占位：端口靠边（输入左/输出右），器件按信号传播分层
排列——从输入端口网络 BFS，器件层 = 其所属网络最大层 +1；GND 等
全局网不参与分层（否则所有器件挤在第 1 层）。层内均布。间距沿用
已验证的庭院安全值（横向 14mm、纵向 13mm，DRC 实测无重叠）。

纯确定性：同输入同布局（无随机），与整体"可复现"口径一致。
"""
from __future__ import annotations

X_MARGIN = 9.0        # 端子列中心（板缘内）
DEV_X0 = 20.0         # 第一器件列中心
X_PITCH = 16.0        # 器件列距（横向安全间距；14 曾在桥式整流上撞庭院——
                      # 相邻层列的轴向器件庭院宽 ~13mm，16 实测干净）
Y_PITCH = 13.0        # 器件行距（纵向安全间距）
Y_TOP = 10.0          # 首行中心

_GND_NAMES = {"gnd", "0", "vcc", "vee", "vdd", "vss"}


def _is_global(net: str) -> bool:
    low = net.lower()
    return low in _GND_NAMES or low.startswith(("v+", "v-", "vcc", "vee", "vdd", "vss"))


def signal_layers(components: list[dict], ports: list[str]) -> dict[str, int]:
    """信号流分层：{位号: 层}。层 0 = 输入端口网络。

    min/max 松弛迭代（验收报告指出的静态缺陷修正）：原实现要求器件的全部
    网络先有层号才定层，而新网络只能由已定层器件赋予层号——两者互相等待，
    新网络实际无法扩展，多数器件落入兜底按序摆放。改为迭代至不动点：
    器件层 = 1 + max(其非全局网络层)；非端口网络层 = min(其器件层)。
    单调有界（迭代 ≤ 器件数+1 次），结果与元件顺序无关。
    """
    nets_of: dict[str, set[str]] = {}
    net_devices: dict[str, set[str]] = {}
    for c in components:
        nets_of[c["ref"]] = set()
        for net in c.get("pads", []):
            if _is_global(net):
                continue
            nets_of[c["ref"]].add(net)
            net_devices.setdefault(net, set()).add(c["ref"])

    src_nets = {p for p in ports[: (len(ports) + 1) // 2] if not _is_global(p)}
    if not src_nets:
        cand = sorted(net_devices, key=lambda n: len(net_devices[n]))
        if cand:
            src_nets.add(cand[0])

    net_layer: dict[str, int] = {n: 0 for n in src_nets}
    net_from: dict[str, str] = {}          # 网络 ← 冻结它的器件（自反馈排除用）
    dev_layer: dict[str, int] = {}
    # 波次推进 + 网层冻结：器件层 = 1 + max(已定层网络)，但排除"由该器件自己
    # 冻结的网络"（net→dev→同 net 的二部环反馈会使 max/min 双向松弛振荡发散）；
    # 网络层在被第一个器件触达时冻结。更晚就绪的深源仍会把多输入器件推深。
    for _round in range(len(components) + 2):
        changed = False
        for c in components:
            ref = c["ref"]
            known = [net_layer[n] for n in nets_of[ref]
                     if n in net_layer and net_from.get(n) != ref]
            if not known:
                continue
            want = max(known) + 1
            if want > dev_layer.get(ref, -1):
                dev_layer[ref] = want
                changed = True
        if not changed:
            break
        for c in components:
            ref = c["ref"]
            if ref not in dev_layer:
                continue
            for n in nets_of[ref]:
                if n not in net_layer:  # 冻结：首触即定
                    net_layer[n] = dev_layer[ref]
                    net_from[n] = ref
    # 悬空/纯全局网器件：沉到最后层
    for c in components:
        dev_layer.setdefault(c["ref"], max(dev_layer.values(), default=0) + 1)
    return dev_layer


def place(components: list[dict], ports: list[str]) -> tuple[float, float, dict[str, tuple[float, float]]]:
    """信号流布局。返回 (板宽, 板高, {位号或 __port__i: (x, y) 毫米})。

    components 为板规格条目（含 pads）；端子连接器的位号由 parts 层
    生成，这里以 __port__<序号> 给出坐标槽位（与 ports 同序）。
    """
    dev_layer = signal_layers(components, ports)
    layers: dict[int, list[str]] = {}
    for ref, layer in dev_layer.items():
        layers.setdefault(layer, []).append(ref)
    rows = max((len(refs) for refs in layers.values()), default=1)
    # 高度按【最挤的一层】给足纵向安全间距（庭院 ≥13mm 实测口径）
    height = max(30.0, Y_TOP * 2 + (rows - 1) * Y_PITCH)
    max_layer = max(layers, default=0)
    width = max(46.0, DEV_X0 + (max_layer + 1) * X_PITCH + X_MARGIN + 4.0)
    pos: dict[str, tuple[float, float]] = {}
    for layer, refs in sorted(layers.items()):
        for i, ref in enumerate(sorted(refs)):
            frac = (i / (len(refs) - 1)) if len(refs) > 1 else 0.5
            y = Y_TOP + (height - 2 * Y_TOP) * frac
            pos[ref] = (round(DEV_X0 + layer * X_PITCH, 2), round(y, 2))
    inputs = ports[: (len(ports) + 1) // 2] if len(ports) > 1 else ports[:1]
    for i, p in enumerate(ports):
        left = p in inputs
        n_side = sum(1 for q in ports if (q in inputs) == left)
        idx = sum(1 for q in ports[:i] if (q in inputs) == left)
        y = Y_TOP + (height - 2 * Y_TOP) * (idx / n_side if n_side > 1 else 0.5)
        pos[f"__port__{i}"] = (round(X_MARGIN if left else width - X_MARGIN, 2), round(y, 2))
    return round(width, 1), round(height, 1), pos
