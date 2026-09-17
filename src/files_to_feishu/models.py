from typing import Literal

from pydantic import BaseModel, Field


class Notice(BaseModel):
    page: int
    reason: str


class Element(BaseModel):
    kind: Literal["text", "heading", "bullet", "ordered", "table", "image"]
    page: int
    text: str = ""
    level: int = 1
    rows: list[list[str]] = Field(default_factory=list)
    asset: str = ""
    bbox: list[float] = Field(default_factory=list)


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
