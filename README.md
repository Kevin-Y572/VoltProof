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
setx CIRCUITPILOT_MODEL deepseek-flash     # 可选，默认即 deepseek-flash
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

## 双轨后端（SKiDL vs 裸 SPICE 网表）

`backend="spice"`（默认）：LLM 直接写 SPICE 网表。
`backend="skidl"`：LLM 写 [SKiDL](https://github.com/devbisme/skidl)（MIT）的 Python 电路代码，
沙箱执行后生成网表——连接显式、无网表方言陷阱（浮空/单位/续行），`.control`
块由构建器按 print 协议固定拼接，`set filetype=ascii`/`write` 不再依赖 LLM。

```bash
python bench/run_bench.py --backend=skidl   # 基准（报告 bench/report_skidl.md）
python bench/run_exp.py --backend=skidl     # 综合实验（证据 bench/exp_skidl/）
```

同日 A/B（2026-09-19，DeepSeek）：bench 通过率 spice 9/10 vs skidl 9/10（打平，
唯二失败均为 square-osc 方差）；综合实验谐波分析两轨均达理论值
（a3/a1=0.330≈1/3、a5/a1=0.193≈0.2）。协议合规：实测 skidl 2.3.0 的
`skidl.pyspice` 原语不加载 PySpice（GPL）。

## 安全边界（2026-09-21 审查后）

已防御（均有 PoC 回归测试，见 tests/test_offline.py "安全" 断言）：
- **ngspice 系统命令**：`.control` 内 `shell/system/alias/...` 一律拒绝；
  `.include/.lib` 只允许相对路径（防任意文件读写）
- **沙箱逃逸**：AST 拦截 dunder 逃逸链（`__subclasses__`/`__globals__` 等，
  曾实测从"沙箱"读出环境变量 API Key）；子进程最小环境**不携带任何
  凭据**（纵深防御：即使出现新逃逸路径也偷不到 Key）
- **上传限制**：附件 ≤256KB（前后端双重）、正文 ≤8KB

**仍存在的边界（公网部署前必须处理）**：
- `/chat` 无认证无速率限制——公网部署等于开放你的 API Key 给任何人刷量；
  部署时必须加反向代理认证/限流，或仅监听 127.0.0.1
- LLM 生成代码的沙箱是应用层的（AST+最小环境），非 OS 级隔离；严肃部署
  应加容器/AppContainer 隔离
- 间接提示注入（网表注释里藏指令诱导 LLM）理论上可影响生成内容，但生成
  结果仍受静态检查/仿真器/沙箱三重约束，无法直接执行危险操作

## 原理图：确定性自动布局（2026-09 升级）

原路线"LLM 生成 schemdraw 代码"在复杂电路上布局崩坏（坐标靠语言模型想象）。
现改为确定性管线：**网表 → 二部图 → graphviz dot 布局（rankdir=LR，
地沉底/电源置顶）→ schemdraw 按坐标渲染**，LLM 退出画图环节。

- `app/schematic_layout.py`：`render_netlist_schematic(netlist, png)`；
  两端元件 `.at(netA).to(netB)` 精确落位，多端元件（Q/M/X）画 IC 方框+
  引脚连线，拓扑 100% 正确；dot 不可用时回退纯 Python 网格布局
- pipeline 优先确定性渲染，失败才回退 LLM 老路（省一次 LLM 调用）
- graphviz 是 EPL-1.0，子进程调用（与 ngspice 同哲学），无协议传染

环境变量一览：

| 变量 | 必填 | 说明 |
|---|---|---|
| `CIRCUITPILOT_API_KEY` | 是 | OpenAI 兼容 API Key（也接受 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`） |
| `CIRCUITPILOT_NGSPICE` | 是(Windows) | ngspice.exe 完整路径；已在 PATH 中可省略 |
| `CIRCUITPILOT_BASE_URL` | 否 | 默认 DeepSeek `https://api.deepseek.com` |
| `CIRCUITPILOT_MODEL` | 否 | 默认 `deepseek-flash`（推理型：思考放 reasoning_content，重任务思考 1-13 分钟，谐波类精度极佳；换 `deepseek-v4-pro` 走高端档） |
| `CIRCUITPILOT_LLM_TIMEOUT` | 否 | 单次调用超时秒数，默认 480 |
| `CIRCUITPILOT_LLM_RETRIES` | 否 | SDK 对超时/断连/429/5xx 的自动退避重试次数，默认 2 |
| `CIRCUITPILOT_THINKING` | 否 | 思考模式总开关 on/off，默认 on；调用处可用 `thinking=` 参数按次覆盖 |
| `CIRCUITPILOT_REASONING_EFFORT` | 否 | none/low/high/max（别名 minimal/medium/xhigh 按官方映射表归一），默认不传（服务端 high） |

## LLM 调用模块（app/llm.py，对齐 DeepSeek 官方文档）

- **思考模式**：官方默认开启（effort=high），`chat(..., thinking=False)` 按次关闭；
  思考开启时不下发 temperature——官方文档明确思考模式下 temperature 被静默忽略。
  解读、修复环重启等轻量/求快调用已按次关闭思考。
- **JSON 模式**：`chat_json()` 启用 `response_format=json_object`，自动补官方要求的
  prompt "json" 字样、空 content 自动重试、`finish_reason=length` 报截断错。
- **流式**：`chat_stream()` 按 delta 分流累计 reasoning_content 与 content，
  `include_usage` 取回末块用量（服务于 W3 的 SSE 演示 TODO）。
- **错误翻译**：401/402/422/429/500/503 按官方错误码表翻译成中文 `LLMError`；
  429=账号级并发超限（flash 2500 / pro 500），传输类错误由 SDK 指数退避重试。
- **用量观测**：`llm.last_usage` 记录 `prompt_cache_hit_tokens` 等（上下文硬盘缓存
  命中价约为未命中 1/50；system 固定在最前以稳定前缀提高命中）。

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
