# CircuitPilot —— AI 电路仿真 Copilot

自然语言需求 → AI 生成电路网表 → **ngspice 真实仿真验证** → 波形 + 实测指标 + AI 解读。

> 直接问 AI 得到的是"关于电路的文字"，这里给出的是"被仿真验证过的电路"。

配套文档：
- 《AI电路仿真助手-实施路线图.md》（上级目录）—— 阶段规划与逐周任务
- 《AI电路仿真助手-头脑风暴与开源调研.md》（上级目录）—— 开源底座调研与协议合规

## 快速开始

```bash
# 1. 安装 ngspice（Windows：官网下载安装包并加入 PATH，验证：ngspice --version）
# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置模型（OpenAI 兼容接口，默认指向智谱 GLM）
set CIRCUITPILOT_API_KEY=你的key        # Windows CMD（Git Bash 用 export）
set CIRCUITPILOT_MODEL=glm-4-flash      # 可选，默认即此

# 4. 命令行跑通管道（W1 里程碑）
python -m app.pipeline "设计一个截止频率1kHz的RC低通滤波器"

# 5. 启动 Web 界面（W3 里程碑）
uvicorn app.main:app --reload
# 浏览器打开 http://127.0.0.1:8000
# 同题对照演示页：http://127.0.0.1:8000/compare.html
```

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
```

## Phase 0 验收标准（见路线图 2.5）

- 10 个基准任务 100% 走通闭环（含自动重试）
- 重试后仿真通过率 ≥90%（保底 80%）
- 端到端 ≤60 秒；同题对照页可现场运行

## 协议

- 本项目代码：Apache-2.0（Phase 1 开源发布时正式挂出）
- 依赖均为宽松协议：ngspice(BSD)/spyci(MIT)/schemdraw(MIT)/FastAPI(MIT)/matplotlib(PSF)
- **不使用** PySpice、spicelib（GPL 绑核），ngspice 一律子进程调用
