"""便携版构建：内嵌 Python 运行时 + 依赖 + ngspice + 启动器 → 一个免安装 zip。

产物解压到任意目录、双击 start.bat 即用——不需要安装 Python、不需要安装
ngspice、不需要配环境变量（ngspice 由 app.ngspice_runner 的第三级探测
"<包根>/ngspice/bin" 接管，见 _resolve_ngspice）。

为什么用官方 embeddable 发行版而不是 PyInstaller：本产品会在子进程里用
sys.executable -I 执行 LLM 生成的受限代码（SKiDL/schemdraw 沙箱），
embeddable 里的 sys.executable 是真正的 python.exe，沙箱语义原样保留；
PyInstaller 冻结后 sys.executable 指向产品 exe，沙箱会整体失效。

安全写法说明：所有子进程调用在调用点写显式参数列表 + shell=False（无任何
外部输入拼进命令行）；--ngspice 路径先拒绝 ".."，再 resolve 规范化并强制
校验 bin/ngspice.exe 存在后才使用。

用法（仓库根目录）：
    python scripts/build_portable.py --ngspice "D:\\path\\to\\ngspice-47\\Spice64"

产物：
    dist/VoltProof/                    解压即用的目录
    dist/VoltProof-win64-portable.zip  发布用压缩包
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"
APPDIR = DIST / "VoltProof"
PYEXE = APPDIR / "py" / "python.exe"
DL = DIST / "_dl"

PY_EMBED_URL = ("https://www.python.org/ftp/python/3.12.10/"
                "python-3.12.10-embed-amd64.zip")
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# 便携说明（随包分发，替换仓库 README 里的安装章节）
_PORTABLE_README = """\
# VoltProof 便携版（Windows x64，免安装）

解压到任意有写权限的目录，双击 **start.bat**，浏览器会自动打开对话页。

- 不需要安装 Python、ngspice 或配置任何环境变量
- 首次使用：点右上角 ⚙ 模型，填任意 OpenAI 兼容供应商的 Base URL / API Key /
  模型名（默认 DeepSeek）。Key 只保存在本包目录的 settings.json，不回传
- 产物（网表/波形/原理图）与会话记录全部保存在包目录 out/ 下——
  拷走整个文件夹就是完整归档；换电脑 = 拷贝文件夹
- 再次双击 start.bat 可继续使用；关闭黑窗口即停止服务
- 同题对照演示页：服务启动后访问 http://127.0.0.1:8000/compare.html

离线自检（可选，验证仿真链路，不需要 API Key）：

    py\\python.exe tests\\test_offline.py

本包内含：Python 3.12 运行时（PSF License）、ngspice 47（BSD License）及
Python 依赖库（FastAPI/uvicorn/OpenAI SDK/spyci/matplotlib/numpy/schemdraw/
skidl，均为 MIT/BSD/Apache/PSF 系协议），详见 NOTICE。VoltProof 本体协议见
LICENSE（Apache-2.0）。
"""

_NOTICE = """\
# NOTICE — 本便携包内含的第三方组件

| 组件 | 协议 | 说明 |
|---|---|---|
| Python 3.12（embeddable） | PSF License | https://www.python.org |
| ngspice 47 | BSD 3-Clause | https://ngspice.sourceforge.io |
| fastapi / starlette | MIT | |
| uvicorn | BSD 3-Clause | |
| openai (SDK) | Apache-2.0 | |
| spyci | MIT | |
| matplotlib | PSF License (matplotlib) | |
| numpy | BSD 3-Clause | |
| schemdraw | MIT | |
| skidl | MIT | |

VoltProof 本体：Apache-2.0（见 LICENSE）。
"""


def _download(url: str, dest: Path) -> Path:
    if dest.exists():
        print(f"  已缓存 {dest.name}")
        return dest
    print(f"  下载 {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, dest)
    return dest


def _locate_ngspice(cli_arg: str | None) -> Path:
    """定位并校验 ngspice-XX/Spice64 目录。拒绝含 ".." 的输入，resolve
    规范化后必须含 bin/ngspice.exe；不合法直接退出。"""
    raw = cli_arg
    if raw is None:
        import os
        env = os.environ.get("VOLTPROOF_NGSPICE", "")
        if env and Path(env).is_file():
            raw = str(Path(env).parent.parent)
        else:
            found = shutil.which("ngspice")
            if found:
                raw = str(Path(found).parent.parent)
    if not raw:
        sys.exit("请用 --ngspice 指向 ngspice-XX/Spice64 目录（内含 bin/ngspice.exe）")
    if ".." in Path(raw).parts:
        sys.exit(f"--ngspice 不接受含 .. 的路径：{raw}")
    src = Path(raw).resolve()
    if not src.is_dir() or not (src / "bin" / "ngspice.exe").is_file():
        sys.exit(f"--ngspice 目录不合法（需内含 bin/ngspice.exe）：{src}")
    return src


def step_python() -> None:
    z = _download(PY_EMBED_URL, DL / Path(PY_EMBED_URL).name)
    print("解压 embeddable Python …")
    shutil.rmtree(APPDIR / "py", ignore_errors=True)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(APPDIR / "py")
    # ._pth 控制整个 sys.path：启用 site、挂上依赖目录与包根（import app）
    (APPDIR / "py" / "python312._pth").write_text(
        "python312.zip\n.\nLib\\site-packages\n..\n\nimport site\n",
        encoding="ascii")
    # embeddable 自带 vcruntime140*.dll；万一缺失从本机构建机补齐
    for dll in ("vcruntime140.dll", "vcruntime140_1.dll"):
        dst = APPDIR / "py" / dll
        if not dst.exists():
            cand = Path(sys.base_prefix) / dll
            if cand.is_file():
                shutil.copy2(cand, dst)


def step_deps() -> None:
    gp = _download(GET_PIP_URL, DL / "get-pip.py")
    print("bootstrap pip …")
    r = subprocess.run([str(PYEXE), str(gp), "--no-warn-script-location"],
                       shell=False, cwd=APPDIR, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"get-pip 失败：{r.stderr[-500:]}")
    # 部分依赖只发源码包（如 spyci），装依赖前先备好构建后端
    print("安装 setuptools/wheel（源码包构建用）…")
    r = subprocess.run([str(PYEXE), "-m", "pip", "install",
                        "--no-warn-script-location", "--prefer-binary",
                        "setuptools", "wheel"],
                       shell=False, cwd=APPDIR, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"setuptools 安装失败：{r.stderr[-500:]}")
    print("安装依赖（pip install -r requirements.txt）…")
    r = subprocess.run([str(PYEXE), "-m", "pip", "install",
                        "--no-warn-script-location", "--prefer-binary",
                        "-r", str(REPO / "requirements.txt")],
                       shell=False, cwd=APPDIR)
    if r.returncode != 0:
        sys.exit("pip install 失败，见上方输出")


def step_app() -> None:
    print("复制应用文件 …")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc",
                                    "_llm_settings_test_*")
    # static/ 已并入 app 包内随包分发；此处只拷 app/tests/bench
    for d in ("app", "tests"):
        shutil.copytree(REPO / d, APPDIR / d, ignore=ignore,
                        dirs_exist_ok=True)
    (APPDIR / "bench").mkdir(exist_ok=True)
    for pattern in ("*.py", "*.json"):
        for f in (REPO / "bench").glob(pattern):
            shutil.copy2(f, APPDIR / "bench" / f.name)
    shutil.copy2(REPO / "scripts" / "portable_run.py", APPDIR / "portable_run.py")
    shutil.copy2(REPO / "scripts" / "start.bat", APPDIR / "start.bat")
    shutil.copy2(REPO / "LICENSE", APPDIR / "LICENSE")
    shutil.copy2(REPO / "README.md", APPDIR / "README.md")
    (APPDIR / "便携版说明.md").write_text(_PORTABLE_README, encoding="utf-8")
    (APPDIR / "NOTICE").write_text(_NOTICE, encoding="utf-8")


def step_ngspice(src: Path) -> None:
    print(f"复制 ngspice（{src}）…")
    dst = APPDIR / "ngspice"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst,
                    ignore=shutil.ignore_patterns("docs", "examples"),
                    dirs_exist_ok=True)


def step_zip() -> Path:
    out = DIST / "VoltProof-win64-portable.zip"
    print("打包 zip …")
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(APPDIR.rglob("*")):
            zf.write(p, p.relative_to(DIST))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ngspice", default=None,
                    help="ngspice-XX/Spice64 目录（默认取 VOLTPROOF_NGSPICE 的上级或 PATH）")
    src = _locate_ngspice(ap.parse_args().ngspice)
    print(f"ngspice 源：{src}")

    APPDIR.mkdir(parents=True, exist_ok=True)
    step_python()
    step_deps()
    step_app()
    step_ngspice(src)

    # 冒烟：内嵌解释器自检（ssl/site 可用）+ 依赖导入 + 离线测试一套
    print("冒烟验证 …")
    r = subprocess.run([str(PYEXE), "-c",
                        "import ssl, sqlite3, numpy, matplotlib, schemdraw, "
                        "skidl, fastapi, uvicorn, openai, spyci; "
                        "print('  依赖导入 OK')"],
                       shell=False, cwd=APPDIR)
    if r.returncode != 0:
        sys.exit("便携包依赖导入失败，见上方输出")
    r = subprocess.run([str(PYEXE), "tests/test_offline.py"],
                       shell=False, cwd=APPDIR)
    if r.returncode != 0:
        sys.exit("便携包离线自检失败，见上方输出——不产出 zip")

    z = step_zip()
    mb = z.stat().st_size / 1e6
    print(f"\n完成：\n  {APPDIR}  （解压即用）\n  {z}  （{mb:.0f} MB）")


if __name__ == "__main__":
    main()
