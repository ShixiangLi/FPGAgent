# 资料搜索与板卡准备

这版在现有顺序智能体流程上增加资料工具，没有引入多智能体、向量数据库或新的调度框架。模型选择下一步查询，Python执行工具并保存结果；资料准备、工程开发和硬件验证仍然是不同阶段。

搜索、下载和文档读取现已由通用 `research.py` 统一组织；`board_research.py` 只负责板卡配置候选、引用检查和确认安装。协议规范、数据手册、论文等资料的普通开发用法见[通用资料研究](research.md)。

## 先前失败的修正

流水灯/呼吸灯计划中的 `project.bindings` 要求“顶层端口 → 已登记资源名称字符串”。先前模型传入引脚对象，框架把类型错误错误归类为未核实硬件，导致修复方向偏离。

合法示例：

```json
{"bindings": {"led1_n": "pl_led1"}}
```

字段类型错误现在直接提供结构化接口事实：

```json
{
  "source": "framework_contract",
  "code": "invalid_type",
  "field": "project.bindings.led1_n",
  "expected": {"type": "string", "enum": ["pl_led1", "pl_led2"]},
  "actual_type": "object",
  "actual_value": {"pin": "AM13", "standard": "LVCMOS33"}
}
```

这里的枚举仅作展示，实际由当前板卡配置生成。只有字符串确实不在资源列表中才报告 `unknown_resource`。规划和工程组装共用这一规则，模型同时获得明确的字段示例；不会由框架把错误数据自动改成某个引脚。仿真和构建仍然返回原始日志、错误行与退出结果，由模型分析修改。

## 安装与开始

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py doctor
.\.venv\Scripts\python.exe main.py board-research --board axu15egb --request "读取AXU15EGB手册，核对完整器件型号、差分时钟、PL_KEY和两个PL LED；列出原文证据，遇到型号/版本冲突先提问"
```

发现 `references/` 下的参考文件和 `boards/` 中的PDF，不同内容保留不同来源编号。也可以多次指定 `--source`，提供本地PDF、文本或官方网页/PDF URL：

```powershell
.\.venv\Scripts\python.exe main.py board-research --board my_board --source "D:\manuals\board.pdf" --request "根据这份手册登记完整器件型号、时钟和LED资源"
```

`--board` 使用小写字母开头、仅含小写字母、数字和下划线的配置ID。搜索和语言模型使用项目 `.env` 中的同一 DeepSeek 密钥。搜索调用官方 `/v1/tools/search`；代理平台需要提供对应接口，不支持时将实际接口异常反馈给模型，仍可使用本地资料。

## 实际执行步骤

1. 建立独立 `workspace/board-<时间>-<编号>/` 任务，保存来源索引。
2. 模型每轮输出一个资料动作：`list_sources`、`search_web`、`fetch_source`、`search_documents`、`read_page` 或 `inspect_page`。不清楚时用 `clarify` 提问。
3. 工具实际执行，将结果或异常类型与消息返回模型。网页保留正文和链接；PDF可检索文本并按页读取。原理图、扫描页和引脚表可渲染成页面图片，再发送视觉模型读取。
4. 模型用 `notes` 维护发现、待查项和来源。后续上下文只携带来源目录、笔记和最近六次结果，原始资料与工具历史保留在磁盘。
5. 模型提交 `propose` 候选配置。框架检查配置接口、引脚重复和引用是否来自实际读取页面，引用片段必须能在读取文本或保存的视觉提取中找到。
6. 展示候选、证据、写入位置与原配置。输入 `确认` 或 `y` 才安装配置；输入修改意见会重新研究，`/pause` 保存退出。

引用检查证明来源可追溯，不保证模型理解电路一定正确。视觉提取仍属于模型输出；图片和原文保留供人工核对。未解决的冲突不能提交为可安装配置。配置安装不代表已完成综合、下载或实板功能验证。

## 暂停与恢复

```powershell
.\.venv\Scripts\python.exe main.py board-research --board axu15egb --resume <board任务编号>
.\.venv\Scripts\python.exe main.py board-research --board axu15egb --resume <board任务编号> --request "补充：使用的PCB版本为手册对应版本，请继续核对"
```

`--non-interactive` 遇到提问或确认时保存并以退出码2暂停，不默认同意。达到每次运行的查询轮数上限也保存暂停，可恢复查询。已生成的待确认配置可以直接恢复确认，无需重新调用模型。

配置安装后，开发新任务：

```powershell
.\.venv\Scripts\python.exe main.py run --board axu15egb --request "使用已核实的两个PL LED实现自动流水灯，完成仿真、波形分析、构建和上板采样验证"
```

已有板卡配置保持可直接使用，不强制每个开发任务先重新研究。普通需求分析/规划也能主动查资料，但不会自行覆盖配置；需要新增板卡资源时会提问，引导先完成资料准备与确认。

## 保存的产物

| 路径 | 内容 |
| --- | --- |
| `state.json` | 当前研究状态、完整工具调用结果、笔记、待确认候选 |
| `sources/index.json` | 来源ID、原路径/URL、SHA256、页数、已读页面和视觉提取 |
| `sources/doc-*.pdf` / `.txt` | 实际读取的原始来源快照 |
| `sources/*-pages.json` | 分页提取文本 |
| `sources/*-page-*.png` | 实际发送给视觉模型的页面图片 |
| `board_candidate.json` | 模型提交的配置及逐项引用 |
| `board_review.md` | 待核对候选和来源索引 |
| `state.json`、`conversation.jsonl` | 现有确认机制保存的内容摘要、确认时间和用户回答 |
| `previous_profile.json` | 覆盖前的配置，首次安装时没有此文件 |

普通开发任务的只读资料结果在 `research_context.json`、`research_tools.jsonl` 和 `sources/`，不与工程测试日志混淆。`llm_calls.jsonl` 保存模型调用元数据，不保存密钥或思考原文。

## 可选配置与边界

无需修改当前 `config.json`。需要调整时可增加：

```json
"research": {"max_steps": 24, "vision_model": "deepseek-flash"}
```

研究使用当前配置的主模型；页面图像解读默认单独调用支持图像输入的 `deepseek-flash`，不改写主模型选择。搜索最多返回5条，单来源最大25 MiB，PDF最多2000页，每次读取最多10000字符。普通需求/规划每次最多执行8轮资料动作。

初版配置支持PL输入/输出、时钟和差分资源关系；不会自动扩展PS软件、DDR、高速收发器或复杂厂商IP支持。新板卡可准备配置、仿真和构建；当前JTAG脚本只识别 `xczu15*`，其他器件的下载必须先适配执行器，规划时会明确反馈此限制。网页只提取静态正文，不执行网页脚本，也不会绕过登录或下载权限。

## 本次验证范围（2026-10-30）

- `python -m unittest -q`：81项通过，覆盖字段类型错误、真实异常回传、资料读取、未读/虚构引用拒绝、候选修改与确认、无确认不安装、续接板卡选择和现有开发流程。
- 将原失败任务 `20261030-134827-5d3eb7` 的实际计划送入新接口反馈循环，经真实DeepSeek调用修复后，计划接口检查结果为空；不是框架自动替换错误字段。中途发现并补全了VIO探针的结构说明。
- 实际调用DeepSeek搜索接口，获取官方来源结果；实际抓取网页并保存来源快照。已对本地AXU15EGB手册完成文本检索、分页读取、页面渲染与真实视觉模型解读。
- 独立真实研究任务自行检索器件和LED页面，图像读取后因手册缺少明确的BANK电压/IO标准依据而提问，保存为 `waiting_for_user`；没有把未知信息当作事实，也没有安装配置。候选确认、安装及恢复路径由隔离的回归测试验证。
- 本轮没有执行完整应用的仿真、综合、下载或实板功能复验；现有 `boards/axu15egb.json` 保持原样。当前用户配置的主模型仍为 `deepseek-flash`，没有改回Pro。
