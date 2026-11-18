<div align="center">

# VoltProof

用真实的 ngspice 仿真，验证 AI 给出的电路答案。

[English](README.md) · [简体中文](README.zh-CN.md)

[![Version 0.4.0](https://img.shields.io/badge/version-0.4.0-0ea5e9)](https://github.com/Kevin-Y572/VoltProof)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)](https://www.python.org/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

<img src="docs/demo.gif" alt="在工作台加载内置 RC 低通示例并运行 ngspice 仿真" width="90%">

</div>

VoltProof 是一个本地工作台，用真实的 [ngspice](https://ngspice.sourceforge.io/) 仿真来检查 AI 给出的电路。你可以用自然语言描述电路，也可以在画布上画。VoltProof 会写出 SPICE 网表，跑一遍 ngspice，并给出波形、测得的数据和原理图。如果结果没达到目标，它可以把误差送回模型再试一次。

## 特性

- 把 AI 的每份回答都交给 ngspice 运行，并查看波形和测得的数据
- 检查或仿真失败时，可以把误差送回模型再试一次
- 无需密钥即可在画布上画图并仿真，也可以在对话里用自然语言描述电路
- 对某个元件取一组值，逐点重新仿真
- 导出独立的 HTML 实验报告，并可打印为 PDF
- 支持任何 OpenAI 兼容的服务，包括本机 Ollama；密钥只保存在你的电脑上

## 安装

需要 Python 3.10 或更高版本，以及 [ngspice](https://ngspice.sourceforge.io/)。请将 ngspice 加入系统 `PATH`，或用 `VOLTPROOF_NGSPICE` 指定可执行文件路径。

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)，点「示例」，再点「仿真」，就能看到一个 RC 低通的真实波形。这一步不用配置模型密钥。

工作台默认是简体中文。顶栏的「中文 / English」可以切换界面语言，选择会被记住。

也可以先执行 `pip install .`，再运行 `voltproof`。

| 页面 | 地址 |
| --- | --- |
| 工作台（画布 + 对话） | http://127.0.0.1:8000 |
| 独立画板 | http://127.0.0.1:8000/draw.html |
| 同题对照 | http://127.0.0.1:8000/compare.html |

## 使用方法

**画图并仿真。** 在左侧画布上放置元件、连线，选择直流、瞬态或交流分析，然后点「仿真」。悬空引脚和缺少接地会在画布上标出。你可以导出 `.cir` 网表。电路会随项目保存。

**让模型来设计。** 在对话里直接说明需求，例如：「设计桥式整流加电容滤波电路，220 V / 50 Hz 降压到 12 V，纹波小于 1 V。」后续追问会基于上一份已验证的网表继续改。也可以在画布下方的输入栏里提出修改，结果会先预览，确认后才应用。

只有使用 AI 设计时才需要模型密钥。设置 `VOLTPROOF_API_KEY`（或 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`），也可以在页面的模型设置里填写。

**参数扫描。** 对某个元件取一组值，逐点做真实仿真。

**实验报告。** 可以把一次对话导出为独立的 HTML 报告，再打印成 PDF。

## MCP

VoltProof 自带 stdio MCP 服务，提供两个工具：`design_circuit`（设计并验证）和 `verify_netlist`（按原样验证网表；若设置 `repair=true` 则允许修复）。

```json
{
  "mcpServers": {
    "voltproof": {
      "command": "python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "<VoltProof 仓库根目录>"
    }
  }
}
```

如果是用 pip 安装的，可以把 `"command"` 写成 `"voltproof-mcp"`。

## 补充说明

如果 ngspice 还不在 `PATH` 里：

```bash
# Windows（setx 只对之后新开的终端生效）
setx VOLTPROOF_NGSPICE "C:\path\to\ngspice-47\Spice64\bin\ngspice.exe"

# macOS / Linux
export VOLTPROOF_NGSPICE=/usr/bin/ngspice
```

命令行用法：

```bash
python -m app.pipeline "设计一个截止频率 1 kHz 的 RC 低通滤波器"
```

PCB 导出是实验性的可选功能，需要 KiCad 10.x 和 Freerouting。没有它们也不影响仿真。

VoltProof 按本地单用户工具来设计，请绑定 `127.0.0.1`。不要在未加鉴权和隔离的情况下把它暴露到公网。

## 许可证

[Apache-2.0](LICENSE)
