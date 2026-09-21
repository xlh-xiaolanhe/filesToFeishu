"""Versioned task records and the rules for user-initiated task operations."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from .models import UserError


class JobState(StrEnum):
    QUEUED = "queued"
    PUBLISH_QUEUED = "publish_queued"
    PARSING = "parsing"
    FETCHING = "fetching"
    WAITING_VERIFICATION = "waiting_verification"
    CANCELLING = "cancelling"
    PUBLISHING = "publishing"
    VERIFYING = "verifying"
    ARCHIVING = "archiving"
    READY = "ready"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"


ACTIVE = frozenset(
    {
        JobState.QUEUED,
        JobState.PUBLISH_QUEUED,
        JobState.PARSING,
        JobState.FETCHING,
        JobState.WAITING_VERIFICATION,
        JobState.CANCELLING,
        JobState.PUBLISHING,
        JobState.VERIFYING,
        JobState.ARCHIVING,
    }
)
RUNNING = ACTIVE - {JobState.QUEUED, JobState.PUBLISH_QUEUED}
PUBLISHABLE = frozenset(
    {JobState.READY, JobState.FAILED, JobState.NEEDS_REVIEW, JobState.SUCCEEDED}
)


class NotificationReceipt(BaseModel):
    """Older partial receipts need only a known delivery state; preserve other fields."""

    model_config = ConfigDict(extra="allow")
    status: Literal["pending", "sending", "sent", "failed", "uncertain"]


class Job(BaseModel):
    """Validate known fields without discarding unrecognized historical metadata."""

    model_config = ConfigDict(extra="allow")
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1)
    filename: str
    digest: str
    status: JobState
    created_at: str
    source_kind: Literal["pdf", "wechat"] = "pdf"
    content_revision: int = Field(default=0, ge=0)
    content_digest: str = ""
    batch_id: str = ""
    source_url: str = ""
    app_id: str = ""
    requested_title: str = ""
    queued_at: str = ""
    intent_digest: str = ""
    target: dict[str, str] = Field(default_factory=dict)
    document_id: str = ""
    deleted_at: str = ""
    journal: dict[str, JsonValue] = Field(default_factory=dict)
    notifications: dict[str, NotificationReceipt] = Field(default_factory=dict)

    @property
    def content_locked(self) -> bool:
        return bool(
            self.journal
            or self.document_id
            or getattr(self, "has_journal", False)
            or getattr(self, "publish_snapshot", None)
            or self.status
            in {
                JobState.PUBLISH_QUEUED,
                JobState.PUBLISHING,
                JobState.VERIFYING,
                JobState.ARCHIVING,
            }
        )

    def permits(self, operation: Literal["edit", "publish", "delete", "cancel"]) -> bool:
        if self.deleted_at:
            return False
        if operation == "edit":
            return self.status == JobState.READY and not self.content_locked
        if operation == "publish":
            return self.status in PUBLISHABLE
        if operation == "cancel":
            return self.status == JobState.QUEUED or (
                self.source_kind == "wechat"
                and self.status
                in {JobState.FETCHING, JobState.WAITING_VERIFICATION, JobState.CANCELLING}
            )
        return (
            self.status not in ACTIVE
            and (self.status == JobState.SUCCEEDED or not self.content_locked)
            and not any(
                receipt.status in {"pending", "sending"} for receipt in self.notifications.values()
            )
        )


def validate_job(data: dict) -> dict:
    """Missing version/source fields are legacy v1; unknown future versions are rejected."""
    if data.get("schema_version", 1) != 1:
        raise UserError("任务数据版本不受支持，请升级程序后再打开；原记录已保留。")
    try:
        job = Job.model_validate(data)
    except ValidationError as exc:
        raise UserError("任务记录字段或状态无效；原记录已保留，请核对数据版本。") from exc
    result = job.model_dump(mode="json", exclude_unset=True)
    result.setdefault("schema_version", 1)
    result.setdefault("source_kind", "pdf")
    result.setdefault("content_revision", 0)
    result.setdefault("journal", {})
    return result


class RevisionConflict(UserError):
    """The supplied edit baseline no longer identifies the committed content."""


def allowed_actions(data: dict) -> list[str]:
    """Expose the same state-based operation policy to HTTP responses and the UI."""
    job = Job.model_validate(data)
    operations: tuple[Literal["edit", "publish", "delete", "cancel"], ...] = (
        "edit",
        "publish",
        "delete",
        "cancel",
    )
    return [operation for operation in operations if job.permits(operation)]
