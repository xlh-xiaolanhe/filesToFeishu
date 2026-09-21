import pytest

from files_to_feishu.store import Store


def test_job_survives_restart_and_uncertain_work_never_becomes_success(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    job = store.create("report.pdf", "abc123")
    store.update(job["id"], status="publishing", document_id="doc123")
    reopened = Store(tmp_path / "jobs.sqlite")
    reopened.recover()
    recovered = reopened.get(job["id"])
    assert recovered["status"] == "needs_review"
    assert recovered["document_id"] == "doc123"
    assert "中断" in recovered["error"]


@pytest.mark.parametrize("status", ["queued", "fetching", "waiting_verification", "cancelling"])
def test_article_restart_discards_session_and_requires_new_fetch(tmp_path, status):
    store = Store(tmp_path / "jobs.sqlite")
    job = store.create("article.zip", "", source_kind="wechat")
    store.update(job["id"], status=status)
    store.recover()
    restored = store.get(job["id"])
    assert restored["status"] == "failed"
    assert "重新获取" in restored["error"]
    assert not restored["journal"]
