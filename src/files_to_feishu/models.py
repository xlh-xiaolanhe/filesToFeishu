from typing import Literal

from pydantic import BaseModel, Field


class Notice(BaseModel):
    page: int | None = None
    reason: str


CodeLanguage = Literal["plaintext", "javascript", "typescript"]


class CodeSource(BaseModel):
    page: int
    asset: str = ""
    bbox: list[float] = Field(default_factory=list)


class TextRun(BaseModel):
    text: str
    bold: bool = False
    italic: bool = False
    strike: bool = False
    inline_code: bool = False
    link: str = ""


class SourceMetadata(BaseModel):
    url: str = ""
    title: str = ""
    account: str = ""
    author: str = ""
    published_at: str = ""


class Asset(BaseModel):
    name: str
    media_type: str
    digest: str
    source_url: str = ""


class Element(BaseModel):
    kind: Literal["text", "heading", "bullet", "ordered", "table", "image", "code", "quote"]
    page: int | None = None
    text: str = ""
    level: int = 1
    rows: list[list[str]] = Field(default_factory=list)
    asset: str = ""
    bbox: list[float] = Field(default_factory=list)
    language: CodeLanguage = "plaintext"
    code_origin: Literal["", "pdf_text", "ocr", "manual", "html"] = ""
    code_reviewed: bool = False
    code_sources: list[CodeSource] = Field(default_factory=list)
    runs: list[TextRun] = Field(default_factory=list)
    table_runs: list[list[list[TextRun]]] = Field(default_factory=list)
    list_depth: int = Field(default=0, ge=0, le=8)
    list_start: int | None = Field(default=None, ge=1)
    web_locator: str = ""


class ParsedDocument(BaseModel):
    pages: int | None = None
    elements: list[Element]
    notices: list[Notice] = Field(default_factory=list)
    page_images: list[str] = Field(default_factory=list)
    source_kind: Literal["pdf", "wechat"] = "pdf"
    metadata: SourceMetadata = Field(default_factory=SourceMetadata)
    assets: list[Asset] = Field(default_factory=list)


class Target(BaseModel):
    space_id: str
    node_token: str
    title: str
    host: str


class UserError(Exception):
    """A bounded, actionable message suitable for displaying to the local user."""


class UncertainWrite(UserError):
    """The remote mutation may have succeeded; repeating it could duplicate content."""
