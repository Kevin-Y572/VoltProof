# PCB 验收缺陷修复记录（回应 PCB_ACCEPTANCE_2026-10-30）

修复完成日期：2026-10-30。验收报告的全部 7 项缺陷（F01–F07）、布局静态缺陷
与"跳过项误报 PASS"问题均已修复，反例已按报告要求转换为正确行为的回归断言。

> **10-31 收尾补记**：上节"未处理事项"中的四项亦已完成——
> ① 器件库扩到 **22 条**（tht/smd 双安装方式：0805 R/C/LED、SOT-23 三极管
> MMBT3904/3906——同一型号两种安装对应**不同脚位表**，按 mount 选项选条目；
> 1N5819、8mm 电解、M3 安装孔、LM358 SOIC）+ `options.mount` 全链
> （映射/jobs/preflight/placement/页面下拉框）；
> ② 板选项：尺寸（只放大保庭院间距）/四角 M3 安装孔/矩形禁布区（规则区域，
> 双铜层）贯穿 spec→造板→API→页面（尺寸输入框/安装孔勾选）；
> ③ 网页交互补齐：端口可从全部网络手工增删（输出 out 一键加端子）、选中
> 封装旋转 ±90°/锁定（strip 重布线保留锁定走线）、点焊盘高亮同网络；
> ④ `scripts/pcb_bench.py`：30 个固定案例**逐板真实布线**统计（成功率/
> 中位/P95 耗时，档案 pcb/runs/bench-*），CI 改跑 test_isolated 六件套 +
> `scripts/check_wheel.py`（19 项必需文件本地验证通过，含器件库与固定案例集）。

## 逐项修复

### F01 引脚身份级一致性检查（P1）
- `connectivity.py` 重写：对照保持**引脚身份**（SPICE 引脚号 → 器件引脚功能 →
  焊盘号），不再退化成"每元件每网络几个脚"——交换二极管 A/K、晶体管 C/E、
  连接器脚号现在必然失败（回归断言覆盖二极管双级）。
- 两层证据：`check_partition`（源电路 ↔ 板规格）+ **`board_partition_with_map`
  （源电路 ↔ 实际板文件**，从 `preview_from_pcb` 读真实焊盘）。任务检查
  （jobs.s_check）与回导复核（api.recheck）都改走实际板文件——DRC 零违规
  不再被当作一致性的替身。
- `build_board` 按**焊盘号**（`pad.GetNumber()`）赋网，不再依赖
  `zip(fp.Pads(), pads)` 的封装内部遍历序。

### F02 网络重命名碰撞（P1）
- `_safe_rename`：安全名避开既有网络名与已分配名（原案例 `1→N2` 与既有
  `N2` 碰撞、把 R1 两脚并到一个网络——现在 `1→N1`）。
- 单射/无捕获守卫：两个不同原始网络映射同目标（非地别名）、或重命名目标
  撞上未改名网络 → AssertionError 拒绝生成 IR。

### F03 仿真元件静默丢失（P1）
- 电流源（I）从无人处理的 `probe` 角色改为显式 `blocked`（转换指引：
  端子激励或诺顿等效+重仿真）。
- 0V 检测源识别重写：`0`、`DC 0`（含大小写）、`0V`、工程后缀数值全覆盖
  （原实现只看第 4 个 token，`DC 0` 的值 token 是 "DC" 直接绕过）。
- 角色三分法定案：device / stimulus / blocked 之外无静默路径。

### F04 检查-导出冻结绑定（P1）
- 导出验证链：状态必须 `checked`（复核失败会降级 `recheck_failed`）→
  当前采用指针匹配 → **板文件哈希与检查时一致**（被改 → 409 要求回导再检）→
  产物完整性（缺 .gbrjob/.drl/BOM.csv → 422 列明缺失）→ 源电路过期
  （默认 409；显式 `history=true` 导出并标记 `status: "historical"`）。
- `recheck` 结论回写版本记录（`update_version_status`），失败不再保持
  checked 继续放行导出。
- 导出返回逐文件 SHA-256 清单。

### F05 制造规则贯通（P1）
- KiCad 10 实测结论：python 侧 BDS 字段/网络类赋值**不随 SaveBoard 持久化**
  （保留名 Default 被序列化丢弃、m_TrackMinWidth 仅内存生效、(min_thickness)
  是遗留 token 不进 DRC）——约束的唯一持久化位置是伴生 `.kicad_pro` 的
  `board.design_settings.rules`。
- `write_project_rules`：线宽/净距/过孔最小值 + 尺寸表写入工程文件，
  `kicad-cli pcb drc` 直接以它为判据。**真实复验**：track_min=1.0mm 的板
  上塞 0.25mm 走线 → DRC 报 6 条 track_width 错误（回归断言入库）。
- 默认规则对齐 KiCad 默认布线宽度（0.2/0.2/0.6·0.3）保证标准链绿；
  自定义更严规则真实生效。
- DRC 去掉 `--severity-error`：错误/警告分级计数 + 警告明细保留（任务
  manifest 与检查 JSON 均含 warnings 列表）。

### F06 版本记录与双重修订保护（P1）
- 电气哈希重写：完整元件行（含全部值/参数写法，`DC 5→DC 50`、
  `SIN(0 1 1k)→(0 2 2k)` 均变化）+ `.model/.subckt/.include/.lib/.param`
  定义（模型参数变化可测）；仍与注释/分析语句/空白无关。
- 映射哈希含每个条目的**引脚表与封装内容**（改 pad_functions/footprint
  即失效），不再只有条目 ID。
- 版本记录新增 `source_version_id`（任务提交时的项目电路修订号）。
- `adopt` 双重校验：①指针 revision（前端采用现带 `expected_revision`）；
  ②源电路电气哈希 vs 项目当前已验证网表——不一致即 409
  （"旧电路的板不可采用为当前电路的制板结果"）。

### F07 拖拽坐标语义（P2）
- `preview_from_pcb` 输出**封装原点**（origins）与焊盘号。
- pcb.js 拖拽重写：基准=封装原点（含已提交位移，支持连续拖动），焊盘按
  相对原点的原始位移**刚性平移**（不再塌叠到同一点），提交坐标即原点。
- 修正版探针（probe_drag_fixed.cjs）验证：原点误差 0.00mm、焊盘不塌叠、
  连续拖动累积正确。

### 布局静态缺陷（signal_layers 传播死锁）
- 原"器件等全部网络定层才入层，而新网络只能由已定层器件赋予"互相等待。
- 重写为波次推进 + 网层冻结 + 自反馈排除（net→dev→同 net 二部环会使
  max/min 双向松弛振荡发散——两次实测教训）。

### 跳过项误报
- 测试框架新增 `skip()`：集成用例工具缺失时打 `[SKIP]` 独立计数，
  不再混入 PASS。

## 测试与证据

- `tests/test_pcb.py::test_acceptance_regressions`：10 项后端反例全部转为
  正确行为断言 + F05 真实 DRC 执行断言（工具在场），全部通过。
- 修正版前端探针 `.test-results/pcb-acceptance-20261029/probe_drag_fixed.cjs`
  → `FIXED_BEHAVIOR_CONFIRMED`（旧探针按报告要求由回归断言取代）。
- 真实工具链全套 137 项 PASS（含板级对照、规则执行、中文路径、锁定走线
  保真、PUT placement/recheck）。
- 六件套隔离回归全绿（见 .test-results 同名日志）。

## 验收报告未在本轮处理的事项（如实声明）

- ~~审校器件库扩到计划规模（20–30 项 + 0805/SOIC 等 SMD 条目）~~ ✅ 10-31 完成（22 条）。
- ~~网页端封装确认界面、旋转/锁定按钮、网络高亮、板尺寸/安装孔/禁布区设置；
  输出端口手工添加入口~~ ✅ 10-31 完成（映射报告即封装确认单，页面按引脚表展示）。
- ~~30 个支持案例的逐板真实布线成功率/耗时统计~~ ✅ 10-31 完成
  （`scripts/pcb_bench.py`，见 pcb/runs/bench-*/report.md；结果随机器负载波动，
  首轮全量 30/30 通过、单案例中位 ~40s 冷启动口径）。
- ~~CI 纳入 PCB 测试与 wheel/Windows 发布包检查~~ ✅ 10-31 完成（workflow 更新；
  凭据仍缺 workflow scope 未推送——历史遗留，与代码无关）。
- LM358 实体化贯通流程（理想运放→补供电新候选重验）仍是阻断指引 + 参考条目；
  Windows 便携发布包整体重打包验证未做（wheel 内容检查已覆盖分发面）。
