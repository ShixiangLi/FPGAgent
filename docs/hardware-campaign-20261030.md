# 三轮真实上板测试报告

三轮均使用真实 DeepSeek 生成工程，并执行 XSIM、Python 波形绘图、Vivado 构建、独立上下文审查、JTAG 下载、VIO 回读与硬件 Python 分析。每轮 CSV 另由测试执行者编写的独立检查器复核，并验证生成的分析脚本能拒绝被篡改的数据。

板卡：AXU15EGB，器件 xczu15eg-ffvb1156-2-i；Vivado 2024.2；模型 deepseek-flash，thinking=true，effort=high，max_tokens=32768。

需求和设计确认由 Codex 根据用户授权作为测试执行者完成；记录中包含需求/方案修改过程。后两轮在确认点主动暂停并续接，随后并行准备工程；下载由测试执行者在测试驱动中串行放行，实际调用原有 deploy 流程。RTL、testbench、分析脚本均由模型生成及修复，测试执行者没有手工替换这些文件。

| 轮次 | 功能 | 实际采样 | 跨度 | 自动硬件结果 |
|---|---|---:|---:|---|
| 1 | 500ms双灯交替 | 61 | 6569 ms | PASS |
| 2 | 250ms格雷码计数 | 81 | 6689 ms | PASS |
| 3 | 20kHz双路PWM及硬件周期统计 | 41 | 4385 ms | PASS |

## 实测发现与修正

第二轮第一次已实际编程，但采样失败：Vivado 将 VIO 端口0的2位总线拆成 binary_count（位0）和 gray_count_1（位1），原脚本假定每端口只有一个完整探针而报 VIO probe mismatch。这是框架缺陷。通过真实 get_hw_probes/report_property 定位后，新增 templates/vio.tcl 按 PROBE_PORT_BITS 和 WIDTH 拼接片段，校验完整位覆盖、重叠、越界及无效值；generic_program.tcl 使用同一读取实现，工程生成和环境检查同步包含该辅助脚本。新增6项 Tcl 回归，全套108项测试通过。真实板卡先行验证12个拼接样本后，三轮均从原模型草稿重新仿真、构建、审查、下载，最终结果均使用修正后的模板。

第三轮还发生一次模型生成截断，恢复机制先要求保存短检查点，再逐步生成分析脚本；首次仿真因模型遗漏 testbench 的 done 输出端口失败，模型读取原始 XSIM 错误后自动修复。历史失败日志保留，不以最终通过掩盖中间错误。

## 每轮证据

### 1. 500ms双灯交替

任务 `20261030-222003-86a39e`；源码摘要 `81f3a36a2800aa1ad0df72d8f240386d101d985c6abe4aa832657e59975e53b0`。

VIO 值域：`{"phase": [0, 1], "led1_n": [0, 1], "led2_n": [0, 1]}`。变化次数：`{"phase": 13, "led1_n": 13, "led2_n": 13}`。

时序估算：`{"observed_half_period_interval_ms": [492.8333333333333, 511.0833333333333]}`。周期区间来自采样边界；帧率来自片上计数与主机时间戳，非示波器测量。

独立检查：[independent-audit.json](../.validation/board-campaign-1793369985925809800/case01/independent-audit.json)；真实采样：[hardware_samples.csv](../.validation/board-campaign-1793369985925809800/case01/20261030-222003-86a39e/runs/program-1793372037041589400/hardware_samples.csv)；任务报告：[report.md](../.validation/board-campaign-1793369985925809800/case01/20261030-222003-86a39e/report.md)。

- python：[waveform.png](../.validation/board-campaign-1793369985925809800/case01/20261030-222003-86a39e/artifacts/python-c355a3cb2367/waveform.png)，[waveform.svg](../.validation/board-campaign-1793369985925809800/case01/20261030-222003-86a39e/artifacts/python-c355a3cb2367/waveform.svg)，[waveform_summary.json](../.validation/board-campaign-1793369985925809800/case01/20261030-222003-86a39e/artifacts/python-c355a3cb2367/waveform_summary.json)
- hardware_analysis：[hardware_plot.png](../.validation/board-campaign-1793369985925809800/case01/20261030-222003-86a39e/artifacts/hardware_analysis-2002f731ec56/hardware_plot.png)，[hardware_report.md](../.validation/board-campaign-1793369985925809800/case01/20261030-222003-86a39e/artifacts/hardware_analysis-2002f731ec56/hardware_report.md)

构建时序：`{"wns_ns": 3.076, "whs_ns": 0.005, "wpws_ns": 1.968}`。负向分析退出码 `1`（应非零）。

模型调用 17 次，累计完成 token 139465，模型请求耗时合计 762.02 秒；JSON 格式修复 0 次，截断 0 次。

规划接口退回 1 次，编辑接口错误 1 次，真实工具失败 0 次。

- 编辑反馈：文件不在当前任务可写范围内：sim/tb_top_led_alternate.sv

### 2. 250ms格雷码计数

任务 `20261030-222132-91c782`；源码摘要 `9470a2df6195c621c8311fb27932e35f8be70cefab5641e6d32e962604b6bb2f`。

VIO 值域：`{"binary_count": [0, 1, 2, 3], "gray_count": [0, 1, 2, 3], "led1_n": [0, 1], "led2_n": [0, 1]}`。变化次数：`{"binary_count": 27, "gray_count": 27, "led1_n": 13, "led2_n": 14}`。

时序估算：`{"observed_step_interval_ms": [247.69230769230768, 253.84615384615384]}`。周期区间来自采样边界；帧率来自片上计数与主机时间戳，非示波器测量。

独立检查：[independent-audit.json](../.validation/board-campaign-1793369985925809800/case02/independent-audit.json)；真实采样：[hardware_samples.csv](../.validation/board-campaign-1793369985925809800/case02/20261030-222132-91c782/runs/program-1793371958599072200/hardware_samples.csv)；任务报告：[report.md](../.validation/board-campaign-1793369985925809800/case02/20261030-222132-91c782/report.md)。

- python：[waveform.png](../.validation/board-campaign-1793369985925809800/case02/20261030-222132-91c782/artifacts/python-f47d730bd6d2/waveform.png)，[waveform.svg](../.validation/board-campaign-1793369985925809800/case02/20261030-222132-91c782/artifacts/python-f47d730bd6d2/waveform.svg)，[waveform_summary.json](../.validation/board-campaign-1793369985925809800/case02/20261030-222132-91c782/artifacts/python-f47d730bd6d2/waveform_summary.json)
- hardware_analysis：[hardware_plot.png](../.validation/board-campaign-1793369985925809800/case02/20261030-222132-91c782/artifacts/hardware_analysis-dd45a851abc0/hardware_plot.png)，[hardware_report.md](../.validation/board-campaign-1793369985925809800/case02/20261030-222132-91c782/artifacts/hardware_analysis-dd45a851abc0/hardware_report.md)

构建时序：`{"wns_ns": 3.151, "whs_ns": 0.009, "wpws_ns": 1.968}`。负向分析退出码 `1`（应非零）。

模型调用 11 次，累计完成 token 75511，模型请求耗时合计 456.52 秒；JSON 格式修复 0 次，截断 0 次。

规划接口退回 0 次，编辑接口错误 0 次，真实工具失败 1 次。

- 失败证据：program，[program-1793371152421123700](../.validation/board-campaign-1793369985925809800/case02/20261030-222132-91c782/runs/program-1793371152421123700)

### 3. 20kHz双路PWM及硬件周期统计

任务 `20261030-222132-cce283`；源码摘要 `5839848e9045087215a5f05ca6b3110ef4ab483d72aad556b7e06cf3b901ad1a`。

VIO 值域：`{"measured_on1": [2500], "measured_on2": [7500], "frame_number": {"min": 1133, "max": 64365, "distinct": 41}}`。变化次数：`{"measured_on1": 0, "measured_on2": 0, "frame_number": 40}`。

时序估算：`{"frame_rate_estimate_hz_from_host_timestamps": 19998.403648802738}`。周期区间来自采样边界；帧率来自片上计数与主机时间戳，非示波器测量。

独立检查：[independent-audit.json](../.validation/board-campaign-1793369985925809800/case03/independent-audit.json)；真实采样：[hardware_samples.csv](../.validation/board-campaign-1793369985925809800/case03/20261030-222132-cce283/runs/program-1793372098187030700/hardware_samples.csv)；任务报告：[report.md](../.validation/board-campaign-1793369985925809800/case03/20261030-222132-cce283/report.md)。

- python：[waveform.png](../.validation/board-campaign-1793369985925809800/case03/20261030-222132-cce283/artifacts/python-af284a1779b4/waveform.png)，[waveform.svg](../.validation/board-campaign-1793369985925809800/case03/20261030-222132-cce283/artifacts/python-af284a1779b4/waveform.svg)，[waveform_summary.json](../.validation/board-campaign-1793369985925809800/case03/20261030-222132-cce283/artifacts/python-af284a1779b4/waveform_summary.json)
- hardware_analysis：[hardware_plot.png](../.validation/board-campaign-1793369985925809800/case03/20261030-222132-cce283/artifacts/hardware_analysis-550493a5aaa8/hardware_plot.png)，[hardware_report.md](../.validation/board-campaign-1793369985925809800/case03/20261030-222132-cce283/artifacts/hardware_analysis-550493a5aaa8/hardware_report.md)

构建时序：`{"wns_ns": 3.086, "whs_ns": 0.022, "wpws_ns": 1.968}`。负向分析退出码 `2`（应非零）。

模型调用 27 次，累计完成 token 178573，模型请求耗时合计 1004.64 秒；JSON 格式修复 1 次，截断 1 次。

规划接口退回 2 次，编辑接口错误 0 次，真实工具失败 1 次。

- 失败证据：simulation，[simulation-1793371068876909900](../.validation/board-campaign-1793369985925809800/case03/20261030-222132-cce283/runs/simulation-1793371068876909900)

## 验证范围与复现

三轮最后均停在 awaiting_acceptance：自动硬件验证已通过，未代替用户作现场目视验收。VIO 观察到的逻辑状态和片上 PWM 统计不等于示波器测量或对 LED 实际发光的观察。最后一轮 PWM 设计保留在 PL 中，本次没有写入非易失存储。

完整机器可读记录：[summary.json](../.validation/board-campaign-1793369985925809800/summary.json)；测试原始需求：[manifest.json](../.validation/board-campaign-1793369985925809800/manifest.json)。CSV、历史日志、每轮最终 bitstream/LTX 和图像保留在 `.validation/board-campaign-1793369985925809800`。

复查任一轮的原始数据和负向检查：

```powershell
.\.venv\Scripts\python.exe -X utf8 .validation/audit_hardware_campaign.py 1
```

将末尾轮次改为 2 或 3。该命令只分析已有证据，不重新下载。重新开发可从 manifest.json 中取对应自然语言需求，通过 main.py run --request 输入，按原流程确认需求与方案。
