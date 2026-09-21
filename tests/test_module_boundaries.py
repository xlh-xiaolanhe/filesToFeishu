import json
import subprocess
import sys

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings
from files_to_feishu.store import Store


def test_feishu_can_be_used_without_loading_pdf_engine():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from files_to_feishu.integrations.feishu import FeishuClient, Publisher; "
            "import sys; "
            "assert not any(m.startswith(('docling', 'pypdf', 'files_to_feishu.converters')) "
            "for m in sys.modules)",
        ],
        check=True,
    )


def test_pdf_converter_does_not_load_feishu_or_web():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from files_to_feishu.converters.pdf import DoclingParser; "
            "import sys; "
            "assert not any(m.startswith(('files_to_feishu.integrations', 'fastapi')) "
            "for m in sys.modules)",
        ],
        check=True,
    )


def test_existing_serialized_job_remains_readable_after_reorganization(tmp_path):
    store = Store(tmp_path / "jobs.sqlite3")
    job = store.create("legacy.pdf", "source-digest")
    store.update(
        job["id"],
        status="succeeded",
        parsed=True,
        document_id="existing-doc",
        url="https://example.feishu.cn/wiki/existing",
        journal={
            "original-pdf:bind": {
                "state": "done",
                "result": {"block": {"file": {"token": "original"}}},
            }
        },
    )
    folder = tmp_path / "jobs" / job["id"]
    folder.mkdir(parents=True)
    legacy = {
        "pages": 1,
        "elements": [
            {
                "kind": "text",
                "page": 1,
                "text": "Old content",
                "level": 1,
                "rows": [],
                "asset": "",
                "bbox": [],
            }
        ],
        "notices": [],
        "page_images": ["page-1.png"],
    }
    (folder / "parsed.json").write_text(json.dumps(legacy), encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path))
    service = app.state.service
    try:
        parsed = service.parsed(job["id"])
        assert (
            parsed.model_dump(
                exclude={
                    "source_kind": True,
                    "metadata": True,
                    "assets": True,
                    "elements": {
                        "__all__": {
                            "language",
                            "code_origin",
                            "code_reviewed",
                            "code_sources",
                            "runs",
                            "table_runs",
                            "list_depth",
                            "list_start",
                            "web_locator",
                        }
                    },
                }
            )
            == legacy
        )
        assert parsed.source_kind == "pdf"
        assert parsed.metadata.url == ""
        assert parsed.assets == []
        assert parsed.elements[0].language == "plaintext"
        assert parsed.elements[0].code_origin == ""
        assert parsed.elements[0].code_reviewed is False
        assert parsed.elements[0].code_sources == []
        service.store.recover()
        persisted = service.store.get(job["id"])
        assert persisted["status"] == "succeeded"
        assert persisted["document_id"] == "existing-doc"
        assert persisted["journal"]["original-pdf:bind"]["state"] == "done"
    finally:
        service.close()
