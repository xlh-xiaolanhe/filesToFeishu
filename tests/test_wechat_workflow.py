import hashlib
import io
import threading
import time
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings
from files_to_feishu.models import Asset, Element, Notice, ParsedDocument, SourceMetadata, TextRun
from files_to_feishu.service import article_digest
from tests.helpers import reviewed_payload
from tests.integrations.feishu.test_publisher import MemoryFeishu
from tests.test_workflow import wait

URL = "https://mp.weixin.qq.com/s/original-fixture"
PAYLOAD = {"url": "https://test.feishu.cn/wiki/parent", "confirmed": True}


class ArticleFixture:
    def __init__(self, challenge=False):
        self.challenge = challenge
        self.text = "Original fixture paragraph"
        self.calls = []
        self.threads = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False

    def start(self, url, folder, progress):
        self.calls.append("start")
        self.threads.append(threading.get_ident())
        self.started.set()
        if self.block:
            assert self.release.wait(5)
        return None if self.challenge else self.result(url, folder)

    def resume(self, folder, progress):
        self.calls.append("resume")
        self.threads.append(threading.get_ident())
        return self.result(URL, folder)

    def cancel(self):
        self.calls.append("cancel")
        self.threads.append(threading.get_ident())

    def close(self):
        self.cancel()

    def pump(self):
        self.calls.append("pump")
        self.threads.append(threading.get_ident())
        time.sleep(0.01)

    def result(self, url, folder):
        assets = folder / "assets"
        assets.mkdir(exist_ok=True)
        buffer = io.BytesIO()
        frames = [Image.new("RGB", (64, 32), color) for color in ("#246754", "#91bcb0")]
        frames[0].save(
            buffer, format="GIF", save_all=True, append_images=frames[1:], duration=200, loop=0
        )
        content = buffer.getvalue()
        (assets / "image-1.gif").write_bytes(content)
        (assets / "not-in-manifest.png").write_bytes(b"private")
        with ZipFile(folder / "source.zip", "w") as archive:
            archive.writestr("index.html", "<p>" + self.text + "</p>")
            archive.writestr("assets/image-1.gif", content)
        return ParsedDocument(
            source_kind="wechat",
            metadata=SourceMetadata(url=url, title="Original article"),
            elements=[
                Element(kind="text", text=self.text, runs=[TextRun(text=self.text, bold=True)]),
                Element(kind="image", asset="image-1.gif"),
                Element(
                    kind="code",
                    text="if (ok) {\n  run();\n}",
                    code_origin="html",
                    code_reviewed=True,
                ),
            ],
            notices=[Notice(reason="示例视频未完整转换，保留原文入口")],
            assets=[
                Asset(
                    name="image-1.gif",
                    media_type="image/gif",
                    digest=hashlib.sha256(content).hexdigest(),
                )
            ],
        )


@pytest.fixture
def article_app(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, feishu_app_id="test"))
    service = app.state.service
    service.client.close()
    service.client = MemoryFeishu()
    service.wechat = ArticleFixture()
    with TestClient(app) as client:
        yield service, client


def test_article_confirm_publish_archive_dedupe_and_changed_content(article_app):
    service, client = article_app
    job = client.post("/api/jobs/wechat", json={"url": URL}).json()
    ready = wait(client, job["id"], {"ready", "failed"})
    assert ready["status"] == "ready", ready
    assert ready["preview"]["pages"] is None
    assert ready["preview"]["elements"][0]["runs"][0]["bold"]
    original = client.get(f"/api/jobs/{job['id']}/original")
    assert original.headers["content-type"] == "application/zip"
    assert original.content.startswith(b"PK")
    assert (
        client.post(f"/api/jobs/{job['id']}/publish", json={"url": PAYLOAD["url"]}).status_code
        == 400
    )
    assert service.client.created == 0
    assert (
        client.post(
            f"/api/jobs/{job['id']}/publish", json=reviewed_payload(client, job["id"], PAYLOAD)
        ).status_code
        == 202
    )
    saved = wait(client, job["id"], {"succeeded", "failed"})
    assert saved["status"] == "succeeded", saved
    journal = service.store.get(job["id"])["journal"]
    assert "original-wechat:bind" in journal
    assert "original-pdf:bind" not in journal
    duplicate = client.post("/api/jobs/wechat", json={"url": URL}).json()
    wait(client, duplicate["id"], {"ready"})
    client.post(
        f"/api/jobs/{duplicate['id']}/publish",
        json=reviewed_payload(client, duplicate["id"], PAYLOAD),
    )
    reused = wait(client, duplicate["id"], {"succeeded", "failed"})
    assert reused["status"] == "succeeded", reused
    assert service.client.created == 1
    service.wechat.text = "Article updated"
    changed = client.post("/api/jobs/wechat", json={"url": URL}).json()
    wait(client, changed["id"], {"ready"})
    client.post(
        f"/api/jobs/{changed['id']}/publish", json=reviewed_payload(client, changed["id"], PAYLOAD)
    )
    result = wait(client, changed["id"], {"succeeded", "failed"})
    assert result["status"] == "succeeded", result
    assert service.client.created == 2


def test_asset_manifest_prevents_private_file_and_symlink_reads(article_app, tmp_path):
    service, client = article_app
    job = client.post("/api/jobs/wechat", json={"url": URL}).json()
    wait(client, job["id"], {"ready"})
    root = f"/api/jobs/{job['id']}/assets/"
    response = client.get(root + "image-1.gif")
    assert response.headers["content-type"] == "image/gif"
    assert response.content.startswith(b"GIF89a")
    assert client.get(root + "not-in-manifest.png").status_code == 404
    secret = tmp_path / "private.txt"
    secret.write_text("not an image")
    asset = service.folder(job["id"]) / "assets/image-1.gif"
    asset.unlink()
    asset.symlink_to(secret)
    assert client.get(root + "image-1.gif").status_code == 404


def test_challenge_continue_cancel_and_thread_ownership(article_app):
    service, client = article_app
    service.wechat.challenge = True
    job = client.post("/api/jobs/wechat", json={"url": URL}).json()
    wait(client, job["id"], {"waiting_verification"})
    assert "pump" in service.wechat.calls
    assert client.get(f"/api/jobs/{job['id']}/original").status_code == 404
    queued = client.post("/api/jobs/wechat", json={"url": URL}).json()
    assert queued["status"] == "queued"
    assert client.post(f"/api/jobs/{queued['id']}/cancel").status_code == 202
    assert client.post(f"/api/jobs/{job['id']}/continue").status_code == 202
    wait(client, job["id"], {"ready"})
    second = client.post("/api/jobs/wechat", json={"url": URL}).json()
    wait(client, second["id"], {"waiting_verification"})
    client.post(f"/api/jobs/{second['id']}/cancel")
    wait(client, second["id"], {"cancelled"})
    assert client.post(f"/api/jobs/{second['id']}/continue").status_code == 400
    assert len(set(service.wechat.threads)) == 1
    assert service.wechat.threads[0] != threading.get_ident()


def test_cancel_inflight_fetch_never_exposes_ready_result(article_app):
    service, client = article_app
    service.wechat.block = True
    job = client.post("/api/jobs/wechat", json={"url": URL}).json()
    assert service.wechat.started.wait(2)
    try:
        cancelled = client.post(f"/api/jobs/{job['id']}/cancel").json()
        assert cancelled["status"] == "cancelling"
    finally:
        service.wechat.release.set()
    finished = wait(client, job["id"], {"cancelled"})
    assert not finished.get("parsed")
    assert client.post(f"/api/jobs/{job['id']}/publish", json=PAYLOAD).status_code == 400


def test_invalid_article_input_never_creates_task(article_app):
    service, client = article_app
    for url in ("http://127.0.0.1/", "https://mp.weixin.qq.com.evil.test/s/a", "file:///etc/hosts"):
        assert client.post("/api/jobs/wechat", json={"url": url}).status_code == 400
    assert service.store.list() == []


def test_article_identity_ignores_expiring_asset_links_but_not_content(tmp_path):
    parsed = ArticleFixture().result(URL, tmp_path)
    original = article_digest(parsed)
    parsed.assets[0].source_url = "https://example.com/image?temporary=new"
    parsed.elements[0].web_locator = "#js_content > p:nth-child(2)"
    assert article_digest(parsed) == original
    parsed.elements[0].runs[0].italic = True
    assert article_digest(parsed) != original
    parsed.elements[0].runs[0].italic = False
    parsed.assets[0].digest = "different-image"
    assert article_digest(parsed) != original


def test_oversized_archive_fails_before_ready_or_remote_creation(article_app):
    service, client = article_app
    result = service.wechat.result

    def oversized(url, folder):
        parsed = result(url, folder)
        with (folder / "source.zip").open("ab") as archive:
            archive.truncate(20 * 1024 * 1024 + 1)
        return parsed

    service.wechat.result = oversized
    job = client.post("/api/jobs/wechat", json={"url": URL}).json()
    failed = wait(client, job["id"], {"failed"})
    assert "20 MB" in failed["error"]
    assert not failed.get("parsed")
    assert service.client.created == 0
    assert client.post(f"/api/jobs/{job['id']}/publish", json=PAYLOAD).status_code == 400
