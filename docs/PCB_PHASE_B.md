# PCB 阶段 B 结果（第一块）：CircuitIR 与审校器件库

日期：2026-10-23。对应 `PCB_UPGRADE_PLAN_2026-10-23.md` §4（P0：统一电路数据与
实物映射）与 §9 P0 前两行。前置：阶段 A（见 PCB_PHASE_A.md）。

> **收官补记（同日晚）**：方案计划内任务至此全部完成——在第五块之后又补齐
> ①BOM 导出（fabrication 阶段落 `BOM.csv`，位号/数值/封装合并、未供料号标注）；
> ②SSE 进度端点 `GET /jobs/{id}/events`（前端 EventSource 优先、轮询兜底）；
> ③`PUT /versions/{id}/placement` 布局调整（清旧走线→移动封装→重布线→检查，
> 产生带 `parent_version_id` 链的新候选，不改当前采用板）+ 预览拖拽交互
> （焊盘按位号整件拖动 → 「重布线（新候选）」）；④`POST /recheck`（KiCad 修改
> 板后重跑 DRC+分区对照，附板哈希前后对照）；⑤**§10 固定测试集**：
> `app/pcb/tests_data/pcb_cases.json` 30 支持案例（映射+分区对照+规格校验三重
> 断言）+ 15 异常案例（按原因关键词断言阻断）。
> 实测修复：circuit_ir 0V 阻断原因曾不进 block_reason；异常阻断回退丢失一次；
> `subprocess` Windows 偶发 stdout=None 已防御。

## 结论

**"验证过的网表 → 制板文件"闭环打通**：`pcb_smoke.py` 第二案例以 RC 低通网表
为源，经 CircuitIR 角色分类 + 审校器件库映射（自动布局/端子/铺铜）生成板规格，
再走阶段 A 全链（造板→DSN→Freerouting→回导→DRC 0 违规→Gerber/钻孔）。
两案例合计 ~15 秒，报告归档 `pcb/runs/<id>/`。

## 落库内容

- `app/circuit_ir.py`——网表 → 版本化 CircuitIR：元件/网络/角色。
  角色即方案 §4 规则：device（R/C/L/D/Q）、stimulus（V 源，非地网络须声明
  外部端子）、blocked（理想运放/MOSFET/受控源/0V 检测源——全部显式给原因，
  不静默丢弃）。解析复用 `checks` 的规范入口（iter_element_lines/parse_nodes），
  自行补 Q/M/X 型号字段提取。节点 0/gnd → GND 统一。
- `app/pcb/parts.py`——审校器件库访问 + `map_netlist(netlist, ports)`：
  器件条目按型号关键字/容值阈值匹配（不按类别猜脚位）；焊盘网络由
  「焊盘号→引脚功能→SPICE 位序」三段合成；映射报告（mapped/ports/blocked/
  引脚表/极性提示）随档案归档；确定性占位布局（端子靠边、器件网格，
  阶段 C 换信号流布局）。
- `app/pcb/part_catalog/catalog.json`——首批 10 条审校条目（R 0207、C 瓷片/电解、
  L 径向、1N4148/1N4007/LED、2N3904/2N3906、端子排针），含引脚表/极性/封装/
  审校来源/版本。教学模型（QNPN/QPNP/DMOD）→ 实物型号映射并强制提示额定参数核对。
- `tests/test_pcb.py` 扩展——映射离线断言 18 项 + 工具在场时
  「网表→板→DSN→布线→回导→DRC」集成。
- `scripts/pcb_smoke.py`——双案例（规格直给 + 网表映射），pyproject 补器件库打包。

## 引脚位审校要点（本批人工核对，映射正确性的根基）

| 器件 | SPICE 位序（VoltProof 网表约定） | KiCad 焊盘功能 | 极性陷阱 |
|---|---|---|---|
| 二极管/LED | A, K | pad1=K, pad2=A | 焊盘网络须反序——极性错误在这里最容易发生 |
| Q（QNPN/QPNP） | C, B, E | TO-92 pad1=E, 2=B, 3=C | 网表序与焊盘序不同 |
| 电解电容 | 约定节点1=+ | pad1=+， pad2=− | 值 ≥1uF 才映射电解；极性须人工复核 |
| 电阻/瓷片/电感 | 1, 2 | 对称 | 无 |

## 实测抓到并修复的缺陷

1. **标题行幻影元件**：SPICE 首行标题 "RC lowpass 1kHz" 被解析成电阻 RC 进了
   板子（离线测试子集断言未拦住，集成 DRC 才暴露）。修复：circuit_ir 按 SPICE
   规范跳过首行；测试断言改精确集合。
2. **自动布局右缘庭院重叠**：器件列与右缘连接器 5mm 间距触发 DRC
   courtyards_overlap。修复：板宽公式含 16mm 端子余量（两侧间距 ≥11mm）。
3. Q/M/X 型号字段：schematic_layout 的 Elem 不提取（画布层无此需求），
   circuit_ir 自行按 kind 取对应 token。

## 遗留（阶段 B 后续 / 阶段 C）

- 映射交互层：把 blockers/报告呈现给用户补齐（端子声明、型号选择）——
  当前 ports 由调用方传入；`jobs.run_pcb_job` 的 on_stage 回调已为 SSE 直连
  预留，待 main.py 接线（方案 §6.2 接口表）。
- 布局升级为信号流次序（复用 canvas_layout 的 dot 排序思想）。
- 更多审校条目（真实运放如 LM358：含供电引脚的新电路候选流程）。
- DSN→SES 往返对锁定物/禁布区的保真用例；KiCad 侧中文路径实测。

## 第二块（同日补）：版本、任务与网络一致性（§6 + §9 P0 后两行）

P0 四行至此全部落库：

- `app/pcb/schema.py`——版本记录构造/校验；**三类哈希**：电气内容哈希
  （只看元件签名——注释/激励参数/分析语句变化不判过期，§6.1 口径实测）、
  映射哈希（审校条目+库版本）、规则哈希；§6.2 状态机常量。
- `app/pcb/versions.py`——候选（不可变）/采用（revision 乐观并发，复用
  project.Conflict）/当前指针/过期判定；存 `<ws>/.cp/pcb_versions/` 与
  `pcb_current.json`，原子写。
- `app/pcb/jobs.py`——`run_pcb_job`：mapping→board→routing→import→check→
  fabrication 六阶段；映射先于工具检查（网表自身问题优先暴露）；
  协作式取消（阶段边界 cancel_check，布线由看门狗兜底）；§6.1 归档结构
  （input/routing/final/checks/fabrication + manifest）；检查通过才产
  候选版本（采用是用户动作）；on_stage 回调供 SSE 直连。
- `app/pcb/connectivity.py`——分区对照（源电路连接分区 vs 板规格焊盘分组，
  纯离线、独立于映射器的第二防线；实测能抓"两脚同网/丢地"类错网）+
  铜连接判定（路由器未布数 + DRC 未连/违规三方汇总，只认数字）。

测试新增 17 项断言（哈希语义 2、版本流 8、一致性 4、任务离线 3），
全套（六件套）隔离测试绿；冒烟案例 2 已切 jobs 通道（`pcb_smoke.py`）。

### 实测修正

- 分区对照对"对称元件两脚互换网络"不报警是**正确行为**（电气分区不变，
  脚序正确性属器件库引脚表职责）——测试用例曾想错，改为"两脚接同一网络"
  这类真分区破坏。
- 任务流程重排：映射（离线、便宜）先于工具检查，否则网表自身的阻断项
  会被"工具缺失"掩盖。

## 第五块（同日补）：信号流布局 / 报告并入 / 保真用例 / LM358（P1 清尾）

- `app/pcb/placement.py`——信号流布局替换网格占位：端口网络 BFS 分层
  （器件层 = 其非全局网络最大层 +1，多输入器件等全部来源就绪才定层），
  器件按层左→右、层内均布；端子输入左/输出右；间距沿用庭院安全值
  （横 14 / 纵 13mm）。纯确定性（同输入同布局）。悬空/纯全局网器件沉末层。
- `report.py` 项目报告增"PCB 制板记录"段：当前采用版本、状态语义
  （checked=检查通过）、工具指纹、电气内容哈希（变更→过期提示）、
  阶段表（去重）、"DRC 通过≠实物已验证"边界声明。
- 集成用例补强（全部实测过）：**中文路径全链**（中文目录+文件名：
  造板/DSN/布线/回导/DRC 五步）；**锁定走线 DSN/SES 往返保真**
  （pcbnew 加锁→Freerouting 绕开布线→回导后 `IsLocked` 保持——方案 §5.2
  的遗留问题关闭）。
- 器件库增 LM358_DIP8 参考条目（DIP-8 脚位表全录）；理想运放阻断提示
  升级为可操作指引（替换型号 + 补供电 + 新候选重验流程）。
- 测试 +18 断言（布局 8/中文路径 5/锁定保真 4/报告段隐含回归）；六件套全绿。

## 第四块（同日补）：PCB 页签与映射交互（§9 P1 前端）

- `app/static/pcb.js` / `pcb.css` + index.html 接入——工作台第三视图
  （header「🔲 PCB」宽屏 toggle；窄屏 tabbar 三选一页签，与画布/对话同机制）。
  面板流：工具链探测（三绿点+器件库条数）→ ① 预检（角色徽章 device/stimulus/
  ⛔阻断、阻断原因、端口候选自动勾选）→ ② 生成 PCB（阶段轮询：mapping→board→
  routing→import→check→fabrication 逐条 ✓/✗，取消按钮）→ ③ 版本卡
  （状态/工具指纹/采用/预览/导出——导出仅当前采用的 checked 版本可用）。
- 板预览：后端 `GET /api/pcb/preview/{vid}` 解析归档 .kicad_pcb（内置极简
  s-expression 解析器，纯文本零依赖：板框/焊盘/走线/过孔/焊盘网络名/位号），
  前端 SVG 渲染（F.Cu 正面银/B.Cu 背面金、GND 焊盘绿色、位号标注、
  悬停 title 显示 ref·net）。
- `use_project` 语义：preflight/jobs 可带 `use_project: true` 从项目当前
  **已验证**网表取源（`active_netlist(verified=True)`——画布 dirty/空项目
  一律 400："先仿真"）；PCB 永远不基于未验证的电路。
- 浏览器 E2E（真实工具链，截图存 `gui-test-pcb/`）：示例载入→画板直仿→
  PCB 页签→预检（端口自动勾选）→生成（六阶段 6.7s checked）→预览 SVG
  （DOM 几何验证：6 焊盘分布合理非堆叠）→采用（"✓ 当前采用"）全流程通过。
  期间抓到并修复：pcb.js 模板串闭合语法错（页签首次完全不加载——
  **静态 JS 交付前必须 `node --check`**，教训入册）。
- 测试 +9 断言（解析器含封装旋转坐标变换、use_project 三分支、空文件 None）；
  六件套隔离全绿。

## 第三块（同日补）：HTTP 接口接线（§6.2，P1）

- `app/pcb/api.py`——八端点挂 `/api/pcb/*`（main.py include_router，无条件
  挂载：PCB 可选，工具缺失时 capabilities 如实报告、任务类端点 400 带修复
  指引）：capabilities / preflight（角色+阻断+端口候选+工具+当前板过期）/
  jobs（后台线程 + cancel_event 协作取消 + 内存注册表轮询；进程重启后从
  归档 manifest 恢复状态）/ jobs/{id} / jobs/{id}/cancel / versions /
  apply（revision 乐观并发 → 409）/ export（只允许从当前采用的 CHECKED
  版本冻结，直接引用归档目录不重新生成）。
- 与主程序同一套约定：本地信任、`_active_ws` 全局工作区、threading.Event
  取消（chat/runs 同款）；任务结果不改 current 指针（迟到任务只留候选）。
- `run_pcb_job` 增加 `job_id` 参数（调用方 id 与归档目录名一致——曾因
  内外两个 id 不一致导致 export 找不到产物目录，实测修复）；工具缺失
  分支补 `state=FAILED`。
- 真实服务实测（uvicorn + curl）：capabilities/preflight → 提交任务
  5.7s `checked` → 采用 revision=1 → 导出 26 个制板文件，全链通过。
- 测试 +12 断言（capabilities/preflight 合法与非法/jobs 提交轮询/版本
  列表/采用冲突 409/导出 404-410 语义/mapping 无重复执行防回归），
  含工具在场与缺失两分支；六件套隔离测试全绿。
- README 增"PCB 导出（实验性）"一节（工具环境变量、冒烟命令、接口清单）。
