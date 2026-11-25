"""PCB 适配层测试：离线单元（日志解析/规格校验/工具探测）+ 工具在场时的集成冒烟。

离线部分不需要任何外部工具；集成部分在 KiCad/Freerouting 缺失时跳过
（PCB 是可选能力，退出码 0——与 ngspice 必需语义不同）。

运行：python tests/test_pcb.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("VOLTPROOF_SETTINGS", str(
    Path(__file__).resolve().parent / f"_llm_settings_test_{os.getpid()}.json"))

from app.pcb import api as api_mod  # noqa: E402
from app.pcb import connectivity, jobs, kicad_backend, parts, placement, router_backend, schema, versions  # noqa: E402
from app.pcb.procutil import no_window_kwargs  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from app.circuit_ir import netlist_to_ir  # noqa: E402
from app.workspace import Workspace  # noqa: E402

FAILURES: list[str] = []
SKIPPED: list[str] = []


def skip(name: str) -> None:
    """工具缺失等跳过项——与 PASS 严格区分（验收报告：曾把跳过打成 PASS）。"""
    print(f"  [SKIP] {name}")
    SKIPPED.append(name)

# —— 真实日志样本（2026-10-23 阶段 A 实测产物节选）——
LOG_COMPLETED = """2026-10-23 13:10:37.985 INFO   [EBF5BA\\254407] Auto-routing pass #2 on board 'b9344' was completed in 0.00 seconds with score 1000.00 (0 unrouted and 0 violations).
2026-10-23 13:10:37.996 INFO   [EBF5BA\\254407] Auto-routing stage completed: started with 5 unrouted nets, completed in 0.19 seconds, final score: 1000.00 (0 unrouted and 0 violations), using 0.00 total CPU seconds.
2026-10-23 13:10:38.004 INFO   [EBF5BA\\254407] Job 'EBF5BA\\254407' finished with state: COMPLETED (elapsed: 0.24 seconds, finished at UTC: 2026-10-23T05:10:38.004684800Z).
"""
LOG_TIMEOUT = """2026-10-23 12:15:53 INFO Auto-routing pass #2 on board '5fea' was completed in 19.61 seconds with score 685.59 (108 unrouted and 101 violations).
2026-10-23 12:16:40 INFO Job 'F7F' finished with state: TIMED_OUT (elapsed: 300.0 seconds).
"""
LOG_CRASH = ("Exception in thread \"Thread-1\" com.oracle.svm.core.jdk.UnsupportedFeatureError: "
             "SerializationConstructorAccessor class not found")


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if not cond and detail else ""))
    if not cond:
        FAILURES.append(name)


def test_parse_log() -> None:
    print("日志解析（成败判定不依赖退出码的根基）")
    r = router_backend.parse_route_log(LOG_COMPLETED)
    check("COMPLETED 状态解析", r["state"] == "COMPLETED")
    check("final 行优先：unrouted=0", r["unrouted"] == 0 and r["violations"] == 0)
    check("score=1000.0", r["score"] == 1000.0)
    check("无崩溃标记", not r["crash"])

    r2 = router_backend.parse_route_log(LOG_TIMEOUT)
    check("TIMED_OUT 状态解析", r2["state"] == "TIMED_OUT")
    check("无 final 行时回退 pass 行：unrouted=108", r2["unrouted"] == 108)
    check("pass 行 score=685.59", r2["score"] == 685.59)

    r3 = router_backend.parse_route_log(LOG_CRASH)
    check("崩溃标记识别（原生 CLI 缺陷防御）", r3["crash"])

    r4 = router_backend.parse_route_log("")
    check("空日志不崩、全空结果",
          r4 == {"state": None, "unrouted": None, "violations": None,
                 "score": None, "crash": False})


GOOD_SPEC = {
    "name": "rc", "size_mm": [50, 30], "nets": ["GND", "IN"],
    "components": [
        {"ref": "J1", "lib": "Connector_PinHeader_2.54mm",
         "footprint": "PinHeader_1x02_P2.54mm_Vertical", "x": 10, "y": 15,
         "pads": ["IN", "GND"]}],
}


def test_spec_validation() -> None:
    print("板规格校验（生成脚本的白名单防线）")
    kicad_backend.validate_spec(GOOD_SPEC)  # 不抛即过
    check("合法规格通过", True)

    def expect_bad(label: str, mutate):
        spec = json_copy(GOOD_SPEC)
        mutate(spec)
        try:
            kicad_backend.validate_spec(spec)
            check(label, False, "未拒绝")
        except kicad_backend.SpecError:
            check(label, True)

    def json_copy(d):
        import json as _j
        return _j.loads(_j.dumps(d))

    expect_bad("位号注入（引号）被拒", lambda s: s["components"][0].__setitem__("ref", 'J1"; import os'))
    expect_bad("网络名带路径分隔符被拒", lambda s: s.__setitem__("nets", ["GND", "IN/../../etc"]))
    expect_bad("焊盘网络不在 nets 被拒", lambda s: s["components"][0].__setitem__("pads", ["IN", "NOPE"]))
    expect_bad("尺寸越界被拒", lambda s: s.__setitem__("size_mm", [20, 9999]))
    expect_bad("铺铜轮廓点数不足被拒", lambda s: s.__setitem__(
        "zones", [{"net": "GND", "layer": "B.Cu", "outline_mm": [[1, 1], [2, 2]]}]))


def test_finders_missing() -> None:
    print("工具探测：缺失时返回 None（不抛异常）")
    old_k, old_f = os.environ.pop("VOLTPROOF_KICAD", None), os.environ.pop("VOLTPROOF_FREEROUTING", None)
    kicad_backend._find_kicad_cached.cache_clear()
    router_backend._find_freerouting_cached.cache_clear()
    try:
        # 本机可能真装了工具——只验证"不抛异常、类型正确"
        k = kicad_backend.find_kicad()
        f = router_backend.find_freerouting()
        check("find_kicad 返回 KiCad|None", k is None or isinstance(k, kicad_backend.KiCad))
        check("find_freerouting 返回 Path|None", f is None or isinstance(f, Path))
    finally:
        if old_k:
            os.environ["VOLTPROOF_KICAD"] = old_k
        if old_f:
            os.environ["VOLTPROOF_FREEROUTING"] = old_f
        kicad_backend._find_kicad_cached.cache_clear()
        router_backend._find_freerouting_cached.cache_clear()


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    kicad_backend._find_kicad_cached.cache_clear()
    router_backend._find_freerouting_cached.cache_clear()
    router_backend._find_java_cached.cache_clear()


def _write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def test_linux_discovery_and_launch() -> None:
    """不依赖真实 KiCad/Java/Freerouting：Linux 安装树、环境变量覆盖、启动标志。"""
    print("Linux 工具发现与跨平台启动标志（无需真实 KiCad/Java/Freerouting）")
    keys = ("VOLTPROOF_KICAD", "VOLTPROOF_FREEROUTING", "VOLTPROOF_JAVA",
            "XDG_DATA_HOME", "PATH", "KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR",
            "KICAD8_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR")
    saved = {key: os.environ.get(key) for key in keys}
    try:
        kw = no_window_kwargs()
        if sys.platform == "win32":
            check("Windows 保留隐藏控制台标志",
                  kw.get("creationflags") == subprocess.CREATE_NO_WINDOW)
        else:
            check("非 Windows 不传 creationflags", "creationflags" not in kw)
        launched = subprocess.run(
            [sys.executable, "-c", "print('vp-ok')"],
            capture_output=True, text=True, timeout=30, **kw)
        check("启动标志不会让子进程崩溃",
              launched.returncode == 0 and "vp-ok" in (launched.stdout or ""),
              (launched.stderr or "")[:200])

        with tempfile.TemporaryDirectory(prefix="vp_kicad_disc_") as td:
            root = Path(td)
            win = root / "win"
            (win / "bin").mkdir(parents=True)
            (win / "bin" / "kicad-cli.exe").write_text("", encoding="utf-8")
            (win / "bin" / "python.exe").write_text("", encoding="utf-8")
            (win / "share" / "kicad" / "footprints").mkdir(parents=True)
            os.environ["VOLTPROOF_KICAD"] = str(win)
            kicad_backend._find_kicad_cached.cache_clear()
            found = kicad_backend.find_kicad()
            check("Windows 布局：kicad-cli.exe + python.exe",
                  found is not None and found.cli.name == "kicad-cli.exe"
                  and found.python.name == "python.exe",
                  str(found.cli) if found else "None")

            linux = root / "prefix"
            _write_exec(linux / "bin" / "kicad-cli", "#!/bin/sh\necho 10.0.8\n")
            _write_exec(linux / "bin" / "python3", "#!/bin/sh\nexit 0\n")
            (linux / "share" / "kicad" / "footprints").mkdir(parents=True)
            os.environ["VOLTPROOF_KICAD"] = str(linux)
            kicad_backend._find_kicad_cached.cache_clear()
            found = kicad_backend.find_kicad()
            check("Linux 前缀：bin/kicad-cli 与 bin/python3",
                  found is not None and found.cli == (linux / "bin" / "kicad-cli").resolve()
                  and found.python == (linux / "bin" / "python3").resolve()
                  and found.footprints == (linux / "share" / "kicad" / "footprints").resolve(),
                  "" if found is None else f"cli={found.cli} py={found.python}")
            ver = found.version() if found else ""
            check("kicad-cli version 在当前平台可启动", "10.0.8" in ver, ver)

            distro = root / "distro"
            _write_exec(distro / "bin" / "kicad-cli", "#!/bin/sh\necho distro\n")
            pcbnew = distro / "lib" / "python3" / "dist-packages"
            pcbnew.mkdir(parents=True)
            (pcbnew / "pcbnew.py").write_text("# fake pcbnew\n", encoding="utf-8")
            os.environ["VOLTPROOF_KICAD"] = str(distro)
            kicad_backend._find_kicad_cached.cache_clear()
            found = kicad_backend.find_kicad()
            system_py = Path("/usr/bin/python3")
            if system_py.is_file():
                check("发行版布局：pcbnew.py 配对 /usr/bin/python3",
                      found is not None and found.python.resolve() == system_py.resolve()
                      and "dist-packages" in found.pythonpath,
                      "" if found is None else f"py={found.python} path={found.pythonpath}")
            else:
                check("没有 /usr/bin/python3 时不把残缺发行版树当成 KiCad", found is None)

            os.environ["VOLTPROOF_KICAD"] = str(root / "missing-root")
            os.environ["PATH"] = str(linux / "bin") + os.pathsep + (saved.get("PATH") or "")
            kicad_backend._find_kicad_cached.cache_clear()
            check("无效的 VOLTPROOF_KICAD 不回退到 PATH",
                  kicad_backend.find_kicad() is None)

            os.environ.pop("VOLTPROOF_KICAD", None)
            kicad_backend._find_kicad_cached.cache_clear()
            found = kicad_backend.find_kicad()
            check("PATH 上的 kicad-cli 能定位 Linux 前缀",
                  found is not None and found.root == linux.resolve(),
                  "" if found is None else str(found.root))

            os.environ["VOLTPROOF_KICAD"] = str(win)
            kicad_backend._find_kicad_cached.cache_clear()
            found = kicad_backend.find_kicad()
            check("VOLTPROOF_KICAD 优先于 PATH",
                  found is not None and found.cli.name == "kicad-cli.exe")

            os.environ["VOLTPROOF_KICAD"] = str(linux / "bin" / "kicad-cli")
            kicad_backend._find_kicad_cached.cache_clear()
            found = kicad_backend.find_kicad()
            check("VOLTPROOF_KICAD 也可以直接指向 kicad-cli",
                  found is not None and found.root == linux.resolve())

            bare = kicad_backend._kicad_from_root(Path("/usr"))
            if (Path("/usr/bin/kicad-cli")).is_file():
                check("已安装的 /usr/bin/kicad-cli 可识别", bare is not None)
            else:
                check("只有系统 python3、没有 kicad-cli 时 /usr 不是 KiCad", bare is None)

            jar = root / "freerouting-2.1.0.jar"
            jar.write_bytes(b"not-a-real-jar")
            os.environ["VOLTPROOF_FREEROUTING"] = str(jar)
            router_backend._find_freerouting_cached.cache_clear()
            check("VOLTPROOF_FREEROUTING 覆盖生效",
                  router_backend.find_freerouting() == jar.resolve())
            xdg = root / "xdg"
            (xdg / "voltproof").mkdir(parents=True)
            xdg_jar = xdg / "voltproof" / "freerouting-9.9.9.jar"
            xdg_jar.write_bytes(b"jar")
            os.environ["XDG_DATA_HOME"] = str(xdg)
            os.environ["VOLTPROOF_FREEROUTING"] = str(root / "no-such.jar")
            router_backend._find_freerouting_cached.cache_clear()
            check("无效的 VOLTPROOF_FREEROUTING 不回退到其它 JAR",
                  router_backend.find_freerouting() is None)
            os.environ.pop("VOLTPROOF_FREEROUTING", None)
            router_backend._find_freerouting_cached.cache_clear()
            check("XDG 数据目录里的 freerouting-*.jar 可被发现",
                  router_backend.find_freerouting() == xdg_jar.resolve())

            java21 = root / "java21"
            _write_exec(java21, "#!/bin/sh\necho 'openjdk version \"21.0.4\"' >&2\n")
            java17 = root / "java17"
            _write_exec(java17, "#!/bin/sh\necho 'openjdk version \"17.0.13\"' >&2\n")
            os.environ["VOLTPROOF_JAVA"] = str(java21)
            router_backend._find_java_cached.cache_clear()
            got = router_backend.find_java()
            check("VOLTPROOF_JAVA 覆盖生效", got == java21.resolve())
            check("Java 21 不构成阻断",
                  router_backend.java_blocker(got) is None)
            os.environ["VOLTPROOF_JAVA"] = str(java17)
            router_backend._find_java_cached.cache_clear()
            got = router_backend.find_java()
            blocked = router_backend.java_blocker(got)
            check("Java 17 被拒绝并写明需要 21",
                  got == java17.resolve() and blocked is not None
                  and "Java" in blocked and "21" in blocked,
                  blocked or "")
            os.environ["VOLTPROOF_JAVA"] = str(root / "missing-java")
            router_backend._find_java_cached.cache_clear()
            check("无效的 VOLTPROOF_JAVA 不回退到系统 java",
                  router_backend.find_java() is None)

            fake_java = root / "fake-java"
            _write_exec(fake_java,
                        "#!/bin/sh\n"
                        "echo \"Job 'x' finished with state: COMPLETED\"\n"
                        "echo 'final score: 1000.00 (0 unrouted and 0 violations)'\n")
            os.environ["VOLTPROOF_JAVA"] = str(fake_java)
            work = root / "route"
            work.mkdir()
            try:
                routed = router_backend.route(
                    jar, root / "b.dsn", work / "b.ses", work, timeout_s=30)
                check("Freerouting 启动参数在当前平台不崩",
                      routed.ses_written is False and "SES" in " ".join(routed.problems),
                      routed.summary + " " + "; ".join(routed.problems))
            except router_backend.ToolError as exc:
                check("Freerouting 启动参数在当前平台不崩", False, str(exc))

        from types import SimpleNamespace
        from unittest.mock import patch
        rc = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 1.59k",
                        "C1 out 0 100n", ".end"])
        with tempfile.TemporaryDirectory(prefix="vp_java_job_") as td:
            with patch.object(kicad_backend, "find_kicad", return_value=object()), \
                 patch.object(router_backend, "find_freerouting",
                              return_value=Path("freerouting-2.1.0.jar")), \
                 patch.object(router_backend, "find_java", return_value=None):
                failed = jobs.run_pcb_job(rc, ["in", "out"], Path(td) / "job")
            names = [s["stage"] for s in failed["stages"]]
            check("缺 Java 在进入造板/布线前失败",
                  failed["ok"] is False and "tools" in names
                  and "board" not in names and "routing" not in names
                  and any("Java" in b for b in failed["blockers"]),
                  str(failed["stages"]) + str(failed["blockers"]))
    finally:
        _restore_env(saved)


def test_integration_smoke() -> None:
    print("集成链路（KiCad + Freerouting 在场才跑，否则跳过）")
    kicad = kicad_backend.find_kicad()
    jar = router_backend.find_freerouting()
    if kicad is None or jar is None or router_backend.java_blocker(router_backend.find_java()):
        skip("工具缺失 → 跳过集成（PCB 为可选能力）")
        return
    with tempfile.TemporaryDirectory(prefix="vp_pcb_test_") as td:
        tdir = Path(td)
        pcb = tdir / "t.kicad_pcb"
        dsn = tdir / "t.dsn"
        ses = tdir / "t.ses"
        ok, out = kicad_backend.build_board(kicad, GOOD_SPEC, pcb, tdir)
        check("造板", ok, out[:300])
        if not ok:
            return
        ok, out = kicad_backend.export_dsn(kicad, pcb, dsn, tdir)
        check("DSN 导出", ok, out[:300])
        rr = router_backend.route(jar, dsn, ses, tdir / "rt", max_passes=5, timeout_s=120)
        check("布线（COMPLETED+0未布+SES）", rr.ok, rr.summary + "; " + "; ".join(rr.problems))
        if not rr.ok:
            return
        ok, out = kicad_backend.import_ses(kicad, pcb, ses, tdir, fill_zones=True)
        check("SES 回导+保存", ok, out[:300])
        res = kicad_backend.drc(kicad, pcb, tdir / "drc.json")
        check("DRC 0 违规 0 未连",
              res["ok"] and res["violations"] == 0 and res["unconnected"] == 0,
              str(res)[:300])


def test_circuit_ir_and_mapping() -> None:
    print("CircuitIR + 审校器件库映射（阶段 B P0）")
    rc = "\n".join([
        "RC lowpass 1kHz",
        "Vin in 0 SIN(0 1 1k)",
        "R1 in out 1.59k",
        "C1 out 0 100n",
        ".end",
    ])
    ir = netlist_to_ir(rc)
    kinds = {el["ref"]: el["role"] for el in ir["elements"]}
    check("角色分类：V=stimulus R/C=device",
          kinds.get("Vin") == "stimulus" and kinds.get("R1") == "device")
    check("节点 0 → GND 规范化", "GND" in ir["nets"] and ir["net_renames"].get("0") == "GND")
    check("激励端口候选=非地网络", ir["port_candidates"] == ["in"])

    r = parts.map_netlist(rc, ports=["in", "out"])
    check("RC 映射通过（含 OUT 端口）", r["ok"],
          "; ".join(b["reason"] for b in r["report"]["blocked"]))
    if r["ok"]:
        spec = r["spec"]
        comps = {c["ref"]: c for c in spec["components"]}
        check("元件恰为 R1/C1/J1/J2（无幻影元件）",
              set(comps) == {"R1", "C1", "J1", "J2"}, str(set(comps)))
        check("C1=100n → 无极性瓷片（<1uF）",
              comps["C1"]["footprint"].startswith("C_Disc"))
        check("端子 pads=[信号,GND]", comps["J1"]["pads"][1] == "GND"
              and comps["J1"]["pads"][0] == "in")
        check("默认 GND 铺铜在 B.Cu",
              spec["zones"][0]["net"] == "GND" and spec["zones"][0]["layer"] == "B.Cu")
        rep = {m["ref"]: m for m in r["report"]["mapped"]}
        check("映射报告含引脚表（pad→功能→网络）",
              set(rep["R1"]["pin_map"]) == {"1", "2"})

    # 极性与引脚序的组合（审校库的核心价值）
    led = parts.map_netlist("led deck\nD1 in gnd LED\n.end", ports=["in"])
    d1 = next(m for m in led["report"]["mapped"] if m["ref"] == "D1")
    # SPICE D 引脚序 A,K；KiCad LED pad1=K,pad2=A → 焊盘网络应反序
    check("LED 极性合成：pad1(K)=gnd pad2(A)=in",
          d1["pin_map"]["1"]["net"] == "GND" and d1["pin_map"]["2"]["net"] == "in",
          str(d1["pin_map"]))

    qnpn = parts.map_netlist("q deck\nQ1 vcc base gnd QNPN\nR1 vcc base 10k\n.end", ports=[])
    q1 = next(m for m in qnpn["report"]["mapped"] if m["ref"] == "Q1")
    # 网表 Q 序 C,B,E；TO-92 pad1=E,2=B,3=C → 焊盘网络 [gnd,base,vcc]→GND
    qm = {p: d["net"] for p, d in q1["pin_map"].items()}
    check("QNPN→2N3904 脚位合成：pad1=E(GND) pad3=C(vcc)",
          qm == {"1": "GND", "2": "base", "3": "vcc"}, str(qm))
    check("教学模型映射有核对提示",
          any("额定参数" in n for n in q1["notes"]))

    big_c = parts.map_netlist("c deck\nC1 a b 100u\n.end", ports=[])
    c1 = next(m for m in big_c["report"]["mapped"] if m["ref"] == "C1")
    check("100u → 电解电容（≥1uF）且有极性提示",
          c1["part"] == "C_THT_RADIAL_5MM"
          and any("极性" in n for n in c1["notes"]))

    # 阻断语义（全部显式、不静默）
    opamp = parts.map_netlist("opamp deck\nXU1 in fb out OPAMP\n.end", ports=["in"])
    check("理想运放被阻断并给方案 §4.2 原因",
          not opamp["ok"] and "理想运放" in opamp["report"]["blocked"][0]["reason"],
          str(opamp["report"]["blocked"]))

    no_port = parts.map_netlist(rc, ports=[])
    check("激励源未声明端子被阻断",
          not no_port["ok"] and any("外部端子" in b["reason"]
                                    for b in no_port["report"]["blocked"]))

    zero_v = netlist_to_ir("zero-v deck\nV1 a b 0\n.end")
    zv = next(el for el in zero_v["elements"] if el["ref"] == "V1")
    check("0V 检测源被阻断（不可静默删）",
          zv["role"] == "blocked" and "0V" in zv["note"])

    unknown = parts.map_netlist("d deck\nD1 a b MYSPECIAL\n.end", ports=[])
    check("未审校型号不按类别猜测",
          not unknown["ok"] and any("不按类别猜测" in b["reason"]
                                    for b in unknown["report"]["blocked"]))

    mos = netlist_to_ir("m deck\nM1 d g s s NMOD\n.end")
    check("MOSFET 无条目 → 阻断", mos["elements"][0]["role"] == "blocked")


def test_integration_netlist_to_fab() -> None:
    print("集成：网表 → 板 → DSN → 布线 → 回导 → DRC（工具在场才跑）")
    kicad = kicad_backend.find_kicad()
    jar = router_backend.find_freerouting()
    if kicad is None or jar is None or router_backend.java_blocker(router_backend.find_java()):
        skip("工具缺失 → 跳过（PCB 为可选能力）")
        return
    rc = "\n".join([
        "RC lowpass 1kHz",
        "Vin in 0 SIN(0 1 1k)",
        "R1 in out 1.59k",
        "C1 out 0 100n",
        ".end",
    ])
    r = parts.map_netlist(rc, ports=["in", "out"])
    check("网表映射出板规格", r["ok"],
          "; ".join(b["reason"] for b in r["report"]["blocked"]))
    if not r["ok"]:
        return
    with tempfile.TemporaryDirectory(prefix="vp_pcb_map_") as td:
        tdir = Path(td)
        pcb = tdir / "b.kicad_pcb"
        dsn = tdir / "b.dsn"
        ses = tdir / "b.ses"
        ok, out = kicad_backend.build_board(kicad, r["spec"], pcb, tdir)
        check("网表板造板", ok, out[:300])
        if not ok:
            return
        ok, out = kicad_backend.export_dsn(kicad, pcb, dsn, tdir)
        check("DSN 导出", ok, out[:200])
        rr = router_backend.route(jar, dsn, ses, tdir / "rt", max_passes=5, timeout_s=120)
        check("布线", rr.ok, rr.summary + "; " + "; ".join(rr.problems))
        if not rr.ok:
            return
        ok, out = kicad_backend.import_ses(kicad, pcb, ses, tdir, fill_zones=True)
        check("回导+铺铜", ok, out[:200])
        res = kicad_backend.drc(kicad, pcb, tdir / "drc.json")
        check("DRC 干净", res["ok"] and res["violations"] == 0 and res["unconnected"] == 0,
              str(res)[:300])


def test_schema_and_versions() -> None:
    print("版本模型：三类哈希 / 候选-采用-冲突 / 过期判定（方案 §6）")
    rc = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 1.59k",
                    "C1 out 0 100n", ".end"])
    rc_comments_only = "\n".join(["* 标题与注释改变", "Vin in 0 SIN(0 1 1k)",
                                  "R1 in out 1.59k", "C1 out 0 100n",
                                  ".ac dec 10 1 100k", ".end"])
    rc_stimulus_changed = "\n".join(["RC deck", "Vin in 0 SIN(0 2 2k)", "R1 in out 1.59k",
                                     "C1 out 0 100n", ".end"])
    rc_changed = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 10k",
                            "C1 out 0 100n", ".end"])
    rc_model_changed = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 1.59k",
                                  "C1 out 0 100n", ".model foo D(Is=1n)", ".end"])
    h1 = schema.electrical_hash(rc)
    check("注释/分析语句变化不改变电气哈希（§6.1 口径）",
          h1 == schema.electrical_hash(rc_comments_only))
    check("激励参数变化改变电气哈希（验收 F06：DC/SIN 参数是电气内容）",
          h1 != schema.electrical_hash(rc_stimulus_changed))
    check("元件值变化改变电气哈希", h1 != schema.electrical_hash(rc_changed))
    check(".model 定义变化改变电气哈希（验收 F06）",
          h1 != schema.electrical_hash(rc_model_changed))

    with tempfile.TemporaryDirectory(prefix="vp_pcb_ver_") as td:
        ws = Workspace(td)
        ws.ensure()
        mapped = parts.map_netlist(rc, ports=["in", "out"])
        assert mapped["ok"]
        version = schema.new_version(netlist=rc, report=mapped["report"],
                                     rules=None, tool_versions={"k": "t"},
                                     job_id="job-1")
        version["board_hash"] = "deadbeef"
        version["status"] = schema.CHECKED
        vid = versions.save_candidate(ws, version)
        check("候选保存并可读回", versions.get_version(ws, vid)["pcb_version_id"] == vid)

        check("未采用时不过期", versions.is_stale(ws, rc)["stale"] is False)
        pointer = versions.adopt(ws, vid)
        check("采用 → revision=1", pointer["revision"] == 1
              and versions.current(ws)["version_id"] == vid)
        st = versions.is_stale(ws, rc)
        check("采用后同网表不过期", st["stale"] is False)
        st2 = versions.is_stale(ws, rc_changed)
        check("电气内容变化 → 过期（不凭版本号）", st2["stale"] is True)

        try:
            versions.adopt(ws, vid, expected_revision=0)  # 过期的期望 revision
            check("乐观并发：过期 revision 采用被拒", False)
        except ValueError as e:
            check("乐观并发：过期 revision 采用被拒", "更新" in str(e))
        versions.adopt(ws, vid, expected_revision=1)
        check("正确的 revision 可再采用（revision=2）",
              versions.current(ws)["revision"] == 2)
        check("版本列表非空", len(versions.list_versions(ws)) == 1)


def test_connectivity_partition() -> None:
    print("网络一致性：源电路连接分区 vs 板规格焊盘分组（方案 §7）")
    rc = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 1.59k",
                    "C1 out 0 100n", ".end"])
    ports = ["in", "out"]
    ir = netlist_to_ir(rc)
    mapped = parts.map_netlist(rc, ports)
    ok_case = connectivity.check_partition(ir, mapped["spec"], ports)
    check("正确映射 → 分区一致", ok_case["ok"], str(ok_case["issues"]))

    import copy
    broken = copy.deepcopy(mapped["spec"])
    r1 = next(c for c in broken["components"] if c["ref"] == "R1")
    r1["pads"] = ["in", "in"]  # 两脚接到同一网络（映射错网）：in 多一脚、out 丢一脚
    bad = connectivity.check_partition(ir, broken, ports)
    check("两脚同网（错网）→ 分区对照报警", not bad["ok"], str(bad["issues"]))

    import json as _j
    dropped = _j.loads(_j.dumps(mapped["spec"]))
    c1 = next(c for c in dropped["components"] if c["ref"] == "C1")
    c1["pads"][1] = "in"  # C1 的地脚被改接 in：GND 丢连接
    bad2 = connectivity.check_partition(ir, dropped, ports)
    check("丢地连接 → 分区对照报警", not bad2["ok"])

    copper = connectivity.copper_verdict(None, None)
    check("铜连接判定：无结果 → 不通过", not copper["ok"])
    copper2 = connectivity.copper_verdict(
        router_backend.RouteResult(ok=True, timed_out=False, unrouted=0,
                                   violations=0, score=1000.0, ses_written=True,
                                   elapsed=1.0, log=""),
        {"ok": True, "violations": 0, "unconnected": 0})
    check("铜连接判定：布线+DRC 全净 → 通过", copper2["ok"])


def test_jobs_offline() -> None:
    print("任务编排：离线路径（映射阻断 / 工具缺失 / 取消）不抛异常")
    rc = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 1.59k",
                    "C1 out 0 100n", ".end"])
    with tempfile.TemporaryDirectory(prefix="vp_pcb_job_") as td:
        r = jobs.run_pcb_job(rc, ["in", "out"], Path(td) / "j1")
        stages = {s["stage"]: s["ok"] for s in r["stages"]}
        tools_ready = (kicad_backend.find_kicad() is not None
                       and router_backend.find_freerouting() is not None
                       and router_backend.java_blocker(router_backend.find_java()) is None)
        if tools_ready:
            check("工具在场：全链任务 ok", r["ok"] and r["state"] == schema.CHECKED
                  and r["version_id"], str(r["stages"]))
            check("每阶段恰好执行一次（mapping 无重复）",
                  [s["stage"] for s in r["stages"]].count("mapping") == 1,
                  str([s["stage"] for s in r["stages"]]))
            check("产物齐备（manifest/checks/fab/BOM）",
                  (Path(td) / "j1" / "manifest.json").is_file()
                  and (Path(td) / "j1" / "checks" / "connectivity.json").is_file()
                  and (Path(td) / "j1" / "fabrication").is_dir()
                  and (Path(td) / "j1" / "fabrication" / "BOM.csv").is_file())
            bom = (Path(td) / "j1" / "fabrication" / "BOM.csv").read_text(encoding="utf-8-sig")
            check("BOM 含表头与行（位号/数值/封装合并）",
                  bom.startswith("位号,数值,封装,数量,型号,备注")
                  and "R1" in bom and "采购料号" in bom, bom[:120])
        else:
            check("工具缺失：映射先过、tools 阶段报缺失",
                  stages.get("mapping") is True and stages.get("tools") is False
                  and any(("KiCad" in b or "Freerouting" in b or "Java" in b)
                          for b in r["blockers"]),
                  str(r["stages"]) + str(r["blockers"]))

    opamp = "opamp deck\nXU1 in fb out OPAMP\n.end\n"
    with tempfile.TemporaryDirectory(prefix="vp_pcb_job2_") as td:
        r2 = jobs.run_pcb_job(opamp, ["in"], Path(td) / "j2")
        check("映射阻断 → FAILED 且给出方案 §4.2 原因",
              r2["state"] == schema.FAILED
              and any("理想运放" in b for b in r2["blockers"]),
              str(r2["blockers"]))

    with tempfile.TemporaryDirectory(prefix="vp_pcb_job3_") as td:
        r3 = jobs.run_pcb_job(rc, ["in", "out"], Path(td) / "j3",
                              cancel_check=lambda: True)
        check("阶段边界取消 → CANCELLED", r3["state"] == schema.CANCELLED
              and not r3["ok"], r3["state"])


def test_api_endpoints() -> None:
    print("HTTP 端点（§6.2）：capabilities/preflight/jobs/versions/apply/export")
    from fastapi.testclient import TestClient
    from app import main as app_main
    import tempfile as _tf
    # 隔离工作区：API 层测试不碰真实工作区/版本记录
    old_ws = app_main._active_ws
    tmp = _tf.mkdtemp(prefix="vp_pcb_api_")
    app_main._active_ws = type(old_ws)(tmp)
    try:
        c = TestClient(app_main.app, raise_server_exceptions=False)
        caps = c.get("/api/pcb/capabilities").json()
        check("capabilities 返回工具在场性与提示",
              caps["available"] in (True, False) and "kicad" in caps
              and "freerouting" in caps and caps["catalog_entries"] >= 10)

        rc = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 1.59k",
                        "C1 out 0 100n", ".end"])
        pre = c.post("/api/pcb/preflight", json={"netlist": rc, "ports": ["in", "out"]}).json()
        check("preflight：角色/端口候选/工具三项齐",
              pre["port_candidates"] == ["in"] and pre["tools_ready"] in (True, False)
              and isinstance(pre["elements"], list))
        pre_bad = c.post("/api/pcb/preflight",
                         json={"netlist": "oa\nXU1 a b c OPAMP\n.end", "ports": []}).json()
        check("preflight：理想运放进 blockers",
              any("理想运放" in b["reason"] for b in pre_bad["blockers"]))
        check("preflight 空 netlist 200（只报状态不炸）",
              c.post("/api/pcb/preflight", json={"netlist": "", "ports": []}).status_code == 200)
        check("preflight 非法 ports 400",
              c.post("/api/pcb/preflight", json={"netlist": rc, "ports": "in"}).status_code == 400)

        r404 = c.post("/api/pcb/jobs/x/cancel").status_code
        check("cancel 不存在的任务 404", r404 == 404)

        j = c.post("/api/pcb/jobs", json={"netlist": rc, "ports": ["in", "out"],
                                          "timeout_s": 120}).json()
        check("jobs 提交返回 job_id", bool(j.get("job_id")))
        jid = j["job_id"]
        ok_final = False
        for _ in range(120):  # 轮询至完成（工具在场 ~15s；缺失即时）
            st = c.get(f"/api/pcb/jobs/{jid}").json()
            if st.get("done"):
                ok_final = True
                break
            time.sleep(0.5)
        check("jobs 轮询到终态", ok_final)
        if ok_final:
            if st["ok"]:
                check("任务全链 ok 且产候选版本", st["state"] == "checked"
                      and st.get("version_id"))
                vid = st["version_id"]
                lst = c.get("/api/pcb/versions").json()
                check("版本列表含当前任务候选", any(v["pcb_version_id"] == vid
                                                   for v in lst["versions"]))
                # apply 带 revision：当前无指针 → expected=0 应冲突（指针 revision 从 0 起）
                ap = c.post("/api/pcb/apply", json={"version_id": vid,
                                                    "expected_revision": 0}).json()
                check("采用成功（首次采用 revision=1）",
                      ap.get("ok") and ap.get("revision") == 1, str(ap))
                exp = c.post("/api/pcb/export", json={"version_id": vid}).json()
                check("导出冻结制板包（文件清单+哈希）",
                      exp.get("ok") and exp.get("board_hash")
                      and any(f.endswith(".gbrjob") for f in exp.get("files", [])),
                      str(exp)[:200])
                ap2 = c.post("/api/pcb/apply", json={"version_id": vid,
                                                     "expected_revision": 0}).json()
                check("过期 revision 采用 → 409",
                      c.post("/api/pcb/apply", json={"version_id": vid,
                                                     "expected_revision": 0}).status_code == 409
                      or "error" in ap2)
                if caps["available"]:
                    # PUT placement：移动 J2 → 重布线 → 新候选（带 parent 链）
                    moved = c.put(f"/api/pcb/versions/{vid}/placement",
                                  json={"moves": {"J2": {"x": 50.0, "y": 20.0}}}).json()
                    check("布局调整重布线 → 新候选（parent 链）",
                          moved.get("ok") and moved.get("parent_version_id") == vid
                          and moved.get("version_id") != vid,
                          str({k: moved.get(k) for k in ("ok", "state", "parent_version_id")}))
                    # recheck：对已采用版本重跑 DRC+分区
                    rc2 = c.post("/api/pcb/recheck", json={"version_id": vid}).json()
                    check("回导再检查（DRC+分区对照）", rc2.get("ok") is True,
                          str(rc2)[:200])
                bad_move = c.put(f"/api/pcb/versions/{vid}/placement",
                                 json={"moves": {"J2": {"x": 9999, "y": 0}}})
                check("非法坐标 400", bad_move.status_code == 400)
            else:
                # 工具缺失：任务 FAILED 且给修复指引（按任务结果分支，
                # 不依赖 capabilities 的在场判定——其 java 探测与任务
                # 子进程的解析路径不同，曾出现两说分裂）
                check("工具缺失任务失败给指引",
                      st["state"] == "failed"
                      and any("KiCad" in b or "Freerouting" in b or "Java" in b
                              for b in st.get("blockers", [])),
                      str(st.get("blockers"))[:200])

        check("export 未知版本 404",
              c.post("/api/pcb/export", json={"version_id": "nope"}).status_code == 404)
        check("apply 未知版本 404",
              c.post("/api/pcb/apply", json={"version_id": "nope"}).status_code == 404)
    finally:
        app_main._active_ws = old_ws
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def test_preview_parser_and_project_source() -> None:
    print("预览解析器 + use_project 网表来源（前端页签的支撑）")
    # 解析器单元：构造最小 .kicad_pcb 文本（含旋转封装的焊盘）
    fake = """(kicad_pcb (version 20250123)
  (gr_line (start 5 5) (end 45 5) (layer "Edge.Cuts") (width 0.15))
  (gr_line (start 45 5) (end 45 35) (layer "Edge.Cuts") (width 0.15))
  (gr_line (start 45 35) (end 5 35) (layer "Edge.Cuts") (width 0.15))
  (gr_line (start 5 35) (end 5 5) (layer "Edge.Cuts") (width 0.15))
  (footprint "x" (at 20 10 90)
    (fp_text reference "R1" "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" thru_hole circle (at -5 0) (size 1.7 1.7) (net 3 "IN"))
    (pad "2" thru_hole circle (at 5 0) (size 1.7 1.7) (net 4 "OUT")))
  (segment (start 20 10) (end 30 10) (width 0.25) (layer "F.Cu") (net 3))
  (segment (start 30 10) (end 30 20) (width 0.25) (layer "B.Cu") (net 4))
  (via (at 30 10) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 3)))
"""
    with tempfile.TemporaryDirectory(prefix="vp_pcb_prev_") as td:
        p = Path(td) / "b.kicad_pcb"
        p.write_text(fake, encoding="utf-8")
        d = kicad_backend.preview_from_pcb(p)
        check("解析出板框 8 点", d is not None and len(d["outline"]) == 8)
        check("焊盘 2 个且 net 名正确",
              len(d["pads"]) == 2 and {x["net"] for x in d["pads"]} == {"IN", "OUT"})
        # 封装旋转 90°：焊盘 (-5,0) → 板坐标 (20,10)+rot90(-5,0)=(20+0,10+(-5))=(20,5)
        pad1 = next(x for x in d["pads"] if x["net"] == "IN")
        check("封装旋转坐标变换正确", abs(pad1["x"] - 20.0) < 0.01 and abs(pad1["y"] - 5.0) < 0.01,
              f"({pad1['x']},{pad1['y']})")
        check("走线分层正确（F/B 各 1）",
              sum(1 for t in d["tracks"] if t["layer"] == "F.Cu") == 1
              and sum(1 for t in d["tracks"] if t["layer"] == "B.Cu") == 1)
        check("过孔 1 个", len(d["vias"]) == 1)
        check("bbox 覆盖板框", d["bbox"][0] <= 5 and d["bbox"][2] >= 45)
    check("空文件解析返回 None",
          kicad_backend.preview_from_pcb(Path(_mkfile("x", ""))) is None)

    # use_project 语义（API 层）：项目净表/dirty 两分支
    from fastapi.testclient import TestClient
    from app import main as app_main
    old_ws = app_main._active_ws
    tmp = tempfile.mkdtemp(prefix="vp_pcb_prj_")
    ws = type(old_ws)(tmp)
    ws.ensure()
    app_main._active_ws = ws
    try:
        c = TestClient(app_main.app, raise_server_exceptions=False)
        r = c.post("/api/pcb/preflight", json={"use_project": True, "ports": []})
        check("空项目 preflight → 400（无电路）", r.status_code == 400)
        rc = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 1.59k",
                        "C1 out 0 100n", ".end"])
        p = ws.load_project()
        p.update(netlist=rc, netlist_hash="h1")
        ws.save_project(p)
        r2 = c.post("/api/pcb/preflight", json={"use_project": True, "ports": ["in", "out"]})
        check("项目净表 use_project 预检通过",
              r2.status_code == 200 and r2.json()["port_candidates"] == ["in"])
        p = ws.load_project()
        p.update(dirty=True)
        ws.save_project(p)
        r3 = c.post("/api/pcb/jobs", json={"use_project": True, "ports": []})
        check("画布 dirty → 400 要求先验证", r3.status_code == 400
              and "仿真" in r3.json()["error"])
    finally:
        app_main._active_ws = old_ws
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def _mkfile(name: str, text: str) -> str:
    f = tempfile.NamedTemporaryFile(prefix="vp_", suffix=name, delete=False, mode="w",
                                    encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


def test_placement_signal_flow() -> None:
    print("信号流布局（P1：替换网格占位）")
    # 两级 RC：in → R1 → mid → R2 → out（GND 不参与分层）
    comps = [
        {"ref": "R1", "pads": ["in", "mid"]},
        {"ref": "R2", "pads": ["mid", "out"]},
        {"ref": "C1", "pads": ["mid", "GND"]},
    ]
    layers = placement.signal_layers(comps, ["in", "out"])
    check("R1 在第 1 层（紧随输入口）", layers["R1"] == 1)
    check("R2/C1 在 R1 之后", layers["R2"] > layers["R1"] and layers["C1"] > layers["R1"])
    w, h, pos = placement.place(comps, ["in", "out"])
    check("信号流向：R1.x < R2.x（左进右出）", pos["R1"][0] < pos["R2"][0])
    check("端子两侧：in 靠左 out 靠右",
          pos["__port__0"][0] < pos["R1"][0] and pos["__port__1"][0] > pos["R2"][0])
    xs = [pos[r][0] for r in ("R1", "R2", "C1")]
    # 庭院安全：横向同层间距或纵向错行 ≥ 间距常量
    pairs_ok = all(
        abs(pos[a][0] - pos[b][0]) >= placement.X_PITCH - 0.01
        or abs(pos[a][1] - pos[b][1]) >= placement.Y_PITCH - 0.01
        for a, b in (("R1", "R2"), ("R1", "C1"), ("R2", "C1")))
    check("任意两器件庭院安全（横向≥14 或纵向≥13）", pairs_ok,
          str({k: pos[k] for k in ("R1", "R2", "C1")}))
    check("确定性：同输入同布局", placement.place(comps, ["in", "out"])[2] == pos)
    # 映射集成：RC 网表布局坐标来自 placement
    rc = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 1.59k",
                    "C1 out 0 100n", ".end"])
    r = parts.map_netlist(rc, ports=["in", "out"])
    if r["ok"]:
        comps2 = {c["ref"]: c for c in r["spec"]["components"]}
        check("映射板坐标=信号流布局（R1 左于 J2/右侧端子）",
              comps2["R1"]["x"] < comps2["J2"]["x"])

    # 纯全局网器件（悬空）沉到最后层不炸
    layers2 = placement.signal_layers([{"ref": "RX", "pads": ["GND", "GND"]}], ["a"])
    check("悬空器件不炸且沉末层", layers2["RX"] >= 1)


def test_integration_chinese_path_and_locked() -> None:
    print("集成：中文路径全链 + 锁定走线 DSN/SES 往返保真（工具在场才跑）")
    kicad = kicad_backend.find_kicad()
    jar = router_backend.find_freerouting()
    if kicad is None or jar is None or router_backend.java_blocker(router_backend.find_java()):
        skip("工具缺失 → 跳过")
        return
    # 1) 中文路径：目录与文件名都带中文
    with tempfile.TemporaryDirectory(prefix="中文路径_测试_") as td:
        tdir = Path(td)
        board_dir = tdir / "电路板工程"
        board_dir.mkdir()
        pcb = board_dir / "测试板.kicad_pcb"
        dsn = board_dir / "测试板.dsn"
        ses = board_dir / "测试板.ses"
        ok, out = kicad_backend.build_board(kicad, GOOD_SPEC, pcb, board_dir)
        check("中文路径造板", ok, out[:200])
        ok, out = kicad_backend.export_dsn(kicad, pcb, dsn, board_dir)
        check("中文路径 DSN 导出", ok, out[:200])
        rr = router_backend.route(jar, dsn, ses, board_dir / "布线", max_passes=5, timeout_s=120)
        check("中文路径布线", rr.ok, rr.summary)
        ok, out = kicad_backend.import_ses(kicad, pcb, ses, board_dir)
        check("中文路径 SES 回导", ok, out[:200])
        res = kicad_backend.drc(kicad, pcb, board_dir / "检查.json")
        check("中文路径 DRC JSON", res["ok"] and res["violations"] == 0,
              str(res)[:200])

    # 2) 锁定走线保真：锁定一条走线 → DSN → 布线 → 回导后锁定走线仍在
    with tempfile.TemporaryDirectory(prefix="vp_pcb_lock_") as td:
        tdir = Path(td)
        pcb = tdir / "b.kicad_pcb"
        dsn = tdir / "b.dsn"
        ses = tdir / "b.ses"
        ok, out = kicad_backend.build_board(kicad, GOOD_SPEC, pcb, tdir)
        if not ok:
            check("造板（锁定用例前置）", False, out[:200])
            return
        lock_script = (
            "import pcbnew\n"
            f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
            "t = pcbnew.PCB_TRACK(b)\n"
            "t.SetStart(pcbnew.VECTOR2I(12*1000000, 18*1000000))\n"
            "t.SetEnd(pcbnew.VECTOR2I(18*1000000, 18*1000000))\n"
            "t.SetWidth(250000)\n"
            "t.SetLayer(b.GetLayerID('F.Cu'))\n"
            "t.SetNet(b.FindNet('GND'))\n"
            "t.SetLocked(True)\n"
            "b.Add(t)\n"
            f"print('LOCKED_SAVED:', pcbnew.SaveBoard({str(pcb)!r}, b))\n")
        lock_path = tdir / "_lock.py"
        lock_path.write_text(lock_script, encoding="utf-8")
        import subprocess as _sp
        proc = _sp.run([str(kicad.python), str(lock_path)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120,
                       env=kicad.subprocess_env(), **no_window_kwargs())
        check("锁定走线写入", "LOCKED_SAVED: True" in proc.stdout,
              (proc.stdout + proc.stderr)[-300:])
        if "LOCKED_SAVED: True" not in proc.stdout:
            return
        ok, out = kicad_backend.export_dsn(kicad, pcb, dsn, tdir)
        check("锁定板 DSN 导出", ok, out[:200])
        rr = router_backend.route(jar, dsn, ses, tdir / "rt", max_passes=5, timeout_s=120)
        check("锁定板布线（布线器须绕开锁定走线）", rr.ok, rr.summary)
        ok, out = kicad_backend.import_ses(kicad, pcb, ses, tdir, fill_zones=False)
        check("锁定板 SES 回导", ok, out[:200])
        verify = (
            "import pcbnew\n"
            f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
            "locked = [t for t in b.GetTracks() if t.IsLocked()]\n"
            "print('LOCKED_NOW:', len(locked))\n")
        vpath = tdir / "_verify.py"
        vpath.write_text(verify, encoding="utf-8")
        proc2 = _sp.run([str(kicad.python), str(vpath)], capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=120,
                        env=kicad.subprocess_env(), **no_window_kwargs())
        check("往返后锁定走线仍在", "LOCKED_NOW: 1" in proc2.stdout,
              (proc2.stdout + proc2.stderr)[-300:])


def test_fixed_cases() -> None:
    print("§10 固定测试集：30 支持案例（映射+分区对照全过）+ 15 异常（按原因阻断）")
    import json as _json
    cases_path = Path(__file__).resolve().parent.parent / "app" / "pcb" / "tests_data" / "pcb_cases.json"
    data = _json.loads(cases_path.read_text(encoding="utf-8"))
    sup = [c for c in data["supported"] if not c.get("skip")]
    fails: list[str] = []
    for c in sup:
        r = parts.map_netlist(c["netlist"], c["ports"])
        if not r["ok"]:
            fails.append(f"{c['id']}: 映射阻断 " + "; ".join(
                b["reason"][:60] for b in r["report"]["blocked"][:2]))
            continue
        ir = netlist_to_ir(c["netlist"])
        part = connectivity.check_partition(ir, r["spec"], c["ports"])
        if not part["ok"]:
            fails.append(f"{c['id']}: 分区不一致 " + "; ".join(part["issues"][:2]))
        try:
            kicad_backend.validate_spec(r["spec"])
        except kicad_backend.SpecError as e:
            fails.append(f"{c['id']}: 板规格非法 {e}")
    check(f"支持案例 {len(sup)} 个全部映射通过+分区一致+规格合法", not fails,
          "；".join(fails[:4]))
    check("支持案例数量 = 30", len(sup) == 30, str(len(sup)))

    ab_fails: list[str] = []
    for c in data["abnormal"]:
        r = parts.map_netlist(c["netlist"], c["ports"])
        kw = c.get("expect_keyword", "")
        if r["ok"]:
            ab_fails.append(f"{c['id']}: 应阻断却通过")
        elif kw and not any(kw in b["reason"] for b in r["report"]["blocked"]):
            ab_fails.append(f"{c['id']}: 阻断原因不含 {kw!r}：" +
                            "; ".join(b["reason"][:50] for b in r["report"]["blocked"][:2]))
    check(f"异常案例 {len(data['abnormal'])} 个全部按原因阻断", not ab_fails,
          "；".join(ab_fails[:4]))
    check("异常案例数量 = 15", len(data["abnormal"]) == 15)


def test_acceptance_regressions() -> None:
    """验收报告（PCB_ACCEPTANCE_2026-10-30）反例 → 修复后正确行为断言。"""
    print("验收反例回归：F01–F07 修复行为")
    import copy
    import json as _json
    from types import SimpleNamespace
    from unittest.mock import patch

    # F01：交换二极管两脚必须被规格级对照抓住
    diode_net = "d deck\nV1 vin 0 5\nD1 vin out 1N4148\nR1 out 0 1k\n.end\n"
    mapped = parts.map_netlist(diode_net, ["vin", "out"])
    assert mapped["ok"], mapped["report"]["blocked"]
    bad_spec = copy.deepcopy(mapped["spec"])
    d1 = next(c for c in bad_spec["components"] if c["ref"] == "D1")
    d1["pads"].reverse()
    bad = connectivity.check_partition(netlist_to_ir(diode_net), bad_spec, ["vin", "out"])
    check("F01 规格：二极管 A/K 交换 → 引脚级对照报警", not bad["ok"],
          str(bad["issues"][:2]))
    good = connectivity.check_partition(netlist_to_ir(diode_net), mapped["spec"],
                                        ["vin", "out"])
    check("F01 规格：正确映射 → 通过", good["ok"], str(good["issues"][:2]))
    # F01 板级：伪造板预览（交换 D1 两焊盘网络）必须被抓住
    preview_pads = []
    for p in _preview_from_spec(mapped["spec"]):
        preview_pads.append(dict(p))
    swapped = False
    for p in preview_pads:
        if p["ref"] == "D1":
            other = next(q for q in preview_pads if q["ref"] == "D1" and q is not p)
            p["net"], other["net"] = other["net"], p["net"]
            swapped = True
            break
    assert swapped
    board_bad = connectivity.board_partition_with_map(
        {"pads": preview_pads}, netlist_to_ir(diode_net), ["vin", "out"], mapped["spec"])
    check("F01 板级：板文件焊盘网络交换 → 对照报警", not board_bad["ok"],
          str(board_bad["issues"][:2]))
    board_ok = connectivity.board_partition_with_map(
        {"pads": _preview_from_spec(mapped["spec"])}, netlist_to_ir(diode_net),
        ["vin", "out"], mapped["spec"])
    check("F01 板级：正确板 → 通过", board_ok["ok"], str(board_ok["issues"][:2]))

    # F02：数字节点与命名节点不得合并
    collision = "c deck\nR1 1 N2 1k\nR2 N2 0 2k\n.end\n"
    cir = netlist_to_ir(collision)
    r1_nets = next(e for e in cir["elements"] if e["ref"] == "R1")["nets"]
    check("F02：节点 1 与 N2 保持不同网络", r1_nets[0] != r1_nets[1], str(r1_nets))

    # F03：电流源与显式 DC 0 检测源都阻断
    cur = parts.map_netlist("i deck\nI1 out 0 DC 1m\nR1 out 0 1k\n.end\n", [])
    check("F03：电流源显式阻断（不静默删）", not cur["ok"]
          and any("电流源" in b["reason"] for b in cur["report"]["blocked"]))
    zero = parts.map_netlist("z deck\nVprobe a b DC 0\nR1 a 0 1k\nR2 b 0 2k\n.end\n",
                             ["a", "b"])
    check("F03：显式 DC 0 检测源阻断", not zero["ok"]
          and any("0V" in b["reason"] for b in zero["report"]["blocked"]),
          str(zero["report"]["blocked"][:2]))

    # F06：哈希敏感性与源绑定
    dc5 = "s deck\nV1 vin 0 DC 5\nR1 vin 0 1k\n.end\n"
    dc50 = dc5.replace("DC 5", "DC 50")
    check("F06：DC 5→50 改变电气哈希",
          schema.electrical_hash(dc5) != schema.electrical_hash(dc50))
    ma = "m deck\nV1 v 0 5\nD1 v a 1N4148\nR1 a 0 1k\n.model 1N4148 D(Is=1n)\n.end\n"
    mb = ma.replace("Is=1n", "Is=10n")
    check("F06：.model 参数变化改变电气哈希",
          schema.electrical_hash(ma) != schema.electrical_hash(mb))

    # F04/F05：控制器级（模拟工具）
    with tempfile.TemporaryDirectory(prefix="vp_acc_") as td:
        ws = SimpleNamespace(root=Path(td), cp_dir=Path(td) / ".cp")
        job_id = "acc-job"
        archive = ws.root / "pcb" / job_id
        for sub in ("input", "checks", "final", "fabrication"):
            (archive / sub).mkdir(parents=True)
        (archive / "input" / "netlist.txt").write_text(diode_net, encoding="utf-8")
        (archive / "input" / "ports.json").write_text('["vin", "out"]', encoding="utf-8")
        (archive / "checks" / "mapping_report.json").write_text(
            _json.dumps(mapped["report"]), encoding="utf-8")
        board = archive / "final" / "board.kicad_pcb"
        board.write_text("fixture-board", encoding="utf-8")
        version = schema.new_version(netlist=diode_net, report=mapped["report"],
                                     rules=None, tool_versions={}, job_id=job_id)
        version.update(status=schema.CHECKED, board_hash=api_mod._sha256_file(board))
        vid = versions.save_candidate(ws, version)
        versions.adopt(ws, vid, require_fresh_source=False)  # 控制器替身无项目
        with patch.object(api_mod, "_active_ws", return_value=ws):
            r = api_mod.export_package({"version_id": vid})
            check("F04：空产物目录 → 422 拒绝（不再 ok+空文件表）",
                  isinstance(r, JSONResponse) and r.status_code == 422, str(r)[:120])
            (archive / "fabrication" / "b.gbrjob").write_text("{}", encoding="utf-8")
            (archive / "fabrication" / "b.drl").write_text("x", encoding="utf-8")
            (archive / "fabrication" / "BOM.csv").write_text("位号\n", encoding="utf-8-sig")
            board.write_text("edited board", encoding="utf-8")
            r2 = api_mod.export_package({"version_id": vid})
            check("F04：板哈希变化 → 409 拒绝导出",
                  isinstance(r2, JSONResponse) and r2.status_code == 409, str(r2)[:120])
            board.write_text("fixture-board", encoding="utf-8")  # 恢复
            # 模拟复核失败 → 版本降级，导出被拒
            with patch.object(kicad_backend, "find_kicad", return_value=object()), \
                 patch.object(kicad_backend, "drc",
                              return_value={"ok": True, "violations": 2, "unconnected": 0}), \
                 patch.object(kicad_backend, "preview_from_pcb",
                              return_value={"pads": _preview_from_spec(mapped["spec"])}):
                rc2 = api_mod.recheck({"version_id": vid})
            check("F04：复核失败 → ok=false 且版本降级 recheck_failed",
                  rc2["ok"] is False
                  and versions.get_version(ws, vid)["status"] == schema.RECHECK_FAILED)
            r3 = api_mod.export_package({"version_id": vid})
            check("F04：复核失败后导出被拒",
                  isinstance(r3, JSONResponse) and r3.status_code == 400, str(r3)[:120])
            versions.update_version_status(ws, vid, status=schema.CHECKED)
            r4 = api_mod.export_package({"version_id": vid})
            check("F04：恢复 checked 且内容一致 → 导出成功（含逐文件哈希）",
                  isinstance(r4, dict) and r4.get("ok") and len(r4.get("file_hashes", {})) >= 3)

        # F05：规则必须传到造板
        captured = {}
        def fake_build(k, spec, out_pcb, workdir, rules=None):
            captured["rules"] = rules
            out_pcb.write_text("x", encoding="utf-8")
            return True, "fixture"
        k = SimpleNamespace(version=lambda: "mock", python="x", cli="x", footprints=Path("."))
        rr = SimpleNamespace(ok=True, unrouted=0, problems=[], timed_out=False,
                             summary="mock")
        strict = {**schema.default_rules(), "track_mm": 2.0}

        def fake_fab(kk, pcb, out_dir, timeout=120):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "b.gbrjob").write_text("{}", encoding="utf-8")
            (out_dir / "b.drl").write_text("x", encoding="utf-8")
            return True, "fixture"

        with patch.object(kicad_backend, "find_kicad", return_value=k), \
             patch.object(router_backend, "find_freerouting", return_value=Path("m.jar")), \
             patch.object(router_backend, "java_blocker", return_value=None), \
             patch.object(kicad_backend, "build_board", side_effect=fake_build), \
             patch.object(kicad_backend, "export_dsn", return_value=(True, "x")), \
             patch.object(router_backend, "route", return_value=rr), \
             patch.object(kicad_backend, "import_ses", return_value=(True, "x")), \
             patch.object(kicad_backend, "preview_from_pcb",
                          return_value={"pads": _preview_from_spec(mapped["spec"])}), \
             patch.object(kicad_backend, "drc",
                          return_value={"ok": True, "violations": 0, "unconnected": 0,
                                        "violations_warning": 0}), \
             patch.object(kicad_backend, "export_fab", side_effect=fake_fab):
            job = jobs.run_pcb_job(diode_net, ["vin", "out"], Path(td) / "rules-job",
                                   rules=strict)
        check("F05：制造规则传入造板（track_mm=2.0 到达 build_board）",
              captured.get("rules", {}).get("track_mm") == 2.0,
              str(captured.get("rules")))
        check("F05：规则任务全链（模拟工具）通过", job.get("ok"), job.get("state"))


    # F05 真实链路：违反最小线宽的板必须被 DRC 抓住（工具在场才跑）
    kicad = kicad_backend.find_kicad()
    if kicad is not None:
        with tempfile.TemporaryDirectory(prefix="vp_rule_") as td:
            tdir = Path(td)
            pcb = tdir / "b.kicad_pcb"
            mapped2 = parts.map_netlist("r deck\nV1 v 0 5\nR1 v o 1k\n.end", ["v"])
            ok, out = kicad_backend.build_board(
                kicad, mapped2["spec"], pcb, tdir,
                rules={"track_mm": 1.0, "clearance_mm": 0.2, "via_drill_mm": 0.6,
                       "via_diameter_mm": 1.0})
            check("F05 真实：规则板造板+工程文件落盘", ok and
                  pcb.with_suffix(".kicad_pro").is_file(), out[-200:])
            if ok:
                thin = ("import pcbnew\n"
                        f"b = pcbnew.LoadBoard({str(pcb)!r})\n"
                        "t = pcbnew.PCB_TRACK(b)\n"
                        "t.SetStart(pcbnew.VECTOR2I(12*1000000, 10*1000000))\n"
                        "t.SetEnd(pcbnew.VECTOR2I(30*1000000, 10*1000000))\n"
                        "t.SetWidth(250000)\n"
                        "t.SetLayer(b.GetLayerID('F.Cu'))\n"
                        "t.SetNet(b.FindNet('v'))\n"
                        "b.Add(t)\n"
                        f"print('THIN:', pcbnew.SaveBoard({str(pcb)!r}, b))\n")
                sp = tdir / "_thin.py"
                sp.write_text(thin, encoding="utf-8")
                pr = subprocess.run([str(kicad.python), str(sp)], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=120,
                                    env=kicad.subprocess_env(), **no_window_kwargs())
                assert "THIN: True" in pr.stdout, pr.stdout[-200:]
                res = kicad_backend.drc(kicad, pcb, tdir / "d.json")
                check("F05 真实：0.25mm 走线违反 1.0mm 最小线宽 → DRC 报错",
                      (res["violations"] or 0) > 0, str(res)[:150])
    else:
        skip("F05 真实 DRC 规则执行（工具缺失）")


def _preview_from_spec(spec: dict) -> list[dict]:
    """从板规格合成 preview 焊盘形（ref/pad_no/net）——板级对照测试的板侧替身。"""
    from app.pcb import parts as _parts
    entries = {e["id"]: e for e in _parts.load_catalog()["entries"]}
    pads = []
    for c in spec["components"]:
        entry = entries[c["part"]]
        for i, net in enumerate(c["pads"], start=1):
            pad_no = sorted(entry["pad_functions"], key=int)[i - 1]
            pads.append({"ref": c["ref"], "pad_no": pad_no, "net": net,
                         "x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0})
    return pads


def test_catalog_and_board_options() -> None:
    print("器件库扩容 + 安装方式/板选项（验收遗留项收尾）")
    cat = parts.load_catalog()
    check("器件库 ≥20 条（计划规模）", len(cat["entries"]) >= 20,
          str(len(cat["entries"])))
    mounts = {e.get("mount", "tht") for e in cat["entries"]}
    check("含 tht 与 smd 两类安装方式", mounts == {"tht", "smd"}, str(mounts))

    rc = "\n".join(["RC deck", "Vin in 0 SIN(0 1 1k)", "R1 in out 1.59k",
                    "C1 out 0 100n", ".end"])
    smd = parts.map_netlist(rc, ["in", "out"], options={"mount": "smd"})
    check("smd 映射：R/C→0805", smd["ok"] and all(
        c["footprint"].startswith(("R_0805", "C_0805"))
        for c in smd["spec"]["components"] if c["ref"] in ("R1", "C1")),
        str([(c["ref"], c["footprint"]) for c in smd["spec"]["components"]]))
    q_smd = parts.map_netlist("q deck\nQ1 v b 0 QNPN\nR1 v c 4.7k\n.end", [],
                              options={"mount": "smd"})
    q1 = next(x for x in q_smd["report"]["mapped"] if x["ref"] == "Q1")
    check("smd 三极管：MMBT3904 SOT-23（脚位 1=B）",
          q1["part"] == "Q_MMBT3904_SOT23" and q1["pin_map"]["1"]["function"] == "B")
    q_tht = parts.map_netlist("q deck\nQ1 v b 0 QNPN\nR1 v c 4.7k\n.end", [])
    q1t = next(x for x in q_tht["report"]["mapped"] if x["ref"] == "Q1")
    check("tht 三极管：2N3904 TO-92（脚位 1=E）——同型号不同安装不同脚位表",
          q1t["part"] == "Q_2N3904" and q1t["pin_map"]["1"]["function"] == "E")

    opts = {"mount": "tht", "board_w": 70, "board_h": 45,
            "mounting_holes": True, "keepouts": [{"x": 30, "y": 4, "w": 10, "h": 6}]}
    m = parts.map_netlist(rc, ["in", "out"], options=opts)
    check("板选项：尺寸不小于请求 + 四安装孔 + 禁布区",
          m["ok"] and m["spec"]["size_mm"][0] >= 70 and m["spec"]["size_mm"][1] >= 45
          and len([c for c in m["spec"]["components"] if c["part"] == "HOLE_M3"]) == 4
          and len(m["spec"]["keepouts"]) == 1, str(m["spec"].get("size_mm")))
    bad_opt = parts.map_netlist(rc, ["in", "out"],
                                options={"keepouts": [{"x": 1, "y": 1, "w": 0, "h": 5}]})
    check("非法禁布区 → 阻断", not bad_opt["ok"]
          and any("禁布区" in b["reason"] for b in bad_opt["report"]["blocked"]))

    kicad = kicad_backend.find_kicad()
    if kicad is None:
        skip("真实板选项造板（工具缺失）")
        return
    with tempfile.TemporaryDirectory(prefix="vp_opt_") as td:
        tdir = Path(td)
        pcb = tdir / "b.kicad_pcb"
        ok, out = kicad_backend.build_board(kicad, m["spec"], pcb, tdir)
        check("带孔/禁布区板真实造板（DRC 0 错误）", ok, out[-250:])
        if ok:
            res = kicad_backend.drc(kicad, pcb, tdir / "d.json")
            check("造板 DRC 0 错误（未布线的未连项属预期）",
                  res["violations"] == 0, str(res)[:150])
            import re as _re
            txt = pcb.read_text(encoding="utf-8")
            check("板文件含规则区域（禁布区）与安装孔",
                  txt.count("H1") >= 1 and txt.count("KEEPOUT") + len(
                      _re.findall(r"\(zone[^)]*\(layer \"[FB]\.Cu\"", txt)) >= 2,
                  "")


def main() -> int:
    for fn in (test_parse_log, test_spec_validation, test_finders_missing,
               test_linux_discovery_and_launch,
               test_circuit_ir_and_mapping, test_schema_and_versions,
               test_connectivity_partition, test_jobs_offline,
               test_placement_signal_flow,
               test_preview_parser_and_project_source, test_api_endpoints,
               test_integration_smoke, test_integration_netlist_to_fab,
               test_integration_chinese_path_and_locked, test_fixed_cases,
               test_acceptance_regressions, test_catalog_and_board_options):
        print(f"\n== {fn.__name__} ==")
        fn()
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
