"""Transactional tasks, separately stored publish evidence and atomic content revisions."""

import json
import sqlite3
import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from .job_models import ACTIVE, RUNNING, Job, RevisionConflict, validate_job
from .models import ParsedDocument, UserError

__all__ = ["ACTIVE", "RUNNING", "JobNotFound", "RevisionConflict", "Store"]

INDEX_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT 'queued'",
    "source_kind": "TEXT NOT NULL DEFAULT 'pdf'",
    "deleted_at": "TEXT NOT NULL DEFAULT ''",
    "digest": "TEXT NOT NULL DEFAULT ''",
    "content_digest": "TEXT NOT NULL DEFAULT ''",
    "batch_id": "TEXT NOT NULL DEFAULT ''",
    "source_url": "TEXT NOT NULL DEFAULT ''",
    "app_id": "TEXT NOT NULL DEFAULT ''",
    "parent_token": "TEXT NOT NULL DEFAULT ''",
    "space_id": "TEXT NOT NULL DEFAULT ''",
    "requested_title": "TEXT NOT NULL DEFAULT ''",
    "queued_at": "TEXT NOT NULL DEFAULT ''",
    "intent_digest": "TEXT NOT NULL DEFAULT ''",
    "notification_pending": "INTEGER NOT NULL DEFAULT 0",
}


class JobNotFound(KeyError):
    """A task is absent or has been removed from the visible history."""


class StoredContent(TypedDict):
    parsed_json: str
    content_digest: str
    content_revision: int


def _indexes(job: dict) -> tuple:
    target = job.get("target", {})
    values = {
        **job,
        "parent_token": target.get("node_token", ""),
        "space_id": target.get("space_id", ""),
    }
    values["queued_at"] = job.get("queued_at", job["created_at"])
    values["notification_pending"] = int(
        any(
            receipt.get("status") in {"pending", "sending"}
            for receipt in job.get("notifications", {}).values()
        )
    )
    return tuple(values.get(key, "") for key in INDEX_COLUMNS)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            legacy = "status" not in existing
            for column, specification in INDEX_COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {specification}")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS job_effects "
                "(job_id TEXT NOT NULL, effect_key TEXT NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY (job_id, effect_key))"
            )
            old_journals = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_journals'"
            ).fetchone()
            if old_journals:
                for job_id, raw in conn.execute("SELECT job_id, data FROM job_journals"):
                    self._write_effects(conn, job_id, json.loads(raw))
                conn.execute("DROP TABLE job_journals")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS job_contents (job_id TEXT PRIMARY KEY, "
                "parsed_json TEXT NOT NULL, content_digest TEXT NOT NULL, "
                "content_revision INTEGER NOT NULL)"
            )
            if legacy:
                # All schema/data changes commit together. A corrupt or newer record leaves
                # the original database untouched instead of partially migrating history.
                last_rowid = 0
                while rows := conn.execute(
                    "SELECT rowid, id, data FROM jobs WHERE rowid>? ORDER BY rowid LIMIT 50",
                    (last_rowid,),
                ).fetchall():
                    for rowid, job_id, raw in rows:
                        job = validate_job(json.loads(raw))
                        if job["id"] != job_id:
                            raise UserError("任务记录 ID 不一致；原记录已保留。")
                        self._write(conn, job, journal=job.pop("journal"))
                        last_rowid = rowid
            for name, columns in {
                "history": "deleted_at",
                "queue": "deleted_at, status, queued_at",
                "batch": "deleted_at, batch_id",
                "identity": "source_kind, digest, app_id, space_id, parent_token",
                "content": "source_kind, content_digest, app_id, space_id, parent_token",
                "title": "requested_title, parent_token",
                "intent": "intent_digest",
                "notifications": "deleted_at, notification_pending",
            }.items():
                conn.execute(f"CREATE INDEX IF NOT EXISTS jobs_{name} ON jobs ({columns})")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _summary(raw: str) -> dict:
        job = validate_job(json.loads(raw))
        job.pop("journal", None)
        return job

    @staticmethod
    def _read(
        conn: sqlite3.Connection, job_id: str, *, include_deleted: bool = False, detail: bool = True
    ) -> dict:
        row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        job = validate_job(json.loads(row[0]))
        if job.get("deleted_at") and not include_deleted:
            raise JobNotFound(job_id)
        if detail:
            job["journal"] = {
                key: json.loads(raw)
                for key, raw in conn.execute(
                    "SELECT effect_key, data FROM job_effects WHERE job_id=? ORDER BY rowid",
                    (job_id,),
                )
            }
        else:
            job.pop("journal", None)
        return job

    @staticmethod
    def _write_effects(conn: sqlite3.Connection, job_id: str, journal: dict) -> None:
        # Every effect has its own row: advancing one step does not rewrite all earlier
        # results. Unchanged rows are compared in SQLite and produce no WAL writes.
        existing = {
            row[0]
            for row in conn.execute("SELECT effect_key FROM job_effects WHERE job_id=?", (job_id,))
        }
        for key, value in journal.items():
            conn.execute(
                "INSERT INTO job_effects VALUES (?, ?, ?) ON CONFLICT(job_id, effect_key) "
                "DO UPDATE SET data=excluded.data WHERE data != excluded.data",
                (job_id, key, json.dumps(value, ensure_ascii=False)),
            )
        for key in existing - journal.keys():
            conn.execute("DELETE FROM job_effects WHERE job_id=? AND effect_key=?", (job_id, key))

    @staticmethod
    def _write(conn: sqlite3.Connection, job: dict, *, journal: dict | None = None) -> None:
        # Summary-only updates never rewrite the much larger checkpoint payload.
        summary = {key: value for key, value in job.items() if key != "journal"}
        if journal is not None:
            summary["has_journal"] = bool(journal)
            Store._write_effects(conn, job["id"], journal)
        columns = ", ".join(INDEX_COLUMNS)
        placeholders = ", ".join("?" for _ in range(len(INDEX_COLUMNS) + 2))
        assignments = ", ".join(f"{column}=excluded.{column}" for column in INDEX_COLUMNS)
        conn.execute(
            f"INSERT INTO jobs (id, data, {columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET data=excluded.data, {assignments}",
            (job["id"], json.dumps(summary, ensure_ascii=False), *_indexes(summary)),
        )

    def create(
        self, filename: str, digest: str, *, source_kind: str = "pdf", batch_id: str = ""
    ) -> dict:
        job = validate_job(
            {
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
        )
        with self.connect() as conn:
            self._write(conn, job, journal={})
        return job

    def get(self, job_id: str, *, include_deleted: bool = False) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN")
            return self._read(conn, job_id, include_deleted=include_deleted)

    def get_summary(self, job_id: str, *, include_deleted: bool = False) -> dict:
        with self.connect() as conn:
            return self._read(conn, job_id, include_deleted=include_deleted, detail=False)

    @staticmethod
    def _where(
        *,
        include_deleted: bool = False,
        statuses: Collection[str] | None = None,
        batch_id: str | None = None,
    ) -> tuple[str, list[str]]:
        clauses = [] if include_deleted else ["deleted_at='' "]
        parameters: list[str] = []
        if statuses is not None:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            parameters.extend(statuses)
        if batch_id is not None:
            clauses.append("batch_id=?")
            parameters.append(batch_id)
        return " AND ".join(clauses) or "1", parameters

    def list_summaries(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        include_deleted: bool = False,
        statuses: Collection[str] | None = None,
        batch_id: str | None = None,
    ) -> list[dict]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("limit must be between 1 and 200; offset must be nonnegative")
        where, values = self._where(
            include_deleted=include_deleted, statuses=statuses, batch_id=batch_id
        )
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT data FROM jobs WHERE {where} ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return [self._summary(row[0]) for row in rows]

    def count(
        self,
        *,
        statuses: Collection[str] | None = None,
        batch_id: str | None = None,
        include_deleted: bool = False,
    ) -> int:
        where, values = self._where(
            include_deleted=include_deleted, statuses=statuses, batch_id=batch_id
        )
        with self.connect() as conn:
            return int(
                conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", values).fetchone()[0]
            )

    def next_queued(self) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data FROM jobs WHERE deleted_at='' AND "
                "status IN ('queued', 'publish_queued') "
                "ORDER BY queued_at, rowid LIMIT 1"
            ).fetchone()
        return self._summary(row[0]) if row else None

    def find_candidates(self, *, include_deleted: bool = True, **criteria: str) -> list[dict]:
        """Find indexed summaries; load individual checkpoint details only when needed."""
        allowed = {
            "source_kind",
            "digest",
            "content_digest",
            "source_url",
            "app_id",
            "space_id",
            "parent_token",
            "requested_title",
            "intent_digest",
        }
        if not criteria or not set(criteria) <= allowed:
            raise ValueError("At least one supported identity criterion is required")
        where = " AND ".join(f"{key}=?" for key in criteria)
        if not include_deleted:
            where += " AND deleted_at=''"
        with self.connect() as conn:
            return [
                self._summary(row[0])
                for row in conn.execute(
                    f"SELECT data FROM jobs WHERE {where} ORDER BY rowid DESC",
                    tuple(criteria.values()),
                )
            ]

    def list(self, *, include_deleted: bool = False) -> list[dict]:
        """Compatibility detail API; queue/history callers should use bounded summaries."""
        with self.connect() as conn:
            conn.execute("BEGIN")
            where = "1" if include_deleted else "deleted_at=''"
            rows = conn.execute(f"SELECT id FROM jobs WHERE {where} ORDER BY rowid DESC").fetchall()
            return [self._read(conn, row[0], include_deleted=include_deleted) for row in rows]

    def delete(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._read(conn, job_id, include_deleted=True, detail=False)
            if job.get("deleted_at"):
                return
            if job["status"] in ACTIVE:
                raise UserError("任务正在排队或处理中，请等待完成或取消后再删除。")
            if job["status"] != "succeeded" and (
                job.get("has_journal") or job.get("document_id") or job.get("publish_snapshot")
            ):
                raise UserError("任务已有未完成核验的飞书写入，请先从原任务核对并完成发布。")
            if not Job.model_validate(job).permits("delete"):
                raise UserError("任务通知正在等待发送或发送中，请稍后再删除。")
            job["deleted_at"] = datetime.now(UTC).isoformat()
            self._write(conn, job)

    def update(self, job_id: str, *, load_journal: bool = True, **changes) -> dict:
        if {"id", "content_revision", "content_digest", "content_json"} & changes.keys():
            raise UserError("任务身份和内容版本只能通过原子内容提交更新。")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._read(conn, job_id, detail=False)
            job = validate_job({**job, **changes})
            self._write(conn, job, journal=changes.get("journal"))
            return self._read(conn, job_id, detail=load_journal)

    def claim(self, job_id: str, **changes) -> dict:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._read(conn, job_id, detail=False)
            if not Job.model_validate(job).permits("publish"):
                raise UserError("当前任务正在处理中，请勿重复保存。")
            job = validate_job(
                {
                    **job,
                    **changes,
                    "status": "publish_queued",
                    "error": "",
                    "progress": "等待发布",
                    "queued_at": datetime.now(UTC).isoformat(),
                }
            )
            self._write(conn, job)
            return self._read(conn, job_id)

    def get_content(self, job_id: str, *, include_deleted: bool = False) -> StoredContent | None:
        with self.connect() as conn:
            conn.execute("BEGIN")
            job = self._read(conn, job_id, include_deleted=include_deleted, detail=False)
            row = conn.execute(
                "SELECT parsed_json, content_digest, content_revision "
                "FROM job_contents WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            if job.get("content_revision", 0):
                raise UserError("任务内容版本缺失，请核对本地数据后重新确认。")
            return None
        if (row[1], row[2]) != (job.get("content_digest"), job.get("content_revision")):
            raise UserError("任务内容与摘要版本不一致，请核对本地数据后重新确认。")
        return {"parsed_json": row[0], "content_digest": row[1], "content_revision": row[2]}

    def commit_content(
        self,
        job_id: str,
        parsed_json: str,
        content_digest: str,
        expected_revision: int | None = None,
        **changes,
    ) -> dict:
        """Commit the canonical preview and its identity in one compare-and-set transaction."""
        parsed = ParsedDocument.model_validate_json(parsed_json)
        if (
            not content_digest
            or {"id", "content_revision", "content_digest", "journal"} & changes.keys()
        ):
            raise UserError("内容摘要不能为空，且不能覆盖任务身份、版本或发布日志。")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._read(conn, job_id, detail=False)
            revision = job.get("content_revision", 0)
            if (revision and expected_revision is None) or (
                expected_revision is not None and revision != expected_revision
            ):
                raise RevisionConflict("预览内容已被其他窗口更新，请保留草稿并重新打开任务核对。")
            if revision and not Job.model_validate(job).permits("edit"):
                raise UserError("已开始发布或正在处理的任务不能修改内容。")
            if parsed.source_kind != job.get("source_kind", "pdf"):
                raise UserError("解析内容与任务来源类型不一致。")
            job = validate_job(
                {
                    **job,
                    **changes,
                    "content_revision": revision + 1,
                    "content_digest": content_digest,
                }
            )
            conn.execute(
                "INSERT INTO job_contents VALUES (?, ?, ?, ?) ON CONFLICT(job_id) "
                "DO UPDATE SET parsed_json=excluded.parsed_json, "
                "content_digest=excluded.content_digest, "
                "content_revision=excluded.content_revision",
                (job_id, parsed_json, content_digest, revision + 1),
            )
            self._write(conn, job)
            return self._read(conn, job_id)

    def recover(self) -> None:
        with self.connect() as conn:
            active = ",".join("?" for _ in ACTIVE)
            ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT id FROM jobs WHERE deleted_at='' AND (status IN ({active}) "
                    "OR notification_pending=1)",
                    tuple(ACTIVE),
                )
            ]
        for job_id in ids:
            job = self.get_summary(job_id)
            for event, receipt in job.get("notifications", {}).items():
                if receipt["status"] in {"pending", "sending"}:
                    sending = receipt["status"] == "sending"
                    self.update_notification(
                        job_id,
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
                        job_id,
                        status="failed",
                        progress="获取已中断",
                        error="上次获取已中断，临时浏览器会话已失效。请重新获取文章。",
                    )
                else:
                    self.update(
                        job_id,
                        status="needs_review",
                        progress="任务已中断",
                        error="上次运行中断。请核对任务；已解析任务可点击重试，写入前会检查远端状态。",
                    )

    def update_notification(
        self, job_id: str, event: str, expected: set[str], **changes
    ) -> dict | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._read(conn, job_id, detail=False)
            receipt = job.get("notifications", {}).get(event)
            if receipt is None or receipt["status"] not in expected:
                return None
            receipt.update(changes)
            self._write(conn, validate_job(job))
            return receipt
