# 开源组件和参考来源

实现和许可证记录日期：2026-09-17。完整 Python 依赖版本以 `uv.lock` 为准；模型权重不随项目提交。

| 组件或参考 | 使用方式 | 许可与来源 |
| --- | --- | --- |
| Docling 2.128.0 / docling-core 2.97.0 | 本地版面、文字及表格解析；读取公共 JSON 模型 | [Docling](https://github.com/docling-project/docling)，MIT；模型另列 |
| Docling layout Heron | 默认布局模型；下载工具也准备 ONNX 变体 | [模型仓库](https://huggingface.co/docling-project/docling-layout-heron)、[ONNX](https://huggingface.co/docling-project/docling-layout-heron-onnx)，两者模型卡标注 Apache-2.0 |
| Docling TableFormer | 表格结构模型 | [模型仓库](https://huggingface.co/docling-project/docling-models)，模型卡标注 CDLA-Permissive-2.0 |
| pypdf / pypdfium2 / Pillow | 输入校验、文字覆盖对照、页面渲染和图片裁切 | [pypdf](https://github.com/py-pdf/pypdf) BSD-3-Clause；[pypdfium2](https://github.com/pypdfium2-team/pypdfium2) Apache-2.0 或 BSD-3-Clause（PDFium 及捆绑库另附通知）；[Pillow](https://github.com/python-pillow/Pillow) MIT-CMU |
| FastAPI / Uvicorn / Jinja / HTTPX / Pydantic | 本地网页、模板、HTTP 请求与数据模型 | 分别见依赖分发包中的 LICENSE；均使用各项目正式 Python 包 |
| ReportLab | 仅生成原创测试样本 | [ReportLab](https://www.reportlab.com/opensource/)，BSD |
| feishu-cli | 方案阶段参考写入、表格和知识库流程 | [源仓库](https://github.com/riba2534/feishu-cli)，MIT；没有直接移植源码 |
| feishu-document-writing | 参考图片占位、素材绑定和文件容器的接口调用顺序 | [源仓库](https://github.com/fan-sun/feishu-document-writing)，未复制或分发该项目代码 |
| MinerU-Skill | 只用于方案比较，不作为运行依赖 | [集成说明](https://github.com/Nebutra/MinerU-Skill/blob/main/references/integrations.md) |

应用接口层、任务日志和网页为本项目实现。测试 PDF 由 `tests/converters/pdf/samples.py` 原创生成。代码许可与模型许可分别适用；重新分发依赖或模型时，应保留其原始许可和通知。
