import time

from fastapi.testclient import TestClient

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings
from files_to_feishu.models import Element, ParsedDocument
from files_to_feishu.parser import render_pages
from tests.test_publisher import MemoryFeishu


def fixture_parser(source, assets, progress):
    return ParsedDocument(
        pages=1,
        elements=[Element(kind="text", page=1, text="Editable PDF content.")],
        page_images=render_pages(source, assets),
    )


def wait(client, job_id, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in expected:
            return job
        time.sleep(0.02)
    raise AssertionError(job)


def test_upload_preview_publish_duplicate_and_refresh(tmp_path, pdf_bytes):
    app = create_app(Settings(data_dir=tmp_path, feishu_app_id="test", feishu_app_secret="secret"))
    service = app.state.service
    service.client.close()
    service.client = MemoryFeishu()
    service.parser = fixture_parser
    with TestClient(app) as client:
        job = client.post("/api/jobs", files={"file": ("sample.pdf", pdf_bytes)}).json()
        ready = wait(client, job["id"], {"ready"})
        assert ready["preview"]["elements"][0]["text"] == "Editable PDF content."
        assert client.get(f"/api/jobs/{job['id']}/assets/page-1.png").status_code == 200
        assert client.get(f"/api/jobs/{job['id']}/original").content == pdf_bytes
        payload = {"url": "https://test.feishu.cn/wiki/parent", "title": "Test"}
        assert client.post(f"/api/jobs/{job['id']}/publish", json=payload).status_code == 202
        saved = wait(client, job["id"], {"succeeded", "failed"})
        assert saved["status"] == "succeeded", saved
        assert "journal" not in saved and "app_id" not in saved
        duplicate = client.post("/api/jobs", files={"file": ("sample.pdf", pdf_bytes)}).json()
        wait(client, duplicate["id"], {"ready"})
        client.post(f"/api/jobs/{duplicate['id']}/publish", json=payload)
        reused = wait(client, duplicate["id"], {"succeeded", "failed"})
        assert reused["url"] == saved["url"]
        assert service.client.created == 1
        assert len(client.get("/api/jobs").json()) == 2


def test_invalid_upload_and_cross_origin_mutation_are_rejected(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.post("/api/jobs", files={"file": ("bad.pdf", b"bad")}).status_code == 400
        assert client.get("/api/jobs/invalid").status_code == 404
        assert (
            client.post(
                "/api/targets/resolve", json={"url": "x"}, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert client.get("/api/health", headers={"Host": "evil.test"}).status_code == 400
        assert client.get("/").headers["content-security-policy"]
