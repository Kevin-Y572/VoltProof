"""KiCad 子进程适配器：造板 / DSN 导出 / SES 回导 / 铺铜 / DRC / 制板导出。

KiCad 定位（Linux 与 Windows 都认）：
环境变量 VOLTPROOF_KICAD（安装根目录，或 kicad-cli 本身）>
PATH 上的 kicad-cli > 常见安装位置。返回 None 表示不可用——PCB 是可选能力，
缺失时调用方应降级提示而不是崩溃（与 ngspice 必需的定位策略不同）。

两种安装树都有效：
- Windows / 便携树：``<根>/bin/kicad-cli.exe`` 与 ``<根>/bin/python.exe``。
- Linux 发行版或前缀安装：``<根>/bin/kicad-cli``（常见根是 ``/usr``），
  pcbnew 由该前缀里的 python3 或系统 ``/usr/bin/python3`` 加载。

显式设置了 VOLTPROOF_KICAD 但目录对不上时，不再回退到别的安装。

关键事实（阶段 A 实测，2026-10-23）：
- kicad-cli 命令面没有 DSN 导出 / SES 导入（pcb 只有 drc/export/import/render/upgrade），
  路由交换必须用带 pcbnew 的 Python 的模块级函数
  ExportSpecctraDSN(board, path) / ImportSpecctraSES(board, path)。
- pcbnew 无法被本进程 import（它是 KiCad 私有构建），所以所有板操作都是
  "生成一段固定形状的 python 脚本 → KiCad 的 python 子进程执行"。
  脚本内容只接受白名单字段（见 _SAFE_* 正则），杜绝注入。
- 铺铜回填同样只有 python API（ZONE_FILLER），CLI 无此命令。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .procutil import no_window_kwargs

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 板规格白名单：位号/网络名/库名/封装名只允许字母数字和 _-. ，
# 防止把用户输入拼进生成的 python 脚本
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
_SAFE_LAYER = re.compile(r"^[FB]\.(?:Cu|Mask|Silk|Paste|Adhes)$|^Edge\.Cuts$")

MM = 1_000_000  # KiCad 内部单位 nm


class SpecError(ValueError):
    """板规格非法（字段不合规、焊盘数与网络表不匹配等）。"""


@dataclass(frozen=True)
class KiCad:
    root: Path
    cli_path: Path
    python_path: Path
    footprints_path: Path
    pythonpath: str = ""

    @property
    def cli(self) -> Path:
        return self.cli_path

    @property
    def python(self) -> Path:
        return self.python_path

    @property
    def footprints(self) -> Path:
        return self.footprints_path

    def subprocess_env(self) -> dict[str, str] | None:
        """pcbnew 不在解释器默认路径上时补 PYTHONPATH。否则沿用父进程环境。"""
        if not self.pythonpath:
            return None
        env = os.environ.copy()
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = self.pythonpath + (os.pathsep + prev if prev else "")
        return env

    def version(self) -> str:
        try:
            out = subprocess.run([str(self.cli), "version"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=30,
                                 env=self.subprocess_env(), **no_window_kwargs())
            if out.returncode == 0:
                return out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return "?"


def find_kicad() -> KiCad | None:
    """定位 KiCad；找不到返回 None（调用方降级）。结果缓存。"""
    return _find_kicad_cached()


def _distro_prefix(root: Path) -> bool:
    """发行版把 kicad-cli 和系统 python3 放在同一前缀里（/usr、/usr/local）。"""
    try:
        resolved = root.resolve()
    except OSError:
        resolved = root
    return resolved in {Path("/usr"), Path("/usr/local"), Path("/")}


def _has_pcbnew(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    if (directory / "pcbnew.py").is_file():
        return True
    return any(directory.glob("_pcbnew*.so")) or any(directory.glob("pcbnew*.so"))


def _pcbnew_dirs(root: Path) -> list[Path]:
    """前缀下可能存放 pcbnew 的目录。发行版常见是 lib/python3/dist-packages。"""
    patterns = [
        root / "lib" / "python3" / "dist-packages",
        root / "lib" / "python3" / "site-packages",
        root / "lib" / "kicad" / "lib" / "python3" / "dist-packages",
        root / "lib" / "kicad" / "lib" / "python3" / "site-packages",
        root / "local" / "lib" / "python3" / "dist-packages",
    ]
    for lib_name in ("lib", "lib64"):
        lib = root / lib_name
        if not lib.is_dir():
            continue
        try:
            children = list(lib.iterdir())
        except OSError:
            continue
        for child in children:
            if child.name.startswith("python3"):
                patterns.append(child / "dist-packages")
                patterns.append(child / "site-packages")
    found: list[Path] = []
    seen: set[Path] = set()
    for directory in patterns:
        try:
            key = directory.resolve()
        except OSError:
            key = directory
        if key in seen:
            continue
        seen.add(key)
        if _has_pcbnew(directory):
            found.append(directory)
    return found


def _cli_path(root: Path) -> Path | None:
    for name in ("kicad-cli.exe", "kicad-cli"):
        candidate = root / "bin" / name
        if candidate.is_file():
            return candidate
    return None


def _python_for(root: Path, pcbnew_dirs: list[Path]) -> Path | None:
    """选择能 import pcbnew 的解释器。

    Windows 便携树用 bin/python.exe。Linux 发行版包把 pcbnew 装进系统
    python3 的 dist-packages，解释器就是该前缀的 bin/python3（/usr/bin/python3）。
    独立前缀若自带 bin/python3，即使 pcbnew 落在解释器自己的库目录里也采用它。
    """
    for name in ("python.exe", "python3.exe"):
        candidate = root / "bin" / name
        if candidate.is_file():
            return candidate
    bundled: list[Path] = []
    for name in ("python3", "python"):
        candidate = root / "bin" / name
        if candidate.is_file():
            bundled.append(candidate)
    if bundled and (pcbnew_dirs or not _distro_prefix(root)):
        return bundled[0]
    if pcbnew_dirs:
        system = Path("/usr/bin/python3")
        if system.is_file():
            return system
    return None


def _footprints_dir(root: Path) -> Path:
    """封装库目录。安装树自带的 share 优先，其次是 KiCad 自己的环境变量。"""
    own = root / "share" / "kicad" / "footprints"
    if own.is_dir():
        return own
    for key in ("KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR",
                "KICAD8_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR"):
        value = os.environ.get(key)
        if value and Path(value).is_dir():
            return Path(value)
    for fallback in (Path("/usr/share/kicad/footprints"),
                     Path("/usr/local/share/kicad/footprints")):
        if fallback.is_dir():
            return fallback
    return own


def _roots_from_spec(spec: Path) -> list[Path]:
    """VOLTPROOF_KICAD 可以是安装根、bin 目录，或 kicad-cli 文件。"""
    if spec.is_file() and spec.name.lower() in {"kicad-cli", "kicad-cli.exe"}:
        return [spec.parent.parent]
    if (spec / "kicad-cli").is_file() or (spec / "kicad-cli.exe").is_file():
        return [spec.parent]
    return [spec]


def _kicad_from_root(root: Path) -> KiCad | None:
    """一个候选根目录能同时找到 cli 和 pcbnew 解释器才算装好了。"""
    try:
        root = root.resolve()
    except OSError:
        return None
    cli = _cli_path(root)
    if cli is None:
        return None
    pcbnew_dirs = _pcbnew_dirs(root)
    python = _python_for(root, pcbnew_dirs)
    if python is None:
        return None
    return KiCad(root=root, cli_path=cli, python_path=python,
                 footprints_path=_footprints_dir(root),
                 pythonpath=os.pathsep.join(str(d) for d in pcbnew_dirs))


def _kicad_candidates() -> list[Path]:
    candidates: list[Path] = []
    for name in ("kicad-cli", "kicad-cli.exe"):
        on_path = shutil.which(name)
        if on_path:
            candidates.append(Path(on_path).resolve().parent.parent)
            break
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        kd = Path(pf) / "KiCad"
        if kd.is_dir():
            for child in sorted(kd.iterdir(), reverse=True):
                candidates.append(child)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            kd_local = Path(local) / "Programs" / "KiCad"
            if kd_local.is_dir():
                for child in sorted(kd_local.iterdir(), reverse=True):
                    candidates.append(child)
    else:
        candidates.extend([Path("/usr"), Path("/usr/local"), Path("/opt/kicad")])
        opt = Path("/opt")
        if opt.is_dir():
            try:
                children = list(opt.iterdir())
            except OSError:
                children = []
            named = [p for p in children if p.name.lower().startswith("kicad")]
            candidates.extend(sorted(named, key=lambda p: p.name, reverse=True))
    candidates.append(_REPO_ROOT / "kicad-x")  # 便携树（7z 解包安装器）
    return candidates


@lru_cache(maxsize=1)
def _find_kicad_cached() -> KiCad | None:
    env = (os.environ.get("VOLTPROOF_KICAD") or "").strip()
    if env:
        for root in _roots_from_spec(Path(env)):
            hit = _kicad_from_root(root)
            if hit is not None:
                return hit
        return None
    seen: set[str] = set()
    for cand in _kicad_candidates():
        try:
            key = str(cand.resolve())
        except OSError:
            key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        hit = _kicad_from_root(cand)
        if hit is not None:
            return hit
    return None


def validate_spec(spec: dict) -> None:
    """校验板规格（build_board 的输入）。非法抛 SpecError。"""
    if not isinstance(spec, dict):
        raise SpecError("板规格必须是对象")
    name = spec.get("name", "")
    if not _SAFE_NAME.match(str(name)):
        raise SpecError(f"板名非法：{name!r}")
    size = spec.get("size_mm")
    if (not isinstance(size, (list, tuple)) or len(size) != 2
            or not all(isinstance(v, (int, float)) and 5 <= v <= 500 for v in size)):
        raise SpecError("size_mm 必须是 [宽, 高]（毫米，5–500）")
    nets = spec.get("nets")
    if not isinstance(nets, list) or not nets or len(nets) > 64:
        raise SpecError("nets 必须是非空数组（≤64）")
    for n in nets:
        if not _SAFE_NAME.match(str(n)):
            raise SpecError(f"网络名非法：{n!r}")
    comps = spec.get("components")
    if not isinstance(comps, list) or not comps or len(comps) > 64:
        raise SpecError("components 必须是非空数组（≤64）")
    for c in comps:
        if not isinstance(c, dict):
            raise SpecError("元件必须是对象")
        for field in ("ref", "lib", "footprint"):
            if not _SAFE_NAME.match(str(c.get(field, ""))):
                raise SpecError(f"元件字段非法：{c.get(field)!r}")
        pads = c.get("pads")
        if c.get("part") == "HOLE_M3":      # 机械安装孔：无电气焊盘
            if pads not in (None, []):
                raise SpecError(f"{c.get('ref')} 是安装孔，不应有焊盘网络")
            continue
        if not isinstance(pads, list) or not pads:
            raise SpecError(f"{c.get('ref')} 的 pads 必须是非空数组")
        for p in pads:
            if p is not None and str(p) not in nets:
                raise SpecError(f"{c.get('ref')} 的焊盘网络 {p!r} 不在 nets 里")
    for k in spec.get("keepouts", []) or []:
        try:
            if float(k["w"]) <= 0 or float(k["h"]) <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise SpecError(f"禁布区参数非法：{k!r}（x/y/w/h 毫米，w/h>0）")
    for z in spec.get("zones", []) or []:
        if not isinstance(z, dict):
            raise SpecError("铺铜区必须是对象")
        if str(z.get("net")) not in nets:
            raise SpecError(f"铺铜网络 {z.get('net')!r} 不在 nets 里")
        if not _SAFE_LAYER.match(str(z.get("layer", ""))):
            raise SpecError(f"铺铜层非法：{z.get('layer')!r}")
        pts = z.get("outline_mm")
        if (not isinstance(pts, list) or len(pts) < 3
                or not all(isinstance(pt, (list, tuple)) and len(pt) == 2 for pt in pts)):
            raise SpecError("铺铜轮廓 outline_mm 至少 3 个 [x, y] 点")


def _run_kicad_python(k: KiCad, script: str, workdir: Path, timeout: int = 120) -> tuple[bool, str]:
    """执行一段 pcbnew 脚本（脚本由本模块内部拼装，字段已过白名单）。"""
    script_path = workdir / "_kicad_op.py"
    script_path.write_text(script, encoding="utf-8")
    try:
        proc = subprocess.run(
            [str(k.python), str(script_path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(workdir), env=k.subprocess_env(), **no_window_kwargs())
    except subprocess.TimeoutExpired:
        return False, f"KiCad python 超时（{timeout}s）"
    except OSError as e:
        return False, f"无法启动 KiCad python：{e}"
    out = (proc.stdout + "\n" + proc.stderr).strip()
    return proc.returncode == 0, out


def _spec_literal(v) -> str:
    """把已过白名单的字段安全地嵌进生成脚本（repr 兜底转义）。"""
    return repr(str(v))


def build_board(k: KiCad, spec: dict, out_pcb: Path, workdir: Path,
                rules: dict | None = None) -> tuple[bool, str]:
    """按规格生成未布线 .kicad_pcb（纯代码造板，无需原理图）。

    rules（F05）：线宽/净距/过孔写入默认网络类——板设计设置是 DSN 导出
    与 DRC 的同一事实源，布线器与检查器从此读到同一套规则，而不是各看各的。
    pads 按【焊盘号 → 网络】赋值（F01：`zip(fp.Pads(), pads)` 依赖封装内部
    遍历序，不与焊盘号绑定曾可致错脚）。
    """
    validate_spec(spec)
    rules = rules or {}
    track_mm = float(rules.get("track_mm", 0.2))
    clearance_mm = float(rules.get("clearance_mm", 0.2))
    via_drill_mm = float(rules.get("via_drill_mm", 0.3))
    via_dia_mm = float(rules.get("via_diameter_mm", 0.0)) or via_drill_mm + 0.3
    lines = [
        "# -*- coding: utf-8 -*-",
        "# VoltProof 生成：从板规格造未布线板（阶段 A 适配器路径）",
        "import os, sys",
        "import pcbnew",
        f"OUT = {str(out_pcb)!r}",
        f"MM = {MM}",
        f"W, H = {float(spec['size_mm'][0])!r}, {float(spec['size_mm'][1])!r}",
        f"FP_LIBS = {str(k.footprints)!r}",
        f"NET_NAMES = {list(spec['nets'])!r}",
        f"RULES = {{'track_nm': {int(track_mm * MM)}, 'clear_nm': {int(clearance_mm * MM)},"
        f" 'via_dia_nm': {int(via_dia_mm * MM)}, 'via_drill_nm': {int(via_drill_mm * MM)},"
        f" 'track_mm': {track_mm!r}, 'clear_mm': {clearance_mm!r}}}",
        "board = pcbnew.BOARD()",
        "x0, y0, x1, y1 = 5*MM, 5*MM, int(W-5)*MM, int(H-5)*MM",
        "for (sx, sy, ex, ey) in [(x0,y0,x1,y0), (x1,y0,x1,y1), (x1,y1,x0,y1), (x0,y1,x0,y0)]:",
        "    edge = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)",
        "    edge.SetStart(pcbnew.VECTOR2I(sx, sy))",
        "    edge.SetEnd(pcbnew.VECTOR2I(ex, ey))",
        '    edge.SetLayer(board.GetLayerID("Edge.Cuts"))',
        "    edge.SetWidth(150000)",
        "    board.Add(edge)",
        "NETS = {}",
        "for n in NET_NAMES:",
        "    item = pcbnew.NETINFO_ITEM(board, n)",
        "    board.Add(item)",
        "    NETS[n] = item",
        'print("BOARD_OK")',
        "def place(rel, fpname, ref, x, y, pads_by_no):",
        '    d = os.path.join(FP_LIBS, rel + ".pretty")',
        "    fp = pcbnew.FootprintLoad(d, fpname)",
        "    if fp is None:",
        '        print("FOOTPRINT_MISSING:", fpname); sys.exit(3)',
        "    fp.SetReference(ref)",
        "    board.Add(fp)",
        "    fp.SetPosition(pcbnew.VECTOR2I(int(x*MM), int(y*MM)))",
        "    pads_obj = fp.Pads()",
        "    by_no = {}",
        "    for pad in pads_obj:",
        "        by_no[str(pad.GetNumber())] = pad",
        "    if set(pads_by_no) - set(by_no):",
        '        print("PAD_NUMBER_MISSING:", ref, sorted(set(pads_by_no) - set(by_no))); sys.exit(3)',
        "    for no, n in pads_by_no.items():",
        "        if n is not None:",
        "            by_no[no].SetNet(NETS[n])",
        '    print("PLACED:", ref, len(pads_obj))',
        "def add_zone(netname, layer, pts_mm):",
        "    zone = pcbnew.ZONE(board)",
        "    zone.SetLayer(board.GetLayerID(layer))",
        "    zone.SetNet(NETS[netname])",
        "    zone.Outline().NewOutline()",
        "    for (px, py) in pts_mm:",
        "        zone.Outline().Append(int(px*MM), int(py*MM))",
        "    board.Add(zone)",
        '    print("ZONE_ADDED:", netname, layer)',
        "def add_keepout(x, y, w, h):",
        "    for layer in ('F.Cu', 'B.Cu'):",
        "        z = pcbnew.ZONE(board)",
        "        z.SetLayer(board.GetLayerID(layer))",
        "        z.SetIsRuleArea(True)",
        "        z.Outline().NewOutline()",
        "        for (px, py) in ((x, y), (x+w, y), (x+w, y+h), (x, y+h)):",
        "            z.Outline().Append(int(px*MM), int(py*MM))",
        "        board.Add(z)",
        '    print("KEEPOUT_ADDED:", x, y, w, h)',
    ]
    for c in spec["components"]:
        # pads 列表按焊盘号升序（_pads_for 契约）→ 显式 {焊盘号: 网络}
        pads_map = {str(i + 1): n for i, n in enumerate(c["pads"])} if c["pads"] else {}
        lines.append("place({!r}, {!r}, {!r}, {!r}, {!r}, {!r})".format(
            str(c["lib"]), str(c["footprint"]), str(c["ref"]),
            float(c["x"]), float(c["y"]), pads_map))
        rot = float(c.get("rot") or 0) % 360
        if rot:
            # 与 move_footprints 同一套角度单位（十分之一度）。0° 不写，避免改动现有脚本。
            lines.append("_fp = [fp for fp in board.GetFootprints() if fp.GetReference()=={!r}][-1]".format(
                str(c["ref"])))
            lines.append("_fp.SetOrientation(pcbnew.EANGLE_T(int(round({}*10))))".format(rot))
    for ko in spec.get("keepouts", []) or []:
        lines.append("add_keepout({!r}, {!r}, {!r}, {!r})".format(
            float(ko["x"]), float(ko["y"]), float(ko["w"]), float(ko["h"])))
    for z in spec.get("zones", []) or []:
        lines.append("add_zone({!r}, {!r}, {!r})".format(
            str(z["net"]), str(z["layer"]),
            [[float(p[0]), float(p[1])] for p in z["outline_mm"]]))
    lines.append('print("SAVED:", pcbnew.SaveBoard(OUT, board))')
    ok, out = _run_kicad_python(k, "\n".join(lines) + "\n", workdir)
    if ok and "SAVED: True" not in out:
        ok = False
    if ok:
        write_project_rules(out_pcb, rules)  # F05：DRC 判据随板落盘
    return ok, out


# KiCad 10 板级约束的唯一持久化位置是伴生 .kicad_pro 的
# board.design_settings.rules（BDS 字段/网络类赋值均不随 SaveBoard 落盘——
# 保留名 Default 被丢弃，m_TrackMinWidth 仅内存生效，均为实测结论）。
# kicad-cli pcb drc 会读取同目录同名工程文件，规则即检查判据。
_RULE_BASE = {
    "allow_blind_buried_vias": False, "allow_microvias": False, "max_error": 0.01,
    "min_connection": 0.0, "min_copper_edge_clearance": 0.01, "min_groove_width": 0.0,
    "min_hole_clearance": 0.0, "min_hole_to_hole": 0.25, "min_resolved_spokes": 2,
    "min_silk_clearance": 0.0, "min_text_height": 0.8, "min_text_thickness": 0.08,
    "solder_mask_to_copper_clearance": 0.0, "use_height_for_length_calcs": True,
}


def write_project_rules(board_path: Path, rules: dict | None) -> Path:
    """把制造规则写入 <板>.kicad_pro 的 design_settings（DRC 同源判据）。
    兜底默认与 schema.default_rules 一致（KiCad 默认布线宽度）。"""
    rules = rules or {}
    track = float(rules.get("track_mm", 0.2))
    clear = float(rules.get("clearance_mm", 0.2))
    via_drill = float(rules.get("via_drill_mm", 0.3))
    via_dia = float(rules.get("via_diameter_mm", 0.0)) or via_drill + 0.3
    pro = board_path.with_suffix(".kicad_pro")
    data: dict = {}
    if pro.is_file():
        try:
            data = json.loads(pro.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    ds = data.setdefault("board", {}).setdefault("design_settings", {})
    # 孔环最小值保持 KiCad 默认 0.05：布线器实际过孔外径不完全受我们控制
    # （DSN 默认值），自设过严（曾按 (外径-孔)/2 算 0.15）会把布线器的
    # 0.55/0.30 过孔（环 0.125）误判为错误——bench 实测教训。
    ds["rules"] = {**_RULE_BASE,
                   "min_track_width": track, "min_clearance": clear,
                   "min_via_diameter": via_dia, "min_through_hole_diameter": via_drill,
                   "min_microvia_diameter": via_dia, "min_microvia_drill": via_drill}
    ds["track_widths"] = [track]
    ds["via_dimensions"] = [{"diameter": via_dia, "drill": via_drill}]
    ds["net_settings"] = _net_settings(rules, track, clear, via_dia, via_drill)
    pro.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return pro


def _netclass_apply_source(rules: dict) -> str:
    """导出 DSN 前尽力把网络类写进已打开的板。API 对不上就跳过，不中断导出。"""
    payload = []
    for item in rules.get("net_classes") or []:
        name = str(item.get("name") or "")
        if not _SAFE_NAME.match(name):
            continue
        nets = [str(n) for n in item.get("nets") or [] if _SAFE_NAME.match(str(n))]
        payload.append({
            "name": name,
            "nets": nets,
            "track_mm": float(item.get("track_mm") or 0),
            "clearance_mm": float(item.get("clearance_mm") or 0),
        })
    if not payload:
        return "print('NETCLASS_SKIPPED', 'none')\n"
    return (
        "try:\n"
        f"    _VP_NC = {payload!r}\n"
        "    _applied = 0\n"
        "    _ds = b.GetDesignSettings()\n"
        "    _ns = getattr(_ds, 'm_NetSettings', None)\n"
        "    for _c in _VP_NC:\n"
        "        _klass = None\n"
        "        if _ns is not None and hasattr(_ns, 'GetNetclass'):\n"
        "            _klass = _ns.GetNetclass(_c['name'])\n"
        "        if _klass is not None and hasattr(_klass, 'SetTrackWidth'):\n"
        "            _klass.SetTrackWidth(int(float(_c['track_mm']) * 1000000))\n"
        "            _applied += 1\n"
        "        if _klass is not None and hasattr(_klass, 'SetClearance'):\n"
        "            _klass.SetClearance(int(float(_c['clearance_mm']) * 1000000))\n"
        "        if _ns is not None and hasattr(_ns, 'SetNetclassAssignment'):\n"
        "            for _n in _c['nets']:\n"
        "                _ns.SetNetclassAssignment(_n, _c['name'])\n"
        "                _applied += 1\n"
        "    print('NETCLASS_APPLIED' if _applied else 'NETCLASS_SKIPPED', _applied)\n"
        "except Exception as _exc:\n"
        "    print('NETCLASS_SKIPPED', type(_exc).__name__)\n"
    )


def _net_settings(rules: dict, track: float, clear: float, via_dia: float, via_drill: float) -> dict:
    """KiCad 工程里的网络类。全局最小线宽/净距仍是 design_settings.rules，不在这里放宽。

    类的线宽和净距只允许大于等于制造底线（调用前已校验）。Freerouting 读的是
    DSN；导出脚本会尝试把同一组数字写进 pcbnew，写不进就记 NETCLASS_SKIPPED，
    不把类宽假装成已经布出来的铜。
    """
    def _klass(name: str, width: float, clearance: float, priority: int) -> dict:
        return {
            "name": name,
            "clearance": clearance,
            "track_width": width,
            "via_diameter": via_dia,
            "via_drill": via_drill,
            "microvia_diameter": via_dia,
            "microvia_drill": via_drill,
            "diff_pair_width": width,
            "diff_pair_gap": clearance,
            "diff_pair_via_gap": clearance,
            "priority": priority,
        }

    classes = [_klass("Default", track, clear, 2147483647)]
    patterns = []
    for item in rules.get("net_classes") or []:
        name = str(item.get("name") or "")
        if not _SAFE_NAME.match(name) or name == "Default":
            continue
        width = max(track, float(item.get("track_mm") or track))
        clearance = max(clear, float(item.get("clearance_mm") or clear))
        classes.append(_klass(name, width, clearance, int(item.get("priority") or 0)))
        for net in item.get("nets") or []:
            if _SAFE_NAME.match(str(net)):
                patterns.append({"pattern": str(net), "netclass": name})
    return {"classes": classes, "netclass_patterns": patterns, "meta": {"version": 3}}


def export_dsn(k: KiCad, pcb: Path, dsn: Path, workdir: Path,
               rules: dict | None = None) -> tuple[bool, str]:
    prelude = _netclass_apply_source(rules or {})
    script = (
        "import pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        + prelude +
        f"print('DSN:', pcbnew.ExportSpecctraDSN(b, {str(dsn)!r}))\n")
    ok, out = _run_kicad_python(k, script, workdir)
    if ok and "DSN: True" not in out:
        ok = False
    return ok, out


def import_ses(k: KiCad, pcb: Path, ses: Path, workdir: Path,
               fill_zones: bool = True) -> tuple[bool, str]:
    """SES 回导进板并保存；fill_zones=True 时顺带回填铺铜（含空区）。"""
    fill = ("filler = pcbnew.ZONE_FILLER(b)\n"
            "print('FILL:', filler.Fill(b.Zones()))\n") if fill_zones else ""
    script = (
        "import pcbnew\n"
        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
        f"print('SES:', pcbnew.ImportSpecctraSES(b, {str(ses)!r}))\n"
        "n_tracks = len(list(b.GetTracks()))\n"
        "print('TRACKS:', n_tracks)\n"
        + fill +
        f"print('SAVED:', pcbnew.SaveBoard({str(pcb)!r}, b))\n")
    ok, out = _run_kicad_python(k, script, workdir)
    if ok and ("SES: True" not in out or "SAVED: True" not in out):
        ok = False
    return ok, out


def drc(k: KiCad, pcb: Path, out_json: Path, timeout: int = 120) -> dict:
    """kicad-cli pcb drc（全严重级——F05：--severity-error 会丢警告，方案 §7
    要求警告逐项保留）。返回 {ok, violations(错误), violations_warning,
    violations_all, unconnected, warnings(明细摘要)}。"""
    proc = subprocess.run(
        [str(k.cli), "pcb", "drc", "--output", str(out_json), "--format", "json",
         str(pcb)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        env=k.subprocess_env(), **no_window_kwargs())
    result = {"ok": proc.returncode == 0, "violations": None, "violations_warning": None,
              "violations_all": None, "unconnected": None, "warning_items": [],
              "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-500:]}
    if out_json.is_file():
        try:
            data = json.loads(out_json.read_text(encoding="utf-8"))
            items = data.get("violations", [])
            errors = [v for v in items if str(v.get("severity", "")).lower() == "error"]
            warns = [v for v in items if str(v.get("severity", "")).lower() != "error"]
            result["violations"] = len(errors)
            result["violations_warning"] = len(warns)
            result["violations_all"] = len(items)
            result["warning_items"] = [
                f"{v.get('type', '?')}: {v.get('description', '')[:120]}" for v in warns[:20]]
            uc = data.get("unconnected_items")  # KiCad 10 schema 为 snake_case
            result["unconnected"] = len(uc) if isinstance(uc, list) else None
        except (json.JSONDecodeError, OSError):
            pass
    return result


def move_footprints(k: KiCad, pcb: Path, moves: dict, workdir: Path) -> tuple[bool, str]:
    """按位号移动/旋转/锁定封装。moves: {ref: {x,y,rot?,lock?}}（毫米/度/布尔）。"""
    lines = [
        "import pcbnew",
        f"b = pcbnew.LoadBoard({str(pcb)!r})",
        "MM = 1000000",
        f"MOVES = {moves!r}",
        "found = 0",
        "for fp in b.GetFootprints():",
        "    m = MOVES.get(fp.GetReference())",
        "    if not m: continue",
        "    fp.SetPosition(pcbnew.VECTOR2I(int(m['x']*MM), int(m['y']*MM)))",
        "    if 'rot' in m:",
        "        fp.SetOrientation(pcbnew.EANGLE_T(int(round(m['rot']*10))))",
        "    if 'lock' in m:",
        "        fp.SetLocked(bool(m['lock']))",
        "    found += 1",
        'print("MOVED:", found, "of", len(MOVES))',
        f"print('SAVED:', pcbnew.SaveBoard({str(pcb)!r}, b))",
    ]
    ok, out = _run_kicad_python(k, "\n".join(lines) + "\n", workdir)
    if ok and ("SAVED: True" not in out or "MOVED: 0" in out and moves):
        ok = False
    return ok, out


def export_fab(k: KiCad, pcb: Path, out_dir: Path, timeout: int = 120) -> tuple[bool, str]:
    """Gerber 全套 + 钻孔导出到 out_dir。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    outs = ""
    oks = []
    for args in (["pcb", "export", "gerbers", "--output", str(out_dir), str(pcb)],
                 ["pcb", "export", "drill", "--output", str(out_dir), str(pcb)]):
        try:
            proc = subprocess.run([str(k.cli), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, env=k.subprocess_env(), **no_window_kwargs())
            oks.append(proc.returncode == 0)
            outs += proc.stdout[-500:] + proc.stderr[-200:]
        except (subprocess.TimeoutExpired, OSError) as e:
            oks.append(False)
            outs += str(e)
    return all(oks), outs


# ---------------------------------------------------------------------------
# 板预览：解析 .kicad_pcb 提取板框/焊盘/走线/过孔（纯文本，不依赖 KiCad）
# ---------------------------------------------------------------------------

def _sexp_parse(text: str):
    """极简 s-expression 解析：返回嵌套 list，原子为 str。只服务预览提取。"""
    tokens = re.findall(r'"[^"]*"|[()]|[^\s()]+', text)
    stack: list[list] = [[]]
    for tok in tokens:
        if tok == "(":
            new: list = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ")":
            if len(stack) > 1:
                stack.pop()
        else:
            stack[-1].append(tok.strip('"'))
    return stack[0]


def _find_all(node, head: str):
    for item in node:
        if isinstance(item, list) and item and item[0] == head:
            yield item


def _child(node, head: str, default=None):
    for item in node:
        if isinstance(item, list) and item and item[0] == head:
            return item
    return default


def _fnum(node, head: str, idx: int) -> float:
    child = _child(node, head)
    if child and len(child) > idx:
        try:
            return float(child[1 + idx])
        except ValueError:
            pass
    return 0.0


def preview_from_pcb(pcb_path: Path) -> dict | None:
    """归档板文件 → 预览数据（毫米坐标）：outline 多边形、焊盘、走线、过孔、板尺寸。
    铺铜区不渲染（前端以半透明底色示意 B 层即可）。解析失败返回 None。"""
    try:
        tree = _sexp_parse(pcb_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    # 解析器产出 [ [kicad_pcb ...] ]：剥掉最外包装，取 kicad_pcb 根
    if len(tree) == 1 and isinstance(tree[0], list):
        tree = tree[0]
    outline_pts: list[tuple[float, float]] = []
    xs: list[float] = []
    ys: list[float] = []
    origins: dict[str, dict] = {}   # 位号 → 封装原点（F07 拖拽与对位用）
    for gr in _find_all(tree, "gr_line"):
        layers = _child(gr, "layer") or []
        if len(layers) > 1 and layers[1] != "Edge.Cuts":
            continue
        x1, y1 = _fnum(gr, "start", 0), _fnum(gr, "start", 1)
        x2, y2 = _fnum(gr, "end", 0), _fnum(gr, "end", 1)
        outline_pts.extend([(x1, y1), (x2, y2)])
        xs += [x1, x2]
        ys += [y1, y2]
    pads: list[dict] = []
    for fp in _find_all(tree, "footprint"):
        at = _child(fp, "at") or ["at", "0", "0", "0"]
        try:
            fx, fy, frot = float(at[1]), float(at[2]), float(at[3]) if len(at) > 3 else 0.0
        except (ValueError, IndexError):
            continue
        ref = ""
        # KiCad 10：SES 回导保存后位号写法从 (fp_text reference "X") 变为
        # (property "Reference" "X")——两种都要认（实测教训）
        for fpt in _find_all(fp, "fp_text"):
            if len(fpt) > 2 and fpt[1] == "reference":
                ref = fpt[2]
                break
        if not ref:
            for prop in _find_all(fp, "property"):
                if len(prop) > 2 and prop[1] == "Reference":
                    ref = prop[2]
                    break
        origins[ref] = {"x": round(fx, 3), "y": round(fy, 3), "rot": round(frot, 1)}
        for pad in _find_all(fp, "pad"):
            pat = _child(pad, "at") or ["at", "0", "0", "0"]
            size = _child(pad, "size") or ["size", "1", "1"]
            pad_no = str(pad[1]) if len(pad) > 1 else ""   # (pad "1" ...) 的名字即焊盘号
            try:
                px, py = float(pat[1]), float(pat[2])
                prot = float(pat[3]) if len(pat) > 3 else 0.0
                w, h = float(size[1]), float(size[2])
            except (ValueError, IndexError):
                continue
            # 焊盘坐标是封装内相对坐标 → 板坐标（footprint 旋转，KiCad 逆时针）
            import math
            r = math.radians(frot)
            bx = fx + px * math.cos(r) - py * math.sin(r)
            by = fy + px * math.sin(r) + py * math.cos(r)
            net_name = ""
            if (net_node := _child(pad, "net")) is not None:
                # 两种写法：造板期 (net 3 "IN")；SES 回导保存后 (net "IN")
                if len(net_node) > 2:
                    net_name = net_node[2]
                elif len(net_node) == 2:
                    net_name = net_node[1]
            pads.append({"x": round(bx, 3), "y": round(by, 3), "w": round(w, 3),
                         "h": round(h, 3), "rot": round(frot + prot, 1),
                         "net": net_name, "ref": ref, "pad_no": pad_no,
                         # 封装内相对坐标（拖拽按统一位移重算用，F07）
                         "lx": round(px, 3), "ly": round(py, 3)})
            xs += [bx - w, bx + w]
            ys += [by - h, by + h]
    tracks: list[dict] = []
    for seg in _find_all(tree, "segment"):
        layers = _child(seg, "layer") or []
        tracks.append({"x1": _fnum(seg, "start", 0), "y1": _fnum(seg, "start", 1),
                       "x2": _fnum(seg, "end", 0), "y2": _fnum(seg, "end", 1),
                       "w": _fnum(seg, "width", 0),
                       "layer": layers[1] if len(layers) > 1 else "?"})
        xs += [tracks[-1]["x1"], tracks[-1]["x2"]]
        ys += [tracks[-1]["y1"], tracks[-1]["y2"]]
    vias: list[dict] = []
    for via in _find_all(tree, "via"):
        size = _child(via, "size") or ["size", "0.8"]
        vias.append({"x": _fnum(via, "at", 0), "y": _fnum(via, "at", 1),
                     "d": float(size[1]) if len(size) > 1 else 0.8})
        xs += [vias[-1]["x"], vias[-1]["y"]]
    if not xs:
        return None
    return {"outline": [[round(x, 3), round(y, 3)] for x, y in outline_pts],
            "pads": pads, "tracks": tracks, "vias": vias, "origins": origins,
            "bbox": [min(xs), min(ys), max(xs), max(ys)]}
