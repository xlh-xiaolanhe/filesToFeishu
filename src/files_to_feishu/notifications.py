"""Persisted, independent delivery receipts for completed task outcomes."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from .config import Settings

NotificationEvent = Literal["succeeded", "failed", "needs_review"]
EVENT_LABELS = {"succeeded": "已保存到飞书", "failed": "处理失败", "needs_review": "需要核对"}


class Notification(BaseModel):
    event: NotificationEvent
    status: Literal["pending", "sending", "sent", "failed", "uncertain"] = "pending"
    uuid: str
    app_id: str
    receive_id_type: str
    receive_id: str
    text: str
    created_at: str
    message_id: str = ""
    error: str = ""

    def public(self) -> dict:
        return self.model_dump(include={"event", "status", "created_at", "message_id", "error"})


def prepare_notification(settings: Settings, job: dict) -> Notification | None:
    """Only explicit opt-in terminal outcomes generate receipts, never historical replays."""
    event = job["status"]
    if not settings.feishu_notify_enabled or event not in EVENT_LABELS:
        return None
    if event in job.get("notifications", {}):
        return None
    title = " ".join(str(job.get("title") or job["filename"]).split())[:200]
    # Feishu text messages recognize <at> markup; source titles must never become mentions.
    title = title.replace("<", "＜").replace(">", "＞")
    # Send no document body, local paths, raw exception strings or authentication data.
    lines = [f"文档转换结果：{EVENT_LABELS[event]}", title, f"任务：{job['id']}"]
    if job.get("batch_id"):
        lines.append(f"批次：{job['batch_id']}")
    if event == "succeeded":
        lines.extend(["正文、附件和知识库位置已核验。", job["url"]])
    elif event == "needs_review":
        lines.append("处理结果不确定，请回本机任务页面核对；不要重复创建文档。")
    else:
        lines.append("请回本机任务页面查看错误并处理，其余排队任务会继续。")
    return Notification(
        event=event,
        uuid=str(uuid.uuid4()),
        app_id=settings.feishu_app_id,
        receive_id_type=settings.feishu_notify_receive_id_type,
        receive_id=settings.feishu_notify_receive_id.strip(),
        text="\n".join(lines),
        created_at=datetime.now(UTC).isoformat(),
    )
