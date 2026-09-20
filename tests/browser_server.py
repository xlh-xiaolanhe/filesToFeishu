"""Browser test server: real local parser, simulated Feishu, disposable job database."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings
from files_to_feishu.converters.pdf import render_pages
from files_to_feishu.models import CodeSource, Element, ParsedDocument
from tests.integrations.feishu.test_publisher import MemoryFeishu


def code_preview_fixture(source, assets, progress):
    """Deterministic code/recognition states for testing editing and publish gates."""
    images = render_pages(source, assets)
    return ParsedDocument(
        pages=2,
        page_images=images,
        elements=[
            Element(
                kind="code",
                page=1,
                text="const n = 1\n  n.toUperCase()",
                language="javascript",
                code_origin="ocr",
                asset="page-1.png",
                code_sources=[
                    CodeSource(page=1, asset="page-1.png"),
                    CodeSource(page=2, asset="page-2.png"),
                ],
            ),
            Element(kind="image", page=2, asset="page-2.png", text="Manual conversion sample"),
        ],
    )


def heading_preview_fixture(source, assets, progress):
    return ParsedDocument(
        pages=2,
        page_images=render_pages(source, assets),
        elements=[
            Element(kind="heading", page=1, text="TypeScript 快速上手", level=1),
            Element(kind="heading", page=1, text="七、常用类型与语法", level=2),
            Element(kind="heading", page=1, text="9. 一个特殊情况", level=3),
            Element(kind="heading", page=2, text="代码段1（正常）", level=4),
            Element(kind="heading", page=2, text="代码段2（特殊）", level=4),
            Element(kind="heading", page=2, text="为什么会这样？", level=4),
            Element(kind="heading", page=2, text="10. 复习类相关知识", level=3),
        ],
    )


if __name__ == "__main__":
    with TemporaryDirectory(prefix="pdf-browser-") as data:
        app = create_app(
            Settings(
                _env_file=None,
                data_dir=Path(data),
                feishu_app_id="browser-test",
                feishu_app_secret="fake-test-secret",
                feishu_parent_url="https://test.feishu.cn/wiki/parent",
            )
        )
        app.state.service.client.close()
        app.state.service.client = MemoryFeishu()
        if os.getenv("TEST_CODE_PREVIEW") == "1":
            app.state.service.parser = code_preview_fixture
        if os.getenv("TEST_HEADING_PREVIEW") == "1":
            app.state.service.parser = heading_preview_fixture
        uvicorn.run(app, host="127.0.0.1", port=8766)
