# Linux 安装、启动与 PCB 制板回归

VoltProof 在 Linux 上正式支持工作台、独立画板、同题对照和 ngspice 仿真。PCB 导出仍是实验功能；本机装齐下面写明的工具后，整条制板链会实际跑完，而不是停在预检。

这些检查不是操作系统级沙箱。网表和模型生成的 Python 只拒绝危险命令、越界路径和 dunder 属性访问。

## 1. 安装并启动 VoltProof

Debian / Ubuntu：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ngspice
git clone https://github.com/Kevin-Y572/VoltProof.git
cd VoltProof
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开：

| 页面 | 地址 |
| --- | --- |
| 工作台 | http://127.0.0.1:8000 |
| 独立画板 | http://127.0.0.1:8000/draw.html |
| 同题对照 | http://127.0.0.1:8000/compare.html |

点「示例」，再点「仿真」，应看到 RC 低通的真实波形。这一步不需要模型密钥，也不需要 KiCad。

ngspice 不在默认路径时：

```bash
export VOLTPROOF_NGSPICE=/usr/bin/ngspice
```

## 2. 构建并安装 .deb

脚本：`scripts/build_linux_deb.py`。在要安装的机器上运行，Python 次版本须与安装目标一致。需要 `python3`、`python3-pip`、`dpkg-deb`，以及能访问 PyPI 的网络。脚本本身不用 root。

```bash
python3 scripts/build_linux_deb.py
sudo apt install ./dist/voltproof_*.deb
voltproof
```

包内有：

- `/opt/voltproof/lib`：应用和 Python 依赖
- `/usr/bin/voltproof`：启动器
- 桌面入口（终端方式，便于看到地址并用 Ctrl+C 停止）

包依赖系统的 `ngspice` 和 `python3`。不包含 KiCad、Java 或 Freerouting。

安装后的数据目录是 `~/.local/share/voltproof`，模型设置是 `~/.config/voltproof/settings.json`。可选工具路径写在 `~/.config/voltproof/env`，例如：

```bash
VOLTPROOF_KICAD=/usr
VOLTPROOF_FREEROUTING=/opt/freerouting/freerouting-2.1.0.jar
VOLTPROOF_JAVA=/usr/bin/java
VOLTPROOF_NGSPICE=/usr/bin/ngspice
```

`VOLTPROOF_KICAD`、`VOLTPROOF_FREEROUTING`、`VOLTPROOF_JAVA` 三个覆盖仍然有效。设了但路径不对时，不会悄悄改用另一份安装。

## 3. 真实制板回归要用的工具

在 Ubuntu 22.04 / 24.04（以及能使用同一 PPA 的 Debian 派生系统）上，按下面这一组安装。版本不要换：KiCad 用 10.0.x，Java 用 21，Freerouting 用 **2.1.0 的 JVM JAR**。

| 工具 | 测试用的版本 | 原因 |
| --- | --- | --- |
| KiCad | 10.0.8（`ppa:kicad/kicad-10.0-releases` 的 10.0 稳定线，随 PPA 更新仍应为 10.0.x） | 制板链按 KiCad 10 的板文件和 DRC JSON 编写 |
| Java | OpenJDK 21（`openjdk-21-jre-headless`） | Freerouting JVM JAR 的最低版本 |
| Freerouting | [`freerouting-2.1.0.jar`](https://github.com/freerouting/freerouting/releases/download/v2.1.0/freerouting-2.1.0.jar) | 2.2 及之后的 JAR 按 Java 25 构建；2.5.0 的原生 CLI 会崩溃并挂死，不要用 `freerouting-*-linux-x64.zip` 或 AppImage |
| 封装库 | `kicad-footprints` | 造板要读 `/usr/share/kicad/footprints` |
| ngspice | 发行版的 `ngspice` 包 | 仿真用，与制板无关 |

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:kicad/kicad-10.0-releases
sudo apt update
sudo apt install -y --install-recommends kicad kicad-footprints
sudo apt install -y openjdk-21-jre-headless

sudo mkdir -p /opt/freerouting
sudo curl -L -o /opt/freerouting/freerouting-2.1.0.jar \
  https://github.com/freerouting/freerouting/releases/download/v2.1.0/freerouting-2.1.0.jar
```

纯 Debian（不是 Ubuntu）如果加不上这个 PPA，请安装 KiCad 10.0.x 的发行版包，并确认下面的检查仍然通过。Flatpak 版 KiCad 不会被自动发现。

装好后做这几项检查。任何一项失败就不要开始制板回归：

```bash
kicad-cli version
# 期望：10.0 开头，例如 10.0.8

/usr/bin/python3 -c "import pcbnew; print(getattr(pcbnew, 'GetBuildVersion', pcbnew.Version)())"
# 期望：能导入，版本与 kicad-cli 同属 10.0.x

java -version
# 期望：openjdk version "21...."

test -s /opt/freerouting/freerouting-2.1.0.jar && echo jar-ok
ls /usr/share/kicad/footprints >/dev/null && echo footprints-ok
ngspice --version
```

环境变量（桌面启动则写进 `~/.config/voltproof/env`）：

```bash
export VOLTPROOF_FREEROUTING=/opt/freerouting/freerouting-2.1.0.jar
# KiCad 装在 /usr 时不必设置。其它前缀：
# export VOLTPROOF_KICAD=/usr
# export VOLTPROOF_JAVA=/usr/bin/java
```

在仓库里跑一条不经过浏览器的冒烟（缺工具时退出码 2）：

```bash
python3 scripts/pcb_smoke.py
```

在界面里：打开工作台，加载示例并仿真，进入 PCB 页。KiCad、Freerouting、Java 三项都应为就绪。预检通过后「生成 PCB」才可点。生成结果应走过 mapping、board、routing、import、check、fabrication，并得到 Gerber、钻孔和 BOM。

Windows 上同一条链仍然可用：KiCad 10.x 的 `bin/kicad-cli.exe` 与 `bin/python.exe`，Java 21+，以及同一个 `freerouting-2.1.0.jar`（用 `VOLTPROOF_FREEROUTING` 指向它）。
