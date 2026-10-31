# PCB 阶段 A 结果：接口验证与全链路样例

日期：2026-10-23。对应 `PCB_UPGRADE_PLAN_2026-10-23.md` §5.2 / §12（阶段 A：技术验证）。
工具版本（实测锁定）：KiCad 10.0.7 + Freerouting v2.5.0（JVM JAR，Build-Revision b647a2f）+ Java 25。

## 结论

方案 §5.2 的第一周技术闭环**已在本机全部实测通过**，并已固化为可复现脚本：
`python scripts/pcb_smoke.py`（RC 低通贯穿案例，方案 §11）。
最近一次运行六步全过、总耗时约 14 秒，档案（板/DSN/SES/DRC JSON/制板文件/manifest
含哈希与各步耗时）落 `pcb/runs/<id>/`。

## 能力矩阵（方案六问逐条回答）

| 方案的问题 | 实测答案 |
|---|---|
| 自动创建/保存/重开板文件并保留焊盘网络？ | 可以。`app/pcb/kicad_backend.build_board` 纯代码造板（pcbnew Python），SaveBoard/LoadBoard 往返保留网络 |
| DSN/SES 交换保留层/线宽/过孔等？ | 本案例（2 层、铺铜、过孔 0）往返保真；禁布区/锁定对象未构造用例，阶段 B 补 |
| 超时/取消后可靠终止并保留原板？ | 是。Freerouting 无内部取消，看门狗强杀（`router_backend.route` 的 timeout_s），实测无残留、原板文件全程只读 |
| 中文/带空格路径？ | Freerouting 全链路通过；KiCad 侧待补测 |
| 需要保持 KiCad 编辑器打开？ | 不需要。全程子进程 + 文件交换，无 GUI、无 IPC 桥接 |
| 工具缺失/版本不匹配有明确诊断？ | `find_kicad()`/`find_freerouting()` 探测 + `pcb_smoke.py` 退出码 2 带修复指引；Freerouting 崩溃型失败由日志标记识别（不依赖退出码） |

## 关键技术事实（设计据此定型）

1. **kicad-cli 没有 DSN 导出/SES 导入**（`pcb {drc,export,import,render,upgrade}`）。
   路由交换唯一路径：KiCad 自带 python 的模块级函数
   `pcbnew.ExportSpecctraDSN(board, path)` / `pcbnew.ImportSpecctraSES(board, path)`。
   铺铜回填同理（`pcbnew.ZONE_FILLER`）。DRC JSON 的未连项键名为 `unconnected_items`。
2. **Freerouting 只可用 JVM JAR**：v2.5.0 原生 CLI（GraalVM）序列化白名单缺失，
   真实板布线一启动即崩且挂死不退出（上游 #957，修复 #966 未随版本发布）。
3. **Freerouting 退出码不可作判据**（内部超时+部分结果也返回 0）：成败 =
   日志解析（`finished with state: COMPLETED` + final 行的 unrouted/violations）+ SES
   存在性 + 回导后独立 DRC 复核。
4. **隔离参数**：每任务独立 `--user_data_path`（默认写共享目录且带锁）、`-da`
   （默认启动外呼遥测/版本检查）、`-l en`（日志锚点稳定）、`-Xmx` 限堆。
5. KiCad 免安装可用：7z 解包官方安装器即得完整便携树（bin/kicad-cli.exe 与带
   pcbnew 的 python 开箱即用）；NSIS `/S` 静默装需 UAC。便携分发路径已验证。
6. 结果可复现：Freerouting 固定种子，同输入同配置逐轮输出一致；冒烟档案含
   SHA-256 哈希可复核。

## 落库内容

- `app/pcb/kicad_backend.py`——KiCad 适配：定位/造板/DSN/SES/铺铜/DRC JSON/制板导出；
  板规格白名单校验（位号/网络/封装名防注入进生成脚本）。
- `app/pcb/router_backend.py`——Freerouting 适配：JAR 定位、看门狗、隔离参数、
  日志解析、崩溃标记识别。
- `scripts/pcb_smoke.py`——RC 贯穿案例全链路 + manifest 归档（阶段 A 交付物本体）。
- `tests/test_pcb.py`——离线单元（日志解析/规格校验/探测）+ 工具在场时的集成链路
  （缺工具自动跳过，退出码 0——PCB 为可选能力）；已接入 `scripts/test_isolated.py`。
- 工具定位：`VOLTPROOF_KICAD`（KiCad 根目录）、`VOLTPROOF_FREEROUTING`（JAR）、
  `VOLTPROOF_JAVA`（可选，默认 PATH 上的 java）。

## 遗留（转阶段 B 输入）

- DSN/SES 往返对锁定走线/禁布区/异形封装的保真（需构造用例）。
- KiCad 侧中文路径实测。
- 网表（VoltProof 电路）→ 板规格（CircuitIR）映射：阶段 B 的
  `app/circuit_ir.py` 与审校器件库，属方案 §4 范畴。
- 任务层（jobs/取消/进度 SSE）与版本绑定：方案 §6.2 的 `pcb/jobs.py` 等，待阶段 B/C。
