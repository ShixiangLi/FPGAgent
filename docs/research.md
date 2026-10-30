# 通用资料研究

资料搜集服务于开发任务，不限定为板卡资料。可以查询协议规范、器件数据手册、IP/工具文档、论文、算法说明、参考RTL和测试数据。模型根据当前需求选择检索、阅读或提问；普通资料研究不要求生成板卡配置。

## 模块职责

| 文件 | 职责 |
| --- | --- |
| `fpga_agent/research.py` | 发现资料、导入来源、调度通用资料动作、反馈结果、维护研究笔记 |
| `fpga_agent/documents.py` | 网页搜索/下载、来源快照、文本提取、分页检索/阅读和PDF页面图片解读 |
| `fpga_agent/board_research.py` | 复用通用工具，处理板卡配置候选、引用检查、用户确认和安装 |

需求分析、方案规划和板卡资料准备使用同一套查询工具，不按每类资料复制一套实现。系统提示词仍在 `fpga_agent/prompts/catalog.py`。

## 提供资料

可以按主题放到项目的 `references/`，子目录中的受支持文件也会被发现：

```text
references/
  protocols/uart.pdf
  devices/adc_datasheet.pdf
  tools/vivado_notes.md
  algorithms/filter_design.pdf
  examples/reference_receiver.sv
  tests/vectors.csv
```

也可以不复制文件，通过 `run --source` 提供文件或URL，参数可重复：

```powershell
.\.venv\Scripts\python.exe main.py run --target sim --source "D:\references\protocol.pdf" --source "D:\references\vectors.csv" --request "根据协议资料实现接收器，完成自检测试和波形分析；遇到版本或时序歧义先提问，不上板"
```

在现有任务中补充资料并修改需求：

```powershell
.\.venv\Scripts\python.exe main.py run --resume <任务编号> --source "D:\references\protocol_v2.pdf" --request "按新提供的第二版协议调整设计，先重新整理需求与测试方案"
```

任务提供的来源列表保存于 `state.json` 的 `research_sources`，恢复时无需重输。相对文件路径相对于项目根目录解析。重复使用的资料也可在 `config.json` 中配置：

```json
"research": {
  "sources": ["references/protocols/uart.pdf", "D:/references/tool_notes.md"],
  "vision_model": "deepseek-flash",
  "max_steps": 24
}
```

已有 `research` 配置时合并字段即可。`max_steps` 控制独立板卡研究每次运行的轮数；普通需求/规划每次最多8轮资料动作。为兼容当前项目，`boards/` 中的PDF仍会被发现；其中的已登记JSON配置不会默认当作独立资料证据导入。

## 执行与保存

1. 需求分析/规划模型看到可用来源列表，判断是否需要查阅资料。提供文件不代表强制读完所有内容，应在需求中说明资料用途。
2. 首次执行资料动作时导入已提供资料。`search_web` 查网页，`fetch_source` 下载HTTP(S)来源，`search_documents` 定位资料页，`read_page` 分段读取，`inspect_page` 解读PDF图片。
3. 工具返回实际结果。单个来源打不开时记录 `source_errors`，其他可用资料继续读取；不会用框架推断替换异常或把失败来源标记为已读。
4. 模型用 `notes` 持续整理结论、来源ID/页码和疑问，保存到 `research.md`。之后的规划、文件生成和修复通过现有任务文档上下文读取这些笔记，避免反复发送整份资料。
5. 需求不明确或来源冲突时仍使用原有提问、修改和用户确认流程。原始资料是参考数据，不具有执行命令或覆盖用户需求的权限。

`research_context.json` 保存当前笔记和最近六次结果，`research_tools.jsonl` 保存完整通用工具记录，`sources/` 保存有SHA256的原始快照、分页文本及页面图片。研究过程不会把参考文件直接复制成工程交付文件，也不会因为读取了新手册就自动修改板卡配置；需要板卡资源变更时使用[板卡准备流程](board-research.md)。

## 当前支持的文件

- PDF：提取文本、分页读取、渲染页面供视觉模型查看。
- UTF-8文本：TXT、Markdown、RST、HTML、JSON、YAML、CSV/TSV、XDC及Verilog/SystemVerilog/VHDL参考代码。
- HTTP(S)网页或PDF：搜索后下载，或用户直接提供URL；静态HTML提取正文和链接。

读取参考VHDL代码不代表新增VHDL工程构建支持。Word、Excel、压缩包等二进制格式尚未适配，需要先导出为PDF或受支持文本；网页动态脚本和登录访问不在当前读取范围。已有来源快照保留在任务内，更新资料建议提供明确版本的新文件，便于区分和追溯。
