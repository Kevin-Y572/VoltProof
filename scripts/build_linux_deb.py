"""构建 VoltProof 的 Debian/Ubuntu 安装包（.deb）。

包里只有 VoltProof 和它的 Python 依赖，装到 /opt/voltproof/lib，
并提供 /usr/bin/voltproof 与桌面入口。ngspice 是包依赖，由 apt 安装。
KiCad、Java、Freerouting 不打进包里，PCB 导出按 docs/LINUX.md 另装。

在仓库根目录、与目标机器相同的 Python 次版本上运行：

    python3 scripts/build_linux_deb.py

产物：dist/voltproof_<版本>_<架构>.deb

    sudo apt install ./dist/voltproof_<版本>_<架构>.deb
    voltproof

需要：python3、python3-pip、dpkg-deb，以及能访问 PyPI 的网络。
脚本不使用 root。安装 .deb 时才需要 root。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import zlib
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STAGE = REPO / "dist" / "linux" / "stage"
LIB = STAGE / "opt" / "voltproof" / "lib"
PACKAGING = REPO / "packaging" / "linux"


def _version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not match:
        sys.exit("pyproject.toml 里没有 version")
    return match.group(1)


def _require(cmd: str) -> None:
    if shutil.which(cmd) is None:
        sys.exit(f"缺少 {cmd}。Debian/Ubuntu 上可安装 dpkg 与 python3-pip。")


def _png(size: int = 48) -> bytes:
    """不依赖第三方库的纯色图标，桌面入口用。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    rgb = bytes((14, 165, 233))
    raw = b"".join(b"\x00" + rgb * size for _ in range(size))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _control(version: str, arch: str, installed_kb: int) -> str:
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    nxt = f"{sys.version_info.major}.{sys.version_info.minor + 1}"
    return f"""Package: voltproof
Version: {version}
Section: science
Priority: optional
Architecture: {arch}
Installed-Size: {installed_kb}
Depends: python3 (>= {py}), python3 (<< {nxt}), ngspice
Maintainer: Kevin-Y572 <251250048@smail.nju.edu.cn>
Homepage: https://github.com/Kevin-Y572/VoltProof
Description: Local circuit workbench checked by ngspice
 VoltProof runs AI circuit answers through real ngspice simulation.
 The package installs the app, its Python dependencies, a voltproof
 command, and a desktop entry. ngspice comes from the distro.
 KiCad 10.x, Java 21+, and the Freerouting JVM JAR are not bundled;
 see /usr/share/doc/voltproof/README.Debian for PCB export.
"""


def _readme_debian() -> str:
    return """VoltProof Linux 包
===================

本包提供工作台、画板、对照页和 ngspice 仿真。启动：

    voltproof

浏览器会打开 http://127.0.0.1:8000 。数据在 ~/.local/share/voltproof ，
模型设置在 ~/.config/voltproof/settings.json 。

PCB 导出（实验性）不包含在本包内。需要 KiCad 10.x、Java 21+、
Freerouting JVM JAR（Java 21 用 freerouting-2.1.0.jar）。
步骤见仓库 docs/LINUX.md。工具路径可写在 ~/.config/voltproof/env 。

本包不是操作系统沙箱。网表和模型生成的 Python 只做命令、路径和 AST 检查。
"""


def _install_payload(lib: Path) -> None:
    if lib.exists():
        shutil.rmtree(lib)
    lib.mkdir(parents=True)
    env = os.environ.copy()
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    cmd = [sys.executable, "-m", "pip", "install", "--target", str(lib),
           "--upgrade", "--no-warn-script-location", str(REPO)]
    print("安装 Python 依赖到", lib, flush=True)
    subprocess.check_call(cmd, cwd=str(REPO), env=env)
    scripts = lib / "bin"
    if scripts.exists():
        shutil.rmtree(scripts)
    messages = lib / "app" / "static" / "messages.json"
    catalog = lib / "app" / "pcb" / "part_catalog" / "catalog.json"
    if not messages.is_file() or not catalog.is_file():
        sys.exit(f"安装结果缺资源文件：{messages} / {catalog}")


def _stage_files(version: str, arch: str) -> None:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    _install_payload(LIB)
    launcher = STAGE / "usr" / "bin" / "voltproof"
    launcher.parent.mkdir(parents=True)
    shutil.copyfile(PACKAGING / "voltproof.sh", launcher)
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    apps = STAGE / "usr" / "share" / "applications"
    apps.mkdir(parents=True)
    shutil.copyfile(PACKAGING / "voltproof.desktop", apps / "voltproof.desktop")
    icon_dir = STAGE / "usr" / "share" / "icons" / "hicolor" / "48x48" / "apps"
    icon_dir.mkdir(parents=True)
    (icon_dir / "voltproof.png").write_bytes(_png())
    doc = STAGE / "usr" / "share" / "doc" / "voltproof"
    doc.mkdir(parents=True)
    (doc / "README.Debian").write_text(_readme_debian(), encoding="utf-8")
    shutil.copyfile(REPO / "LICENSE", doc / "copyright")
    size_kb = max(1, sum(p.stat().st_size for p in STAGE.rglob("*") if p.is_file()) // 1024)
    debian = STAGE / "DEBIAN"
    debian.mkdir()
    (debian / "control").write_text(_control(version, arch, size_kb), encoding="utf-8")


def _self_check() -> None:
    print("检查打进包里的应用可以导入", flush=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(LIB),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    subprocess.check_call(
        [sys.executable, "-c",
         "import app.main, app.i18n, app.pcb.kicad_backend, app.pcb.router_backend\n"
         "assert app.main.app.title == 'VoltProof'\n"
         "assert app.i18n.t('pcb.run')\n"
         "print('package-import-ok')\n"],
        cwd="/tmp", env=env)


def main() -> int:
    _require("dpkg-deb")
    if shutil.which("pip3") is None and shutil.which("pip") is None:
        # python3 -m pip 仍可能可用；下面的安装步骤会暴露真实错误。
        pass
    version = _version()
    arch = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    _stage_files(version, arch)
    _self_check()
    out_dir = REPO / "dist"
    out_dir.mkdir(exist_ok=True)
    deb = out_dir / f"voltproof_{version}_{arch}.deb"
    if deb.exists():
        deb.unlink()
    subprocess.check_call(["dpkg-deb", "--root-owner-group", "--build", str(STAGE), str(deb)])
    subprocess.check_call(["dpkg-deb", "-I", str(deb)])
    print(f"\n已生成 {deb}")
    print(f"安装：sudo apt install ./{deb.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
