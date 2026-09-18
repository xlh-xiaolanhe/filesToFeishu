from typing import Literal

from pydantic import BaseModel, Field


class Notice(BaseModel):
    page: int
    reason: str


CodeLanguage = Literal["plaintext", "javascript", "typescript"]


class CodeSource(BaseModel):
    page: int
    asset: str = ""
    bbox: list[float] = Field(default_factory=list)


class Element(BaseModel):
    kind: Literal["text", "heading", "bullet", "ordered", "table", "image", "code"]
    page: int
    text: str = ""
    level: int = 1
    rows: list[list[str]] = Field(default_factory=list)
    asset: str = ""
    bbox: list[float] = Field(default_factory=list)
    language: CodeLanguage = "plaintext"
    code_origin: Literal["", "pdf_text", "ocr", "manual"] = ""
    code_reviewed: bool = False
    code_sources: list[CodeSource] = Field(default_factory=list)


class ParsedDocument(BaseModel):
    pages: int
    elements: list[Element]
    notices: list[Notice] = Field(default_factory=list)
    page_images: list[str] = Field(default_factory=list)


class Target(BaseModel):
    space_id: str
    node_token: str
    title: str
    host: str


class UserError(Exception):
    """A bounded, actionable message suitable for displaying to the local user."""


class UncertainWrite(UserError):
    """The remote mutation may have succeeded; repeating it could duplicate content."""
