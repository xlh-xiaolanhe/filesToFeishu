import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import UserError

ACTIVE = {
    "queued",
    "publish_queued",
    "parsing",
    "fetching",
    "waiting_verification",
    "cancelling",
    "publishing",
    "verifying",
    "archiving",
}
RUNNING = ACTIVE - {"queued", "publish_queued"}


class JobNotFound(KeyError):
    """A task is absent or has been removed from the visible history."""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def create(
        self, filename: str, digest: str, *, source_kind: str = "pdf", batch_id: str = ""
    ) -> dict:
        job = {
            "id": uuid.uuid4().hex,
            "filename": filename,
            "source_kind": source_kind,
            "batch_id": batch_id,
            "digest": digest,
            "status": "queued",
            "progress": "等待解析",
            "created_at": datetime.now(UTC).isoformat(),
            "error": "",
            "document_id": "",
            "url": "",
            "journal": {},
        }
        with self.connect() as conn:
            conn.execute("INSERT INTO jobs VALUES (?, ?)", (job["id"], json.dumps(job)))
        return job

    def get(self, job_id: str, *, include_deleted: bool = False) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        job = json.loads(row[0])
        if job.get("deleted_at") and not include_deleted:
            raise JobNotFound(job_id)
        return job

    def list(self, *, include_deleted: bool = False) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT data FROM jobs ORDER BY rowid DESC").fetchall()
        jobs = [json.loads(row[0]) for row in rows]
        return jobs if include_deleted else [job for job in jobs if not job.get("deleted_at")]

    def delete(self, job_id: str) -> None:
        """Remove inactive history while retaining files/checkpoints for duplicate verification."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            job = json.loads(row[0])
            if job.get("deleted_at"):
                return
            if job["status"] in ACTIVE:
                raise UserError("任务正在排队或处理中，请等待完成或取消后再删除。")
            if job["status"] != "succeeded" and (job.get("journal") or job.get("document_id")):
                raise UserError("任务已有未完成核验的飞书写入，请先从原任务核对并完成发布。")
            if any(
                receipt["status"] in {"pending", "sending"}
                for receipt in job.get("notifications", {}).values()
            ):
                raise UserError("任务通知正在等待发送或发送中，请稍后再删除。")
            job["deleted_at"] = datetime.now(UTC).isoformat()
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job_id))

    def update(self, job_id: str, **changes) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            job = json.loads(row[0])
            if job.get("deleted_at"):
                raise JobNotFound(job_id)
            job.update(changes)
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job_id))
        return job

    def claim(self, job_id: str, **changes) -> dict:
        """Serialize publish claims, including duplicate clicks from different tabs."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            jobs = [json.loads(row[0]) for row in conn.execute("SELECT data FROM jobs")]
            job = next((j for j in jobs if j["id"] == job_id), None)
            if job is None or job.get("deleted_at"):
                raise JobNotFound(job_id)
            if job["status"] not in {"ready", "failed", "needs_review", "succeeded"}:
                raise UserError("当前任务正在处理中，请勿重复保存。")
            job.update(
                changes,
                status="publish_queued",
                error="",
                progress="等待发布",
                queued_at=datetime.now(UTC).isoformat(),
            )
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job_id))
        return job

    def recover(self):
        for job in self.list():
            for event, receipt in job.get("notifications", {}).items():
                if receipt["status"] in {"pending", "sending"}:
                    sending = receipt["status"] == "sending"
                    self.update_notification(
                        job["id"],
                        event,
                        {receipt["status"]},
                        status="uncertain" if sending else "failed",
                        error=(
                            "发送时服务中断，结果不确定，请在飞书核对；未自动重发。"
                            if sending
                            else "发送前服务中断，可重试通知；未自动补发。"
                        ),
                    )
            if job["status"] in ACTIVE:
                if job.get("source_kind") == "wechat" and job["status"] in {
                    "queued",
                    "fetching",
                    "waiting_verification",
                    "cancelling",
                }:
                    self.update(
                        job["id"],
                        status="failed",
                        progress="获取已中断",
                        error="上次获取已中断，临时浏览器会话已失效。请重新获取文章。",
                    )
                    continue
                self.update(
                    job["id"],
                    status="needs_review",
                    progress="任务已中断",
                    error="上次运行中断。请核对任务；已解析任务可点击重试，写入前会检查远端状态。",
                )

    def update_notification(
        self, job_id: str, event: str, expected: set[str], **changes
    ) -> dict | None:
        """Compare-and-set a delivery receipt without changing task or publish checkpoints."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            job = json.loads(row[0])
            if job.get("deleted_at"):
                raise JobNotFound(job_id)
            receipt = job.get("notifications", {}).get(event)
            if receipt is None or receipt["status"] not in expected:
                return None
            receipt.update(changes)
            conn.execute("UPDATE jobs SET data=? WHERE id=?", (json.dumps(job), job_id))
        return receipt
