# 规划与执行的统一接口

本次调整针对“规划承诺与实际执行能力不一致”，不增加HDL正则语义检查，也不单独放行某个XDC文件名。

## 单一规则来源

`fpga_agent/contract.py` 集中定义任务文件路径、需求格式、任务清单、工程元数据、板卡绑定、VIO采样及Python脚本接口。规划阶段和工程组装复用这些规则；`development.py` 保留原导入接口，不再维护另一套路径规则。

`contract.describe()` 将相同规则及工具职责提供给模型。XDC/Tcl、VIO注入、VCD和正常仿真结束由框架负责。用户工程交付RTL、testbench、Python、数据和README。

## 确认前的反馈循环

流程现在为：需求分析与用户确认 → 生成方案 → 检查执行接口 → 错误反馈模型修正 → 用户确认可执行方案 → 开发。

检查仅判断计划是否能被工具执行，不判断算法或RTL正确性。模块名、输出路径、任务依赖、时钟绑定等接口问题不再到开发时才退出。每轮原计划和错误保存在 `planning/*.json`，实际能力快照在 `execution_contract.json`。默认最多修正3次，仍不满足则停止并保留证据；不会自动删除不支持的需求或视为用户同意。

方案修正后仍必须重新确认；原需求保持冻结，模型需要改变需求时应提问。旧任务通过 `--resume <id> --continue` 时也先执行此检查。

## 硬件采样后的Python分析

需要此阶段时，在project元数据中声明：

```json
{
  "hardware_analysis": {
    "script": "scripts/analyze_hw.py",
    "outputs": ["hardware_plot.png", "hardware_report.md"]
  }
}
```

脚本必须出现在开发任务的输出中；计划需要program=true及有效debug配置。框架在本次下载、VIO采样均成功后调用：

```text
python scripts/analyze_hw.py --input <本次program运行的CSV副本> --output <本次分析产物目录>
```

CSV的elapsed_ms列是十进制毫秒时间戳，其余探针列为十六进制。脚本应根据真实数据检查业务需求，用异常或非零退出报告失败，并生成所有声明产物。框架检查执行结果、产物和来源，不替代脚本的业务判断。

仿真与硬件分析共享同一Python进程执行、超时、日志及产物收集逻辑。硬件分析不会误用project/build中的旧CSV。脚本失败、输入缺失或产物不完整都使任务进入hardware_failed，不会标记验收成功。

VIO可声明debug.min_duration_ms。例如至少采样10秒，可设置sample_ms=100、samples=101、min_duration_ms=10000。规划先检查计划跨度，执行后再按实际时间戳核对跨度。现有采样数量、间隔上限由统一接口描述提供，超出范围需要调整并确认方案。

## 验证证据与验收

仿真后Python结果现在直接传给集成审查，不只藏在进度文档里。硬件分析产物保存在artifacts/hardware_analysis-*，结果记录在runs及state.json中。

硬件分析后，终端提示用户核对自动验证报告。接受结果记录为user_review_of_automated_evidence，不记为目视观察；未配置硬件分析的原有现场验收流程保留。内部回读不能证明LED实际发光。

## 本次本地验证

回归覆盖：错误计划在确认前修正、多项接口错误反馈、冻结需求不被修改、修正上限、声明和实际采样跨度、真实Python读取当前CSV、非零退出、缺少产物、下载失败不执行分析、远程验收来源。

另运行了真实XSIM→VCD→Python波形链路。硬件分析回归使用明确构造的CSV，不是实际板卡采样。本次没有调用DeepSeek执行完整开发，也未操作板卡。
