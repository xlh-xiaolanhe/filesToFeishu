"""Browser test server: real local parser, simulated Feishu, disposable job database."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings
from files_to_feishu.converters.pdf import render_pages
from files_to_feishu.models import Element, ParsedDocument
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
            ),
            Element(kind="image", page=2, asset="page-2.png", text="Manual conversion sample"),
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
        uvicorn.run(app, host="127.0.0.1", port=8766)
