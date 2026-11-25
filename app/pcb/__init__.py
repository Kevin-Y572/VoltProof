"""PCB 集成模块（阶段 A：外部工具适配层）。

职责边界（docs/PCB_UPGRADE_PLAN_2026-10-23.md §5.3）：
- 本包只做"子进程 + 文件交换"的工具适配：KiCad（pcbnew Python / kicad-cli）
  负责板数据与检查，Freerouting（JVM JAR）负责自动布线。
- 不在本包内重新实现布线或 DRC 算法；GPL 工具只以独立进程 + DSN/SES
  文件交换接入，无代码链接（许可证红线见调研笔记 §6）。
- 工具缺失时本包不可用但主程序（仿真/画板/报告）不受影响。

阶段 A 验证结论（2026-10-23，docs/PCB_PHASE_A.md）：
- kicad-cli 无 DSN 导出/SES 导入——交换必须走带 pcbnew 的 Python
  （Windows：bin/python.exe；Linux：发行版 python3 或前缀内的 python3）
  的模块级函数 ExportSpecctraDSN/ImportSpecctraSES。
- Freerouting 只可用 JVM JAR：v2.5.0 原生 CLI 存在崩溃+挂死缺陷（上游 #957/#966）。
- Freerouting 退出码不可作成败判据（内部超时+部分输出也返回 0），
  成败判定 = 日志解析 + SES 存在性 + 后续独立 DRC 复核。
"""
