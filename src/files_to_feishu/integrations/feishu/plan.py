"""Versioned local publication plans, validated before the first remote mutation."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from ...models import Element, ParsedDocument, Target, UserError
from .blocks import element_blocks, text_blocks

PLAN_VERSION = 1
JOURNAL_VERSION = 1


class EffectRecord(BaseModel):
    """Unversioned historical records are version zero and keep their original keys."""

    schema_version: Literal[0, 1] = 0
    state: Literal["pending", "done"]
    result: dict = Field(default_factory=dict)
    input: dict = Field(default_factory=dict)
    started_at: float | None = None
    recovery: dict = Field(default_factory=dict)


@dataclass(frozen=True)
class PlannedElement:
    element: Element
    blocks: tuple[dict, ...]
    cells: tuple[tuple[dict, ...], ...] = ()


@dataclass(frozen=True)
class PublicationPlan:
    fingerprint: str
    context: dict
    elements: tuple[PlannedElement, ...]
    uploads: dict[str, str]


def canonical_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def file_digest(path: Path, label: str) -> str:
    try:
        if not path.is_file():
            raise UserError(f"{label}缺失，请重新获取或上传来源后再发布。")
        size = path.stat().st_size
        if not size or size > 20 * 1024 * 1024:
            raise UserError(f"{label}为空或超过飞书单次上传 20 MB 限制，已停止本次发布。")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise UserError(f"无法读取{label}，请检查本地任务文件后重试。") from exc


def validate_upload_files(parsed: ParsedDocument, source: Path, assets: Path) -> dict[str, str]:
    """Read each referenced file now, including PDFs, before creating remote objects."""
    original_key = "original-pdf" if parsed.source_kind == "pdf" else "original-wechat"
    uploads = {original_key: file_digest(source, "原附件")}
    manifest = {asset.name: asset.digest for asset in parsed.assets}
    if len(manifest) != len(parsed.assets):
        raise UserError("转换结果中包含重复的素材名称，请重新获取来源。")
    asset_root = assets.resolve()
    checked: dict[Path, str] = {}
    for index, element in enumerate(parsed.elements):
        if element.kind != "image":
            continue
        path = (assets / element.asset).resolve()
        if not element.asset or path.parent != asset_root:
            raise UserError("转换结果的图片素材路径无效，请重新获取或上传来源。")
        if path not in checked:
            checked[path] = file_digest(path, "图片素材")
        if parsed.source_kind == "wechat":
            if not manifest.get(element.asset):
                raise UserError("公众号图片缺少素材摘要，无法核验，请重新获取文章。")
            if checked[path] != manifest[element.asset]:
                raise UserError("本地图片素材与转换时的摘要不一致，请重新获取文章后再发布。")
        uploads[f"element-{index}"] = checked[path]
    return uploads


def build_plan(
    parsed: ParsedDocument,
    source: Path,
    assets: Path,
    title: str,
    target: Target,
    digest: str,
    filename: str,
    app_id: str,
) -> PublicationPlan:
    """Compile every block and cell locally; the executor only consumes this plan."""
    if not title.strip() or len(title) > 800:
        raise UserError("文档标题不能为空或超过 800 字符，请调整后重新确认。")
    if not filename or Path(filename).name != filename or "\x00" in filename:
        raise UserError("原附件名称无效，请重新获取来源。")
    if not target.space_id or not target.node_token:
        raise UserError("知识库目标不完整，请重新检查保存位置。")
    uploads = validate_upload_files(parsed, source, assets)
    original_key = "original-pdf" if parsed.source_kind == "pdf" else "original-wechat"
    if uploads[original_key] != digest:
        raise UserError("原附件摘要与已确认内容不一致，请重新获取来源后核对。")
    planned = []
    list_ancestors = 0
    for original in parsed.elements:
        element = original.model_copy(deep=True)
        if element.list_depth > list_ancestors:
            raise UserError("转换结果中的列表层级缺少父项，请重新获取后核对。")
        list_ancestors = element.list_depth + (element.kind in {"bullet", "ordered"})
        if element.kind == "heading" and not 1 <= element.level <= 9:
            raise UserError("转换结果中的标题等级无效，必须在 1 到 9 之间。")
        if element.list_start is not None and element.kind != "ordered":
            raise UserError("只有有序列表可以设置起始序号，请重新获取后核对。")
        if element.runs and "".join(run.text for run in element.runs) != element.text:
            raise UserError("转换结果的富文本与正文不一致，请重新获取后核对。")
        if element.table_runs and (
            [["".join(run.text for run in cell) for cell in row] for row in element.table_runs]
            != element.rows
        ):
            raise UserError("转换结果的表格富文本与正文不一致，请重新获取后核对。")
        runs = element.runs + [run for row in element.table_runs for cell in row for run in cell]
        for run in runs:
            if run.link and urlsplit(run.link).scheme.lower() not in {"http", "https", "mailto"}:
                raise UserError("转换结果中包含不支持的超链接地址，请先校对链接。")
        if element.kind == "code":
            if not element.code_reviewed or not element.text.strip():
                raise UserError("请先在预览中保存所有代码片段的校对结果。")
            if len(element.text) > 20000:
                raise UserError("单个代码片段超过 20000 字符，请拆分后再发布。")
        cells: list[tuple[dict, ...]] = []
        if element.kind == "table":
            rows = element.rows
            if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
                raise UserError("转换结果中表格不规则，已在创建文档前停止发布。")
            nodes = [
                {
                    "block_type": 31,
                    "table": {
                        "property": {
                            "row_size": len(rows),
                            "column_size": len(rows[0]),
                        }
                    },
                }
            ]
            for row_index, row in enumerate(rows):
                for column_index, text in enumerate(row):
                    cell_nodes = (
                        element_blocks(
                            Element(
                                kind="text",
                                text=text,
                                runs=element.table_runs[row_index][column_index],
                            )
                        )
                        if element.table_runs
                        else text_blocks(text)
                    )
                    cells.append(tuple(cell_nodes))
        elif element.kind == "image":
            nodes = []
        else:
            nodes = element_blocks(element)
        planned.append(PlannedElement(element, tuple(nodes), tuple(cells)))
    context = {
        "version": PLAN_VERSION,
        "source_kind": parsed.source_kind,
        "title": title,
        "target": target.model_dump(),
        "app_id": app_id,
        "filename": filename,
        "uploads": uploads,
    }
    fingerprint = canonical_digest(
        {
            **context,
            "elements": [
                {"element": item.element.model_dump(), "blocks": item.blocks, "cells": item.cells}
                for item in planned
            ],
        }
    )
    return PublicationPlan(fingerprint, context, tuple(planned), uploads)
