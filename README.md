# DeepSeek FPGA 工程智能体

输入自然语言需求，智能体先更新需求文档与方案，再生成完整多文件工程，执行自检仿真、Python波形绘图、按需综合实现与上板。执行目标由当前需求和模型计划决定，不再由启动开关固定。

当前使用顺序增量开发：先拆分小任务，任务内通过检查点和片段修改分轮构建工作草稿，最终交付1至3个文件后继续下一项；集成失败时只修改相关文件。支持仿真前Python生成系数等数据。详见[增量开发与恢复](docs/incremental-development.md)。

需求、规划和审查支持分轮保存草案；模型超限后保存为 `model_paused`，可继续原阶段。审查续接复用源码未变时的工具证据。支持用户显式设置 `--max-tokens` 和 `--effort`，配置不会自动变化，见[模型生成与阶段续接](docs/generation-recovery.md)。

系统提示词及通用模型指令集中维护在 [`fpga_agent/prompts/catalog.py`](fpga_agent/prompts/catalog.py)，职责与维护方式见[提示词说明](fpga_agent/prompts/README.md)。核心模块保留流程和动态数据组装。

已有 CSV/VCD 的分析问题可使用 `main.py analyze`：由 DeepSeek 自行读取、修复指定分析脚本、执行并判断结果，支持提问与恢复；不修改原 RTL 或重新下载。见[模型驱动分析与实际验证记录](docs/model-directed-analysis.md)。

方案在用户确认前按[统一执行接口](docs/execution-contract.md)检查并反馈模型修正；规划和工程组装共享规则。已接通VIO采样后的Python分析阶段，可声明实际采样时长、分析脚本和必需报告产物。

开发前有两个用户确认点：先确认需求分析，再确认设计与测试方案。输入 `y` 或 `确认` 继续，输入修改意见让模型重新整理，输入 `/pause` 暂停；空行不会提交，会继续等待。需求未确认不生成方案，方案未确认不生成代码。确认记录绑定内容摘要，修改后受影响的确认需重新进行。当前测试方案保存在任务目录的 `test_plan.md`。

模型按 `config.json` 选择，支持 `deepseek-v4-pro` 和 `deepseek-flash`，开启 `thinking`，默认 `reasoning_effort=high`。Python 3.10+，绘图使用 matplotlib，PDF读取使用 PyMuPDF；安装依赖：` .\.venv\Scripts\python.exe -m pip install -r requirements.txt `。

## 开始一个新任务

```powershell
.\.venv\Scripts\python.exe main.py doctor --llm
.\.venv\Scripts\python.exe main.py run
```

第二条命令会询问需求。也可以直接指定需求：

```powershell
.\.venv\Scripts\python.exe main.py run --request "实现PWM调光，使用板载LED1，100Hz，25%占空比，PL_KEY复位，生成完整工程和自检测试"
```

完整工程比单文件需要更多生成时间。`--effort` 始终由用户选择，省略则使用配置中的 `high`；框架不自动降低思考强度或增加输出预算。开发循环通过小步暂存、累计摘要和按需compact续接；截断后先保存不含代码的拆解检查点，下一轮以选定的小步骤为目标。读取文件不会清除恢复状态。截断内容不应用，连续截断或无进展会保存为 `development_paused`，可用 `--resume <任务编号> --continue` 继续原任务。

自然语言明确要求下载或上板验证时，计划设置 `program=true`，审查通过后自动下载。不要求下载时不自动安排下载；`--program` 只是初始意图提示，后续用户回答可以覆盖：

```powershell
.\.venv\Scripts\python.exe main.py run --request "实现按键去抖计数器，按一次PL_KEY递增，两个PL LED显示低两位，上电清零" --program
```

也可先检查生成的工程，再单独下载：

```powershell
.\.venv\Scripts\python.exe main.py program <任务编号>
```

下载为易失 PL 配置，不写 QSPI/eMMC。下载成功不自动等于应用功能验证成功；有配置的 VIO 探针规则会实际回读，其他情况需要按任务验收条件现场确认。

## 纯逻辑或协议仿真

没有外接硬件时可直接开发 UART、SPI、FIFO、状态机等内部数字逻辑：

```powershell
.\.venv\Scripts\python.exe main.py run --target sim --request "实现参数化UART发送器，100MHz时钟，1Mbps，8N1低位先发，busy时忽略start，生成独立逐位检查的自检测试"
```

`--target sim` 是初始目标提示。用户明确“只仿真、不上板”时，模型计划使用 sim，不综合、不下载，也不要求物理引脚。用户后续要求优先于初始提示。

## 中途与智能体交互

关键需求不清楚时，智能体会提问并等待终端回答。空行会被忽略；输入 `/pause` 明确暂停。非交互模式或终端输入关闭也会保存并暂停，原因记录在events.jsonl。暂停后可用 `--resume <任务编号> --continue` 再次展示问题并回答。

之后补充回答，或对已有任务提出新修改：

```powershell
.\.venv\Scripts\python.exe main.py run --resume <任务编号> --request "改为50%占空比，其他要求保持不变"
```

续接时读取当前任务文档，按最新回答更新计划，不必重复设置 sim。新需求使旧验证与下载资格失效；旧需求/方案保存在 history，旧工程候选和工具日志保留。完整聊天另行保存，不反复全部发送给模型。

当前支持阶段边界上的问答和续接，不支持在 Vivado 正在运行时热修改需求；可 Ctrl+C 终止所属进程，再用 `--resume` 提出修改。旧版固定流水灯任务仍可 program/accept，修改需求请新建通用任务。

批处理使用 `--non-interactive`，遇到问题会保存为 `waiting_for_user` 并退出，不会猜答案。

非交互运行遇到确认点会保存为 `awaiting_confirmation` 并以退出码2暂停，不默认同意。之后使用 `run --resume <任务编号> --continue` 在终端确认。修改需求或方案也可用 `--resume ... --request "修改意见"`；这会重新整理内容并进入确认流程。

```powershell
.\.venv\Scripts\python.exe main.py status <任务编号>
.\.venv\Scripts\python.exe main.py diagnose <失败任务编号>
.\.venv\Scripts\python.exe main.py verify <任务编号> --effort low
.\.venv\Scripts\python.exe main.py accept <任务编号> --passed --note "按任务验收条件现场测试正常"
```

现场异常用 `--failed`。`completed` 才表示已记录现场验收。退出码0也可能只是 `simulation_verified`、`ready_to_program` 或 `awaiting_acceptance`；2为等待输入，1失败，130中断。

`development_paused` 表示开发小步达到本批执行上限，退出码为2；与等待澄清不同，`--continue` 直接恢复工作检查点，不先要求回答一个新问题。暂停原因及下一步见 `working.md`。

`verify` 对当前源码重新仿真、构建和审查，不要求模型重新生成代码，也不自动修复。若某轮模型候选因框架兼容性检查失败，可用 `verify <任务编号> --candidate <任务内candidate文件相对路径>` 重新验证该快照，不能改变任务规格。

## 搜索与解读参考资料

需求分析与方案规划可以调用通用只读资料工具：搜索网页、下载来源、检索PDF/文本、按页阅读，以及将PDF图表页面交给视觉模型解读。资料可包括板卡/器件手册、协议规范、工具文档、论文、算法说明、参考代码和测试数据。工具返回真实结果或异常类型与消息。

将资料放到 `references/`（可分子目录），或按任务提供文件/URL；仍会发现 `boards/` 中的PDF：

```powershell
.\.venv\Scripts\python.exe main.py run --target sim --source "D:\references\protocol.pdf" --source "D:\references\vectors.csv" --request "根据协议资料实现接收器，自检仿真并绘制波形；版本或时序不清楚先提问，不上板"
```

研究笔记写入任务的 `research.md`，供后续设计和代码生成读取；来源快照、分页文本及原始工具结果另行保存。完整说明见[通用资料研究](docs/research.md)。

新板卡或补充资源先运行资料准备任务，智能体自行选择查询步骤，给出附原文引用的候选配置：

```powershell
.\.venv\Scripts\python.exe main.py board-research --board axu15egb --request "读取AXU15EGB手册，核对完整器件型号、差分时钟、PL_KEY及两个PL LED，标注来源页码；型号或版本冲突先提问"
```

终端支持提问、修改与确认。候选配置只有在用户明确确认后才安装到 `boards/<id>.json`，已有配置会在任务目录备份。新任务使用 `run --board <id>` 选择已安装配置，续接沿用原任务板卡。普通开发中的资料查询不会覆盖配置。

详细命令、恢复方式、来源产物和能力边界见[资料搜索与板卡准备](docs/board-research.md)。

## 初版能力边界

- 支持多文件 Verilog/SystemVerilog，可自选模块名、接口、参数、测试入口，不再固定 led_runner。
- 已核实板卡为 AXU15EGB；`boards/axu15egb.json` 注册差分200MHz时钟、PL_KEY和两个PL LED。
- PWM、计数器、按键去抖、数字状态机等可利用现有板载资源构建完整上板工程。
- UART/SPI等核心可以直接仿真；要连接物理接口，须先在板卡配置中补充经核实的引脚、电气标准和方向。智能体会询问，不会猜引脚。
- PS软件、VHDL、DDR/高速收发器、第三方或复杂厂商IP目前不支持。简单时钟原语由生成的RTL实例化，VIO可由框架注入。
- 模型不能直接执行任意shell或生成Tcl；板卡配置变更通过资料准备和用户确认。框架检查文件路径及执行参数，不扫描HDL关键字或系统函数；这不是针对恶意代码的安全沙箱。

## 文件结构

```text
main.py                       命令行与用户交互
config.json                   模型、工具和输出配置
boards/axu15egb.json           已核实的板卡资源，不是应用模板
fpga_agent/
  agent.py                    DeepSeek需求理解、生成、修复、独立上下文审查
  project.py                  多文件校验、工程组装、约束和仿真驱动生成
  workflow.py                 状态、冻结规格、阶段控制、续接和报告
  documents.py                搜索、来源快照、分页PDF读取和页面图像解读
  research.py                 通用资料发现、工具动作、结果反馈和研究笔记
  board_research.py           复用通用资料工具，准备并确认安装板卡配置
references/                   协议、器件、工具等参考资料，可按主题分目录
  tools.py                    工具进程、验证结果、摘要与下载门槛
templates/generic_*.tcl        通用仿真、构建与下载执行器
workspace/<任务编号>/
  state.json                  需求、对话、状态、版本和结果
  draft.json                  已最终交付的文件与任务记录
  editing/<摘要>.json         当前任务的暂存修改、累计摘要、读取窗口和续接信息
  working.md                  当前开发小步、下一步、暂存文件和实际反馈
  revisions/<版本>/candidate-*.json  每一轮模型工程输出与修复，续接不覆盖历史
  design_response.json         最近一次需求阶段响应
  llm_calls.jsonl              模型调用用量和思考是否返回，不保存思考原文
  report.md                   执行与验收报告
  project/
    project.json              顶层、测试、引脚绑定、调试及板卡快照
    spec.json                 冻结的需求与验收标准
    rtl/                      模型生成的多文件RTL，按需注入VIO
    sim/                      模型测试与框架agent_sim结束/超时监控
    pins/board.xdc            根据资源绑定生成的约束
    scripts/                  通用Tcl及控制器生成的project_data.tcl
    README.md                 模型生成的工程说明（若提供）
    build/vivado/design.bit   构建产物；启用VIO时另有design.ltx
  runs/                       原始日志、各轮审查与结构化结果
```

新任务不读取 `projects/led`；该历史示例可以归档，`config.project_dir`仅供旧基准测试使用。

## 验证和修复机制

1. DeepSeek返回结构化规格与完整工程。框架只检查组装工程所需的数据格式、文件路径和板卡绑定；HDL语法与端口连接由真实编译工具处理，测试充分性由DeepSeek审查。响应正文保存在任务目录的 `llm-response-*.txt`（不保存思考内容）；JSON解析失败会携带行列错误请求模型修正，最多两次，不应用残缺工程。

审查分阶段：`pre_program_review` 只决定是否允许进入下载，`approved` 仍由 DeepSeek 判断。当前代码、测试或实现中的具体阻塞问题放入 `blocking_findings`；非阻塞意见放入 `warnings`；尚未执行的 JTAG 下载、VIO 回读及现场验收放入 `pending_checks`，不能因为这些步骤尚未执行而否决下载前审查。缩短参数仿真应结合真实默认参数、位宽与终值的静态分析判断。`target=sim` 只审查仿真范围。审查批准不代表硬件验证成功，实际硬件错误和用户验收仍分别记录。
2. testbench必须有 `output done`，用断言或 `$fatal` 自检，完成后置done；不能调用 `$finish`。框架生成唯一的通过标志和超时监控。
3. XSIM编译全部RTL和测试，并运行自检；失败时不会构建。清理并隔离上一轮日志，避免将旧通过记录当作本轮证据。
4. build目标继续综合实现，检查实际顶层端口和绑定一致、方向一致、建立/保持/脉宽、内部时序覆盖、DRC，以及启用调试时的总线skew。纯仿真目标跳过构建。
5. 在新的模型上下文中，结合真实工具结果核对原始需求、冻结规格、RTL和测试，要求对每条acceptance给出具体覆盖或验证路径。审查不通过则修复，不发放下载资格。纯仿真到此结束。
6. 自动修复最多两次，重新提交完整工程，不能改变已冻结spec。生成文件和工具脚本的摘要关联验证结果；任何修改均使旧结果不可用于下载。
7. 上板下载只接受已验证bit/LTX摘要，按配置采集VIO。没有探针规则时仅能确认下载成功，不声称自动功能验收通过。
8. 最后由用户现场验收。模型生成测试和同模型独立上下文审查不能代替独立工程评审、形式验证、外部仪器或可靠性验证。

## 配置与排障

在项目根目录 `.env` 配置 `DEEPSEEK_API_KEY`，同名环境变量优先。不要提交密钥。`.env`支持简单KEY=value和独立注释行，不支持插值或行尾注释。

`config.json`配置Vivado位置、DeepSeek基础URL、max_tokens、请求超时、工具超时和最大修复次数。模型固定deepseek-v4-pro，开启思考，不自动切换模型。使用JSON输出而非模型工具调用API，因此无需回传reasoning_content。

`llm.proxy_mode` 为 `direct` 时仅本程序直连API，忽略环境和系统代理；为 `system` 时使用Python检测到的环境/系统代理。当前设为direct，避免本机代理端口关闭导致WinError 10061；不修改Windows代理设置。

需求、代码和诊断日志会发送到配置的模型服务。官方参数依据：[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。

网络错误显示具体原因；硬件错误由模型解释后停止。任务和下载均有锁，系统强制终止后如有残留，确认进程已结束再删除对应锁文件。

流式接收只显示思考/生成进度，不输出或保存原始思考文本；请求、工具调用和本地文件状态均保留可复核记录。

## 回归测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用mock模型与工具，不消耗API、不下载。通用真实集成验证使用 `main.py run`，同时检查模型响应、实际生成文件、审查报告和工具结果。
