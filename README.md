# PDF → 飞书知识库

本地网页工具：解析单个文字型 PDF，预览后创建可编辑飞书文档，并保存为指定知识库页面的子页面。复杂区域保留图片，原 PDF 作为附件保存。

当前状态：**实施中**。本地离线解析与模拟发布已验证；真实应用鉴权和目标节点读取已通过，创建文档被权限错误 `99991672` 阻止，正在等待应用权限补齐后续接验收。版本需求与计划见 [文档索引](docs/README.md)。

## 本地启动（macOS，Python 3.12）

```sh
uv sync --extra parser --locked
uv run --extra parser python scripts/download_models.py
cp .env.example .env  # 仅在 .env 不存在时执行，已有配置不要覆盖
uv run --extra parser files-to-feishu
```

打开 <http://127.0.0.1:8765>。模型只在准备阶段下载；解析时禁用 OCR、远程服务和生成式内容补写，并强制模型离线加载。不要使用多个 Uvicorn worker，也不要把本地服务暴露到公网。

在 `.env` 中填写：

```dotenv
FEISHU_APP_ID=你的企业自建应用AppID
FEISHU_APP_SECRET=你的企业自建应用AppSecret
FEISHU_PARENT_URL=https://公司.feishu.cn/wiki/父节点标识
DATA_DIR=.data
DOCLING_ARTIFACTS_PATH=.models
```

修改配置后重启。凭证只在后端使用，`.env`、PDF、模型和任务目录均被 Git 忽略。本次提供的知识库链接已写入当前工作区的本地 `.env`，不进入公开示例。

## 飞书配置

1. 在 [飞书开发者后台](https://open.feishu.cn/app) 创建或选择企业自建应用，取得 App ID 与 App Secret。
2. 在应用权限管理中开通新版文档创建/编辑/读取、素材上传/下载、知识库读取/编辑相关权限，并发布应用版本、完成租户管理员审批。按下表实际调用的接口检查控制台列出的权限，不要只配置用户自己的访问权。
3. 将应用添加为目标知识库的可编辑成员，并确保应用可以在目标父页面下迁入文档。界面不支持直接添加应用时，按租户支持的机器人群组授权流程配置；仅能读取父页面并不代表能迁入新页面。
4. 在网页点击“检查保存位置”，确认页面名称；先用测试文件执行一次完整发布，按 [验收记录](docs/validation/v0.1-mvp.md) 检查用户实际访问、编辑和附件下载。

| 用途 | 调用接口 | 权限配置入口 |
| --- | --- | --- |
| 应用身份 | `POST /auth/v3/tenant_access_token/internal` | 已发布的企业自建应用凭证 |
| 文档和内容块 | `/docx/v1/documents`、`/documents/{id}/blocks`、`/blocks/{id}/children` | 新版文档创建、编辑及读取，常用权限 `docx:document` |
| 上传与核验附件 | `/drive/v1/medias/upload_all`、`/medias/{token}/download` | 对应素材接口的上传、下载权限及文档访问范围 |
| 知识库归档 | `/wiki/v2/spaces/get_node`、`/spaces/{id}/nodes/move_docs_to_wiki`、`/wiki/v2/tasks/{id}` | 知识库读写权限，常用权限 `wiki:wiki`，以及目标知识库编辑成员资格 |

权限的具体名称、可替代权限和审批方式以各接口的当前控制台说明为准。参考 [文档块](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/create)、[素材上传](https://open.feishu.cn/document/server-docs/docs/drive-v1/media/upload_all)、[知识库任务](https://open.feishu.cn/document/server-docs/docs/wiki-v2/task/get)。首版不申请修改现有文档的业务操作，也不执行权限批量扩张。

## 使用与边界

- 支持一个文字型 PDF，最大 20 MB、100 页；扫描型、加密、损坏文件拒绝处理。混合文件中没有文字层的页面保留整页图片并提示。
- 上传 → 本地解析 → 对照原文与转换结果 → 勾选确认 → 保存到飞书。解析不上传 PDF；只有点击保存才向飞书写入。
- 普通正文、标题、列表和简单表格使用原生块。合并单元格、公式等复杂区域使用图片；文字覆盖检测不足时整页保留图片。图片区域不可编辑，预览不是最终飞书版式。
- 保存时核对远端正文、表格、图片、原附件 SHA-256，以及知识库空间和父节点；任何必要步骤失败都不会显示成功。自动核对不能替代逐页人工验收。
- 同文件、同应用、同目标的成功记录经过重新核对后返回已有链接。同名不同内容加时间后缀。不会覆盖已有文档。
- 刷新页面和“最近任务”可以恢复进度。只有单个后台线程执行转换/发布，SQLite 保存任务与每一步写入记录。
- 已开始发布的任务不能换标题、目标或应用；需要改动时重新上传。

## 失败处理

网页显示失败阶段、错误及已知远端文档 ID。确认未生效的接口错误可以重试；已成功步骤会复用。超时或响应丢失的写操作标为“待核对”，重试时会核对已知图片/文件块的绑定和知识库迁移状态，能证实完成的步骤继续执行；无法确认的新建文档、追加块或素材上传不会盲目重放。首版不提供自动删除、自动清理或人工解除不确定步骤的按钮。需要重新创建时，应先人工确认并处置可能存在的半成品。

本地 `.data/jobs/<任务ID>/` 保存原文件、页面图片和结构化预览，SQLite 保存状态和写入日志。文件会持续保留；可在停止服务、确认无需恢复后自行备份或清理整套本地任务数据。不要只删除 SQLite 后对相同文件重新发布，否则失去本地去重依据。

## 开发与验证

```sh
uv run --extra parser pytest -m 'not parser and not live'
RUN_PARSER_TESTS=1 uv run --extra parser pytest
uv run --extra parser ruff check src tests scripts
uv run --extra parser mypy
uv run --extra parser python -m tests.samples
```

最后一条生成原创代表性样本 `output/samples/representative.pdf`，不包含用户数据。真实模型测试要求已准备 `.models`；测试会禁止网络连接并断言中英文文字、标题、列表、表格与图片保留。接口与任务测试使用模拟服务，不会向飞书创建文档。

浏览器人工复验和真实飞书验收步骤见 [验证记录](docs/validation/v0.1-mvp.md)。开源组件与模型许可见 [第三方说明](THIRD_PARTY.md)。

浏览器自动化需要 Node.js、`playwright` 包及 Chrome。在一个终端运行 `uv run --extra parser python -m tests.browser_server`，另一个终端运行 `node tests/browser_smoke.cjs`。测试使用真实本地解析器、临时数据库和模拟飞书，不会向真实知识库写入；截图存入 `output/playwright/`。环境可通过 `NODE_PATH` 指向已安装的 Playwright 包。

只有明确需要创建真实验收文档时才运行 `RUN_LIVE_TESTS=1 node tests/browser_smoke.cjs`；该命令连接正常服务的 8765 端口并写入 `.env` 指定的真实父页面。失败任务请在网页历史中续接，不要反复启动新的真实浏览器测试任务。
