# PDF → 飞书知识库

本地网页工具：解析单个文字型 PDF，预览后创建可编辑飞书文档，并保存为指定知识库页面的子页面。复杂区域保留图片，原 PDF 作为附件保存。

当前版本：**v0.2.5 已交付**。支持代码区域识别、本地代码截图 OCR、块内编辑、跨页代码合并和原生代码块发布，保留原始区域供校对，并自动清理重复列表符号与 PDF 页码。标题等级根据原 PDF 字号、章节编号和上下文恢复。本版修复带标题栏代码框的缩进错位，并将数字键帽、带圈序号转成普通数字，将拆开的图标和同一行条目合并；预览与飞书发布使用同一内容。

`docs/` 是仅在本地维护的需求、计划、使用说明和验证记录，不进入 Git；新检出环境不包含这些文档。已有本地文档可从 `docs/README.md` 查阅。本 README 提供独立的启动、配置和开发入口，模块与演进约定见 [AGENTS.md](AGENTS.md)。公众号等其他来源尚未实现。

OCR 代码必须先校对保存。已有任务保留原预览；使用新识别规则时重新上传 PDF，新转换内容会创建新文档，不覆盖旧文档。

## 本地启动（macOS，Python 3.12）

首次准备：

```sh
uv sync --extra parser --locked
uv run --extra parser python scripts/download_models.py
test -f .env || cp .env.example .env
```

在本地 `.env` 填写 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 和可选的 `FEISHU_PARENT_URL`（格式为 `https://公司.feishu.cn/wiki/父节点标识`）；`DATA_DIR`、`DOCLING_ARTIFACTS_PATH` 默认分别为 `.data`、`.models`。完成下方飞书权限配置后运行：

```sh
./start.sh
```

打开 <http://127.0.0.1:8765>。macOS 也可双击 [start.command](start.command)；两个入口都会定位到项目根目录并使用 `.venv`，保留终端窗口，按 `Ctrl+C` 停止。脚本不自动安装依赖、下载模型或覆盖配置。

模型仅在准备时下载；普通页面读取文字层，代码截图使用 macOS Vision 本地 OCR。解析时不调用远程服务或生成式补写，只有确认发布后才向飞书上传。服务限定本机、单进程运行；修改配置后重启。

上传单个不超过 20 MB、100 页的文字型 PDF → 检查知识库父页面 → 对照预览并保存代码校对 → 勾选整体确认 → 保存到飞书。扫描件、加密或损坏文件不支持；复杂区域保留图片，不承诺逐像素一致。

失败后在“最近任务”核对错误并续接，不要盲目重复新建。`.data` 保存原件、预览、任务状态和写入日志；保留整套数据才能去重与恢复，不能只删数据库再重试。历史已发布任务不会自动重新转换。详细流程如本地已有，可查 `docs/guides/pdf-to-feishu.md`。

## 飞书配置

在飞书开发者后台开通文档创建/编辑/读取、素材上传/下载、知识库读写权限，发布应用并完成管理员审批；同时将该应用/机器人加入目标知识库的可编辑成员，允许在父页面下创建或迁入文档。接口权限和知识库成员权限都需要配置；“检查保存位置”成功仅代表可读取该节点。凭证只保存在本地 `.env`，不提交 Git。

## 开发与验证

修改项目前先阅读 [项目 Agent 规则](AGENTS.md)，其中约定模块边界、格式扩展、Python 规范和交付要求。

在已准备依赖的项目根目录执行：

```sh
# 快速测试：排除需要真实模型和 Vision 的测试
.venv/bin/pytest -m 'not parser and not live'
# 完整本地测试：需要 .models 和 macOS Vision，不向飞书写入
RUN_PARSER_TESTS=1 RUN_OCR_TESTS=1 .venv/bin/pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/mypy
.venv/bin/python -m tests.converters.pdf.samples
```

最后一条生成原创代表性样本 `output/samples/representative.pdf`，不包含用户数据。真实 Docling 测试禁止网络连接；飞书接口和任务测试使用模拟服务。若 Vision 在受限沙箱中不能运行，在本机终端执行完整测试。文档检查包含在 pytest 中：始终核对仓库入口与版本；本地存在 docs 时额外检查文档、章节锚点和页面确认文案，缺少 docs 时只跳过该本地指南专项检查。

浏览器自动化另需 Node.js、`playwright` 包和 Chrome；必要时用 `NODE_PATH` 指向现有 Playwright 包。先生成上述样本，选择下表一个模式启动 8766 测试服务，再在另一个终端运行对应脚本；切换模式前停止前一个测试服务。

| 验证内容 | 启动隔离服务 | 执行验证 |
| --- | --- | --- |
| 真实 PDF 转换与模拟发布 | `.venv/bin/python -m tests.browser_server` | `node tests/browser_smoke.cjs` |
| 代码编辑与跨页来源 | `TEST_CODE_PREVIEW=1 .venv/bin/python -m tests.browser_server` | `node tests/browser_code_smoke.cjs` |
| 标题层级预览 | `TEST_HEADING_PREVIEW=1 .venv/bin/python -m tests.browser_server` | 上传样本，确认文档标题/章节/小节/代码示例为 H1/H2/H3/H4，下一节恢复 H3，确认后模拟发布成功 |

这些模式使用临时数据库和模拟飞书；退出测试服务后临时任务会消失，截图保存在 `output/playwright/`。只有明确要创建真实验收文档时才执行 `RUN_LIVE_TESTS=1 node tests/browser_smoke.cjs`，它会连接 8765 正常服务并写入本地配置的真实父页面；失败任务应在网页历史中续接。

历次验收保留在本地 `docs/`；开源组件与模型许可见 [第三方说明](THIRD_PARTY.md)。
