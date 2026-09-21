"""History removal preserves publishing evidence but revokes public task access."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings
from files_to_feishu.models import UserError
from files_to_feishu.store import ACTIVE, JobNotFound, Store
from tests.helpers import reviewed_payload
from tests.integrations.feishu.test_publisher import MemoryFeishu
from tests.test_wechat_workflow import PAYLOAD, URL, ArticleFixture
from tests.test_workflow import fixture_parser, wait


@pytest.mark.parametrize("source", ["pdf", "wechat"])
def test_deleted_task_is_hidden_but_duplicate_publish_still_verified(tmp_path, pdf_bytes, source):
    app = create_app(Settings(data_dir=tmp_path, feishu_app_id="test"))
    service = app.state.service
    service.client.close()
    service.client = MemoryFeishu()
    service.parser = fixture_parser
    service.wechat = ArticleFixture()
    with TestClient(app) as client:

        def create():
            if source == "wechat":
                return client.post("/api/jobs/wechat", json={"url": URL}).json()
            return client.post("/api/jobs", files={"file": ("sample.pdf", pdf_bytes)}).json()

        task = create()
        job_id = task["id"]
        wait(client, job_id, {"ready"})
        client.post(f"/api/jobs/{job_id}/publish", json=reviewed_payload(client, job_id, PAYLOAD))
        saved = wait(client, job_id, {"succeeded"})
        folder = service.folder(job_id)
        before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        assert client.delete(f"/api/jobs/{job_id}").json() == {"deleted": True}
        assert client.delete(f"/api/jobs/{job_id}").status_code == 200
        assert client.get("/api/jobs").json() == []
        assert client.get(f"/api/jobs/{job_id}").status_code == 404
        assert client.get(f"/api/jobs/{job_id}/original").status_code == 404
        assert client.get(f"/api/jobs/{job_id}/assets/page-1.png").status_code == 404
        for action in ("publish", "cancel", "continue", "notifications/failed/retry"):
            assert client.post(f"/api/jobs/{job_id}/{action}", json=PAYLOAD).status_code == 404
        assert (
            client.post(
                f"/api/jobs/{job_id}/code/0",
                json={"text": "x", "language": "plaintext", "expected_revision": 1},
            ).status_code
            == 404
        )
        assert before == {
            p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()
        }
        reopened = Store(service.store.path)
        reopened.recover()
        assert reopened.list() == []
        assert reopened.get(job_id, include_deleted=True)["journal"]
        duplicate = create()
        wait(client, duplicate["id"], {"ready"})
        client.post(
            f"/api/jobs/{duplicate['id']}/publish",
            json=reviewed_payload(client, duplicate["id"], PAYLOAD),
        )
        result = wait(client, duplicate["id"], {"succeeded", "failed"})
        assert result["status"] == "succeeded", result
        assert result["url"] == saved["url"]
        assert service.client.created == 1


@pytest.mark.parametrize("status", sorted(ACTIVE))
def test_active_tasks_cannot_be_deleted(tmp_path, status):
    store = Store(tmp_path / "jobs.sqlite3")
    task = store.create("original.pdf", "digest")
    store.update(task["id"], status=status)
    with pytest.raises(UserError, match="排队或处理中"):
        store.delete(task["id"])
    assert store.get(task["id"])["status"] == status


@pytest.mark.parametrize("status", ["ready", "failed", "needs_review", "cancelled", "succeeded"])
def test_inactive_history_deletion_is_durable_and_rejects_late_writes(tmp_path, status):
    store = Store(tmp_path / "jobs.sqlite3")
    task = store.create("original.pdf", "digest")
    journal = {"step": {"state": "done"}} if status == "succeeded" else {}
    store.update(task["id"], status=status, journal=journal)
    store.delete(task["id"])
    store.delete(task["id"])
    assert store.list() == []
    retained = store.list(include_deleted=True)[0]
    assert retained["status"] == status
    assert retained["journal"] == journal
    for operation in (
        lambda: store.get(task["id"]),
        lambda: store.update(task["id"], status="ready"),
        lambda: store.claim(task["id"]),
        lambda: store.update_notification(task["id"], "failed", {"failed"}, status="pending"),
    ):
        with pytest.raises(JobNotFound):
            operation()


@pytest.mark.parametrize("status", ["failed", "needs_review"])
def test_unresolved_remote_writes_keep_the_original_recovery_entry(tmp_path, status):
    store = Store(tmp_path / "jobs.sqlite3")
    task = store.create("original.pdf", "digest")
    store.update(task["id"], status=status, journal={"create": {"state": "uncertain"}})
    with pytest.raises(UserError, match="未完成核验"):
        store.delete(task["id"])
    assert store.get(task["id"])["journal"]


@pytest.mark.parametrize("status", ["pending", "sending"])
def test_delete_rejects_outstanding_notification(tmp_path, status):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        store = app.state.service.store
        task = store.create("original.pdf", "digest")
        store.update(task["id"], status="failed", notifications={"failed": {"status": status}})
        response = client.delete(f"/api/jobs/{task['id']}")
        assert response.status_code == 400
        assert "通知" in response.text
        assert store.get(task["id"])["notifications"]["failed"]["status"] == status


def test_delete_validation_and_cross_origin_guard(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        for job_id in ("invalid", "a" * 32):
            assert client.delete(f"/api/jobs/{job_id}").status_code == 404
        assert (
            client.delete(
                "/api/jobs/" + "a" * 32, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )


def test_claim_and_delete_are_atomic_across_connections(tmp_path):
    store = Store(tmp_path / "jobs.sqlite3")
    task = store.create("original.pdf", "digest")
    store.update(task["id"], status="ready")
    barrier = threading.Barrier(2)

    def run(action):
        barrier.wait(timeout=5)
        try:
            action(task["id"])
            return "ok"
        except (UserError, JobNotFound):
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(run, store.delete)
        b = executor.submit(run, store.claim)
        assert sorted([a.result(), b.result()]) == ["ok", "rejected"]
    retained = store.get(task["id"], include_deleted=True)
    assert bool(retained.get("deleted_at")) != (retained["status"] == "publish_queued")
