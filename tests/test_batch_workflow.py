"""Batch submissions share one durable queue and keep failures/review per item."""

import threading

from fastapi.testclient import TestClient

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings
from files_to_feishu.models import UserError
from tests.integrations.feishu.test_publisher import MemoryFeishu
from tests.test_wechat_workflow import PAYLOAD, URL, ArticleFixture
from tests.test_workflow import fixture_parser, wait

BATCH = "b" * 32


def test_multiple_pdfs_queue_failure_isolated_and_publish_serially(tmp_path, pdf_bytes):
    app = create_app(Settings(data_dir=tmp_path, feishu_app_id="test"))
    service = app.state.service
    service.client.close()
    service.client = MemoryFeishu()
    started, release = threading.Event(), threading.Event()
    calls = []

    def parser(source, assets, progress):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
            raise UserError("第一份转换失败")
        return fixture_parser(source, assets, progress)

    service.parser = parser
    with TestClient(app) as client:
        try:
            jobs = []
            for i in range(3):
                response = client.post(
                    "/api/jobs",
                    files={"file": (f"{i}.pdf", pdf_bytes)},
                    data={"batch_id": BATCH},
                )
                assert response.status_code == 202, response.text
                jobs.append(response.json())
            assert started.wait(2)
            assert client.get(f"/api/jobs/{jobs[1]['id']}").json()["status"] == "queued"
        finally:
            release.set()
        assert wait(client, jobs[0]["id"], {"failed"})["error"] == "第一份转换失败"
        for job in jobs[1:]:
            ready = wait(client, job["id"], {"ready"})
            assert ready["batch_id"] == BATCH
        assert len(set(calls)) == 1
        # Same content and target can reuse a verified result even in a batch.
        for job in jobs[1:]:
            assert client.post(f"/api/jobs/{job['id']}/publish", json=PAYLOAD).status_code == 202
        for job in jobs[1:]:
            assert wait(client, job["id"], {"succeeded", "failed"})["status"] == "succeeded"
        assert service.client.created == 1


def test_verification_pauses_queue_and_cancel_does_not_close_another_session(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    service = app.state.service
    service.wechat = ArticleFixture(challenge=True)
    with TestClient(app) as client:
        first = client.post("/api/jobs/wechat", json={"url": URL, "batch_id": BATCH}).json()
        wait(client, first["id"], {"waiting_verification"})
        second = client.post("/api/jobs/wechat", json={"url": URL + "-2"}).json()
        third = client.post("/api/jobs/wechat", json={"url": URL + "-3"}).json()
        assert second["status"] == third["status"] == "queued"
        assert client.post(f"/api/jobs/{second['id']}/cancel").status_code == 202
        assert service.wechat.calls.count("cancel") == 0
        assert client.post(f"/api/jobs/{first['id']}/continue").status_code == 202
        wait(client, first["id"], {"ready"})
        wait(client, third["id"], {"waiting_verification"})
        assert service.wechat.calls.count("start") == 2
        assert client.get(f"/api/jobs/{second['id']}").json()["status"] == "cancelled"
        client.post(f"/api/jobs/{third['id']}/cancel")
        wait(client, third["id"], {"cancelled"})


def test_batch_id_is_validated_before_any_job_creation(tmp_path, pdf_bytes):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert (
            client.post("/api/jobs/wechat", json={"url": URL, "batch_id": "../x"}).status_code
            == 422
        )
        assert (
            client.post(
                "/api/jobs", files={"file": ("a.pdf", pdf_bytes)}, data={"batch_id": "../x"}
            ).status_code
            == 422
        )
        assert client.get("/api/jobs").json() == []


def test_queued_publish_is_locked_and_stale_review_is_rejected(tmp_path, pdf_bytes):
    app = create_app(Settings(data_dir=tmp_path, feishu_app_id="test"))
    service = app.state.service
    service.client.close()
    service.client = MemoryFeishu()
    service.wechat = ArticleFixture(challenge=True)
    service.parser = fixture_parser
    with TestClient(app) as client:
        pdf = client.post("/api/jobs", files={"file": ("a.pdf", pdf_bytes)}).json()
        ready = wait(client, pdf["id"], {"ready"})
        article = client.post("/api/jobs/wechat", json={"url": URL}).json()
        wait(client, article["id"], {"waiting_verification"})
        endpoint = f"/api/jobs/{pdf['id']}/publish"
        rejected = client.post(endpoint, json={**PAYLOAD, "review_token": "0" * 64})
        assert rejected.status_code == 400
        assert "预览内容已变化" in rejected.text
        accepted = client.post(endpoint, json={**PAYLOAD, "review_token": ready["review_token"]})
        assert accepted.json()["status"] == "publish_queued"
        assert accepted.json()["content_locked"] is True
        assert service.client.created == 0
        assert client.post(endpoint, json=PAYLOAD).json()["status"] == "publish_queued"
        service.store.recover()
        restored = service.store.get(pdf["id"])
        assert restored["status"] == "needs_review"
        assert restored["target"]["node_token"] == "parent"
        assert restored["parsed"] is True
        assert service.client.created == 0


def test_queue_limit_and_queued_pdf_cancellation(tmp_path, pdf_bytes):
    app = create_app(Settings(data_dir=tmp_path))
    service = app.state.service
    service.wechat = ArticleFixture(challenge=True)
    with TestClient(app) as client:
        first = client.post("/api/jobs/wechat", json={"url": URL}).json()
        wait(client, first["id"], {"waiting_verification"})
        pdf = client.post("/api/jobs", files={"file": ("a.pdf", pdf_bytes)}).json()
        assert pdf["status"] == "queued"
        assert client.post(f"/api/jobs/{pdf['id']}/cancel").json()["status"] == "cancelled"
        assert service.wechat.calls.count("cancel") == 0
        for _ in range(49):
            service.store.create("queued.pdf", "unused")
        response = client.post("/api/jobs/wechat", json={"url": URL})
        assert response.status_code == 400
        assert "50" in response.text
