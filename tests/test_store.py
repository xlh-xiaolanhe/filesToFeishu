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
