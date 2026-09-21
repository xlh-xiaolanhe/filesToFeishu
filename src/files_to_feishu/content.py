"""Content identity and immutable publication contracts shared by all sources."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ValidationError

from .models import ParsedDocument, Target, UserError


def article_digest(parsed: ParsedDocument) -> str:
    """Hash semantic content, excluding expiring URLs and extraction-only metadata."""
    identity = parsed.model_dump(
        mode="json",
        exclude={
            "assets": {"__all__": {"source_url"}},
            "elements": {"__all__": {"web_locator", "code_origin", "code_reviewed"}},
        },
    )
    return digest_json(identity)


def digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class PublishSnapshot(BaseModel):
    schema_version: Literal[1] = 1
    content_revision: int
    content_digest: str
    review_token: str
    source_kind: Literal["pdf", "wechat"]
    source_url: str = ""
    source_digest: str
    original_digest: str
    filename: str
    uploads: dict[str, str]
    app_id: str
    title: str
    target: Target

    @classmethod
    def from_record(cls, value: object) -> "PublishSnapshot":
        try:
            return cls.model_validate(value)
        except ValidationError as exc:
            raise UserError("发布快照格式或版本不受支持，已保留现场并停止写入。") from exc

    @property
    def intent_digest(self) -> str:
        # ZIP metadata (e.g. capture time) varies across identical article imports.
        # Semantic content and asset digests identify articles; PDFs use original bytes too.
        return digest_json(
            {
                "version": 1,
                "content_digest": self.content_digest,
                "source_kind": self.source_kind,
                "source_url": self.source_url,
                "source_digest": self.source_digest,
                "uploads": {k: v for k, v in self.uploads.items() if k != "original-wechat"},
                "app_id": self.app_id,
                "title": self.title,
                "space_id": self.target.space_id,
                "node_token": self.target.node_token,
            }
        )

    def assert_content(self, parsed: ParsedDocument, revision: int) -> None:
        if revision != self.content_revision or article_digest(parsed) != self.content_digest:
            raise UserError("发布快照与当前内容不一致，已停止；请核对任务恢复现场。")
