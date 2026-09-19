# CircuitPilot —— AI 电路仿真 Copilot

自然语言需求 → AI 生成电路网表 → **ngspice 真实仿真验证** → 波形 + 实测指标 + AI 解读。

> 直接问 AI 得到的是"关于电路的文字"，这里给出的是"被仿真验证过的电路"。

配套文档：
- 《AI电路仿真助手-实施路线图.md》（上级目录）—— 阶段规划与逐周任务
- 《AI电路仿真助手-头脑风暴与开源调研.md》（上级目录）—— 开源底座调研与协议合规

## 快速开始

```bash
# 1. 安装 ngspice（Windows：SourceForge 下载 ngspice-XX_64.7z 解压，
#    不需要进 PATH，设环境变量指向 ngspice.exe 即可）
setx CIRCUITPILOT_NGSPICE "D:\Users\Lenovo\tools\ngspice-47\Spice64\bin\ngspice.exe"

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置模型（OpenAI 兼容接口，默认 DeepSeek）
setx CIRCUITPILOT_API_KEY 你的key         # Git Bash 会话内用 export（setx 对已开终端不生效）
setx CIRCUITPILOT_MODEL deepseek-chat      # 可选，默认即此
setx CIRCUITPILOT_BASE_URL https://api.deepseek.com   # 可选

# 4. 离线自检（不需要 API Key，验证仿真链路/检查器/重试循环/接口契约）
python tests/test_offline.py
python tests/test_pipeline_mock.py

# 5. 命令行跑通管道（W1 里程碑，需要 API Key）
python -m app.pipeline "设计一个截止频率1kHz的RC低通滤波器"

# 6. 启动 Web 界面（W3 里程碑）
uvicorn app.main:app --reload
# 浏览器打开 http://127.0.0.1:8000
# 同题对照演示页：http://127.0.0.1:8000/compare.html
```

环境变量一览：

| 变量 | 必填 | 说明 |
|---|---|---|
| `CIRCUITPILOT_API_KEY` | 是 | OpenAI 兼容 API Key（也接受 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`） |
| `CIRCUITPILOT_NGSPICE` | 是(Windows) | ngspice.exe 完整路径；已在 PATH 中可省略 |
| `CIRCUITPILOT_BASE_URL` | 否 | 默认 DeepSeek `https://api.deepseek.com` |
| `CIRCUITPILOT_MODEL` | 否 | 默认 `deepseek-chat` |

## 实测经验（Windows + ngspice-47，踩坑记录）

- ngspice batch 模式（`-b`）仿真失败时**退出码仍是 0、stdout 为空**，报错只写进
  `-o` 日志文件，且失败电路照样产出全零 raw——成败判定必须扫日志致命标记
  （见 `ngspice_runner._FATAL_MARKERS`）。
- spyci 1.0.2 只解析 **ASCII** raw：网表 `.control` 块第一行必须 `set filetype=ascii`；
  其 `load_raw` 在 `spyci.spyci` 子模块，且用了 NumPy 2 已移除的 `np.complex_`（已垫片）。
- LLM 生成的 schemdraw 代码在受限子进程执行（AST 白名单 + `-I` 隔离 + 超时），
  失败自动降级为显示网表。

## 目录结构与开发周次对应

```
app/
├── llm.py             # OpenAI 兼容封装            W1
├── prompts.py         # 生成/修复/解读提示词         W1-W2 持续迭代
├── ngspice_runner.py  # 子进程调 ngspice + 超时      W1
├── measure.py         # raw 解析 + 指标计算          W1
├── render_wave.py     # 波形 PNG                    W1
├── checks.py          # 网表静态检查                 W2
├── pipeline.py        # 编排：生成→检查→仿真→重试→证据 W1串联/W2重试/W3会话
├── render_schematic.py# schemdraw 电路图（LLM生成代码）W4（可裁剪）
└── main.py            # FastAPI + 静态托管           W3
bench/
├── tasks.json         # 10 个基准任务                W2 使用
└── run_bench.py       # 跑批统计通过率               W2
static/
├── index.html         # 对话页（证据卡片）            W3
└── compare.html       # 同题对照演示页               W4（差异化演示，优先级最高）
tests/
├── test_offline.py        # 离线冒烟：仿真/解析/波形/检查器（22 断言）
└── test_pipeline_mock.py  # mock LLM：重试循环/会话/API/电路图沙箱（21 断言）
```

## Phase 0 验收标准（见路线图 2.5）

- 10 个基准任务 100% 走通闭环（含自动重试）——实测 10/10，平均 8s/任务（bench/report.md）
- 重试后仿真通过率 ≥90% ——实测 100%（DeepSeek deepseek-chat）
- 端到端 ≤60 秒（实测最长 12.7s）；同题对照页浏览器实测通过
- 待办：3 分钟录屏 + 演讲稿

## 演示截图（浏览器实测，2026-09-19）

| 对话页初始状态 | 证据卡片（波形+指标+原理图+解读） | 同题对照页 |
|---|---|---|
| ![初始](docs/gui-test/t1_chat_initial.png) | ![证据卡](docs/gui-test/t2_evidence_card.png) | ![对照](docs/gui-test/t3_compare_page.png) |

## 协议

- 本项目代码：Apache-2.0（Phase 1 开源发布时正式挂出）
- 依赖均为宽松协议：ngspice(BSD)/spyci(MIT)/schemdraw(MIT)/FastAPI(MIT)/matplotlib(PSF)
- **不使用** PySpice、spicelib（GPL 绑核），ngspice 一律子进程调用
