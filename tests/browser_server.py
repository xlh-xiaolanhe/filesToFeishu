"""Browser test server: real local parser, simulated Feishu, disposable job database."""

from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings
from tests.integrations.feishu.test_publisher import MemoryFeishu

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
        uvicorn.run(app, host="127.0.0.1", port=8766)
