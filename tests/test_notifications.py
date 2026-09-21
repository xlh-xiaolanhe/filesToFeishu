import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from files_to_feishu.app import create_app, public
from files_to_feishu.config import Settings
from files_to_feishu.integrations.feishu import FeishuClient
from files_to_feishu.models import UncertainWrite, UserError
from files_to_feishu.notifications import prepare_notification
from files_to_feishu.store import Store
from tests.helpers import reviewed_payload
from tests.integrations.feishu.test_publisher import MemoryFeishu
from tests.test_workflow import fixture_parser, wait


def settings(tmp_path, **changes):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        feishu_app_id="app",
        feishu_app_secret="secret",
        feishu_notify_enabled=True,
        feishu_notify_receive_id="ou_recipient",
        **changes,
    )


class NotifyingFeishu(MemoryFeishu):
    def __init__(self, failure=None):
        super().__init__()
        self.messages = []
        self.failure = failure

    def send_notification(self, *args):
        self.messages.append(args)
        if self.failure:
            raise self.failure
        return "om_message"


def wait_notification(client, job_id, event, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        receipt = next((n for n in job["notifications"] if n["event"] == event), None)
        if receipt and receipt["status"] in expected:
            return job, receipt
        time.sleep(0.02)
    raise AssertionError(job)


def test_messages_are_explicit_opt_in_and_do_not_disclose_body_or_enable_mentions(tmp_path):
    store = Store(tmp_path / "jobs.sqlite3")
    job = store.create('<at user_id="all">all</at>.pdf', "digest")
    job.update(status="failed", error="secret must stay local", journal={"token": "secret"})
    assert prepare_notification(Settings(_env_file=None), job) is None
    receipt = prepare_notification(settings(tmp_path), job)
    assert receipt and "secret" not in receipt.text and "<at" not in receipt.text
    assert "＜at" in receipt.text and "处理失败" in receipt.text
    job["notifications"] = {"failed": receipt.model_dump()}
    assert prepare_notification(settings(tmp_path), job) is None
    exposed = json.dumps(public(job))
    for field in ("receive_id", "app_id", "uuid", "text"):
        assert field not in public(job)["notifications"][0]
    assert "ou_recipient" not in exposed
    job["status"] = "ready"
    assert prepare_notification(settings(tmp_path), job) is None


@pytest.mark.parametrize("kind", ["ok", "rejected", "lost", "missing_id"])
def test_message_http_boundary_acknowledgement_and_no_ambiguous_retry(tmp_path, kind):
    requests = []

    def handle(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        requests.append(request)
        assert request.url.host == "open.feishu.cn"
        assert request.url.path == "/open-apis/im/v1/messages"
        assert request.url.params["receive_id_type"] == "open_id"
        payload = json.loads(request.content)
        assert payload == {
            "receive_id": "ou_user",
            "msg_type": "text",
            "content": json.dumps({"text": "done"}),
            "uuid": "stable-id",
        }
        if kind == "lost":
            raise httpx.ReadTimeout("timeout", request=request)
        if kind == "rejected":
            return httpx.Response(403, json={"code": 230013, "msg": "not available"})
        return httpx.Response(
            200, json={"code": 0, "data": {"message_id": "om_123"} if kind == "ok" else {}}
        )

    client = FeishuClient(settings(tmp_path), httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        if kind == "ok":
            assert client.send_notification("open_id", "ou_user", "done", "stable-id") == "om_123"
        else:
            with pytest.raises(UserError if kind == "rejected" else UncertainWrite):
                client.send_notification("open_id", "ou_user", "done", "stable-id")
        assert len(requests) == 1
    finally:
        client.close()


@pytest.mark.parametrize(
    "failure", [UserError("权限不足"), UncertainWrite("lost"), RuntimeError("secret")]
)
def test_delivery_failure_does_not_undo_success_and_only_certain_failures_can_retry(
    tmp_path, pdf_bytes, failure
):
    app = create_app(settings(tmp_path))
    service = app.state.service
    service.client.close()
    client = service.client = NotifyingFeishu(failure)
    service.parser = fixture_parser
    with TestClient(app) as http:
        job_id = http.post("/api/jobs", files={"file": ("sample.pdf", pdf_bytes)}).json()["id"]
        wait(http, job_id, {"ready"})
        assert http.get(f"/api/jobs/{job_id}").json()["notifications"] == []
        payload = reviewed_payload(http, job_id, {"url": "https://test.feishu.cn/wiki/parent"})
        http.post(f"/api/jobs/{job_id}/publish", json=payload)
        expected = "failed" if type(failure) is UserError else "uncertain"
        saved, receipt = wait_notification(http, job_id, "succeeded", {expected})
        assert saved["status"] == "succeeded" and saved["url"]
        assert "secret" not in receipt["error"]
        assert saved["url"] in client.messages[0][2]
        client.failure = None
        response = http.post(f"/api/jobs/{job_id}/notifications/succeeded/retry")
        if expected == "failed":
            assert response.status_code == 202
            _, receipt = wait_notification(http, job_id, "succeeded", {"sent"})
            assert receipt["message_id"] == "om_message"
            assert len(client.messages) == 2
            assert client.messages[0] == client.messages[1]
        else:
            assert response.status_code == 400 and len(client.messages) == 1
        assert client.created == 1
        # Verifying the existing document again cannot generate another outcome notification.
        http.post(f"/api/jobs/{job_id}/publish", json=payload)
        wait(http, job_id, {"succeeded"})
    assert len(client.messages) == (2 if expected == "failed" else 1)


def test_failed_conversion_notifies_and_next_batch_item_still_finishes(tmp_path, pdf_bytes):
    app = create_app(settings(tmp_path))
    service = app.state.service
    service.client.close()
    client = service.client = NotifyingFeishu()
    calls = []

    def parser(*args):
        calls.append(1)
        if len(calls) == 1:
            raise UserError("private document details")
        return fixture_parser(*args)

    service.parser = parser
    with TestClient(app) as http:
        jobs = [
            http.post(
                "/api/jobs",
                files={"file": (f"sample{i}.pdf", pdf_bytes)},
                data={"batch_id": "a" * 32},
            ).json()
            for i in range(2)
        ]
        failed, _ = wait_notification(http, jobs[0]["id"], "failed", {"sent"})
        assert failed["status"] == "failed"
        wait(http, jobs[1]["id"], {"ready"})
        assert len(client.messages) == 1
        assert "private document details" not in client.messages[0][2]
        assert "a" * 32 in client.messages[0][2]


def test_recovery_preserves_success_and_old_tasks_and_never_replays_unknown_delivery(tmp_path):
    store = Store(tmp_path / "jobs.sqlite3")
    old = store.create("old.pdf", "old")
    store.update(old["id"], status="succeeded", journal={"original-pdf:upload": "unchanged"})
    for state in ("pending", "sending", "sent"):
        job = store.create(state + ".pdf", "digest")
        job.update(status="succeeded", url="https://test.feishu.cn/wiki/child")
        receipt = prepare_notification(settings(tmp_path), job)
        receipt.status = state
        store.update(
            job["id"], status="succeeded", notifications={"succeeded": receipt.model_dump()}
        )
    store.recover()
    states = {job["filename"]: job for job in store.list()}
    assert all(job["status"] == "succeeded" for job in states.values())
    assert states["pending.pdf"]["notifications"]["succeeded"]["status"] == "failed"
    assert states["sending.pdf"]["notifications"]["succeeded"]["status"] == "uncertain"
    assert states["sent.pdf"]["notifications"]["succeeded"]["status"] == "sent"
    assert states["old.pdf"]["journal"] == {"original-pdf:upload": "unchanged"}
    assert "notifications" not in states["old.pdf"]


def test_notification_claim_is_atomic(tmp_path):
    store = Store(tmp_path / "jobs.sqlite3")
    job = store.create("sample.pdf", "digest")
    store.update(job["id"], notifications={"failed": {"status": "pending"}})

    def claim(_):
        return store.update_notification(job["id"], "failed", {"pending"}, status="sending")

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(bool(result) for result in executor.map(claim, range(8))) == 1


def test_missing_recipient_can_be_configured_without_replaying_task(tmp_path):
    config = settings(tmp_path)
    config.feishu_notify_receive_id = ""
    app = create_app(config)
    service = app.state.service
    service.client.close()
    remote = service.client = NotifyingFeishu()
    with TestClient(app) as http:
        job = service.store.create("sample.pdf", "digest")
        service.fail(job["id"], UserError("source failure"))
        _, receipt = wait_notification(http, job["id"], "failed", {"failed"})
        assert "配置" in receipt["error"] and not remote.messages
        config.feishu_notify_receive_id = "ou_configured"
        assert http.post(f"/api/jobs/{job['id']}/notifications/failed/retry").status_code == 202
        wait_notification(http, job["id"], "failed", {"sent"})
        assert remote.messages[0][1] == "ou_configured"
        assert service.store.get(job["id"])["status"] == "failed"


@pytest.mark.parametrize("change", ["recipient", "app", "disabled", "outcome"])
def test_retry_cannot_change_identity_or_send_obsolete_outcome(tmp_path, change):
    config = settings(tmp_path)
    app = create_app(config)
    service = app.state.service
    service.client.close()
    remote = service.client = NotifyingFeishu(UserError("denied"))
    with TestClient(app) as http:
        job = service.store.create("sample.pdf", "digest")
        service.fail(job["id"], UserError("source failure"))
        wait_notification(http, job["id"], "failed", {"failed"})
        if change == "recipient":
            config.feishu_notify_receive_id = "ou_other"
        elif change == "app":
            config.feishu_app_id = "other_app"
        elif change == "disabled":
            config.feishu_notify_enabled = False
        else:
            service.store.update(job["id"], status="succeeded")
        assert http.post(f"/api/jobs/{job['id']}/notifications/failed/retry").status_code == 400
        assert len(remote.messages) == 1


def test_task_result_survives_shutdown_before_notification_can_be_queued(tmp_path):
    app = create_app(settings(tmp_path))
    service = app.state.service
    job = service.store.create("sample.pdf", "digest")
    service.close()
    service.finish(job["id"], status="succeeded", url="https://test.feishu.cn/wiki/child")
    service.store.recover()
    saved = service.store.get(job["id"])
    assert saved["status"] == "succeeded"
    assert saved["notifications"]["succeeded"]["status"] == "failed"
