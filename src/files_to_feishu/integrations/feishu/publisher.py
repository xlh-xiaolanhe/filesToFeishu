import copy
import hashlib
import time
from collections.abc import Callable
from pathlib import Path

from ...models import Element, ParsedDocument, Target, UncertainWrite, UserError
from .blocks import (
    block_text,
    element_blocks,
    empty_text_block,
    plain_text_block,
    text_blocks,
    text_matches,
    text_signature,
)
from .client import FeishuClient


def validate_upload_files(parsed: ParsedDocument, source: Path, assets: Path) -> None:
    """Reject unrecoverable local input failures before creating a remote document."""
    files = [(source, "原附件", "")]
    manifest = {asset.name: asset.digest for asset in parsed.assets}
    asset_root = assets.resolve()
    for element in parsed.elements:
        if element.kind != "image":
            continue
        path = (assets / element.asset).resolve()
        if not element.asset or path.parent != asset_root:
            raise UserError("转换结果的图片素材路径无效，请重新获取或上传来源。")
        digest = ""
        if parsed.source_kind == "wechat":
            digest = manifest.get(element.asset, "")
            if not digest:
                raise UserError("公众号图片缺少素材摘要，无法核验，请重新获取文章。")
        files.append((path, "图片素材", digest))
    checked: dict[Path, str] = {}
    for path, label, digest in files:
        try:
            if not path.is_file():
                raise UserError(f"{label}缺失，请重新获取或上传来源后再发布。")
            size = path.stat().st_size
        except OSError as exc:
            raise UserError(f"无法读取{label}，请检查本地任务文件后重试。") from exc
        if not size or size > 20 * 1024 * 1024:
            raise UserError(f"{label}为空或超过飞书单次上传 20 MB 限制，已停止本次发布。")
        if digest:
            try:
                if path not in checked:
                    checked[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise UserError("无法读取图片素材，请检查本地任务文件后重试。") from exc
            if checked[path] != digest:
                raise UserError("本地图片素材与转换时的摘要不一致，请重新获取文章后再发布。")


class Publisher:
    """Durable write journal: checkpoints are persisted before each remote mutation."""

    def __init__(self, client: FeishuClient, journal: dict, save: Callable, progress: Callable):
        self.client = client
        self.journal = copy.deepcopy(journal)
        self.save = save
        self.progress = progress

    def effect(
        self,
        key: str,
        operation: Callable[[], dict],
        reconcile: Callable[[], dict | None] | None = None,
    ) -> dict:
        entry = self.journal.get(key)
        if entry:
            if entry["state"] == "done":
                return entry["result"]
            if reconcile is not None:
                result = reconcile()
                if result is not None:
                    self.journal[key] = {"state": "done", "result": result}
                    self.save(self.journal)
                    return result
            raise UncertainWrite(
                f"步骤 {key} 的上次写入结果不确定。已停止，避免重复写入；请检查远端文档。"
            )
        self.journal[key] = {"state": "pending"}
        self.save(self.journal)
        try:
            result = operation()
        except UncertainWrite:
            raise
        except UserError:
            del self.journal[key]
            self.save(self.journal)
            raise
        self.journal[key] = {"state": "done", "result": result}
        self.save(self.journal)
        return result

    def reconcile_cell(self, key: str, doc: str, parent: str, blocks: list[dict]) -> dict | None:
        cell = self.client.block(doc, parent)
        if cell.get("block_type") != 32 or not all(plain_text_block(b) for b in blocks):
            return None
        actual = [self.client.block(doc, child) for child in cell.get("children", [])]
        if any(b.get("parent_id") != parent for b in actual):
            raise UncertainWrite("单元格内容归属不一致，已停止恢复，请核对远端文档。")
        candidates = actual
        # Feishu creates a blank paragraph in a new cell. Appending content leaves
        # that paragraph in place; it is not one of our expected created blocks.
        if len(actual) == len(blocks) + 1 and empty_text_block(actual[0]):
            candidates = actual[1:]
        if len(candidates) == len(blocks) and all(
            plain_text_block(a) and text_matches(a, b)
            for a, b in zip(candidates, blocks, strict=True)
        ):
            return {"children": candidates}
        if len(actual) == len(blocks) == 1 and empty_text_block(actual[0]):
            # Legacy journals lack a request token. Reuse the existing placeholder
            # instead of blindly repeating an append whose result was uncertain.
            block_id = actual[0]["block_id"]
            entry = self.journal[key]
            if entry.get("repair_block_id", block_id) != block_id:
                raise UncertainWrite("单元格占位块已改变，已停止恢复，请核对远端文档。")
            entry["repair_block_id"] = block_id
            self.save(self.journal)
            self.client.request(
                "PATCH",
                f"/docx/v1/documents/{doc}/blocks/{block_id}",
                params={"document_revision_id": -1},
                json={"update_text_elements": {"elements": blocks[0]["text"]["elements"]}},
            )
            repaired = self.client.block(doc, block_id)
            if (
                not plain_text_block(repaired)
                or repaired.get("parent_id") != parent
                or not text_matches(repaired, blocks[0])
            ):
                raise UncertainWrite("单元格恢复结果尚未确认，请稍后核对状态并重试。")
            return {"children": [repaired]}
        raise UncertainWrite("单元格已有内容与预期不一致，已停止恢复，请核对远端文档。")

    def reconcile_append(self, key: str, doc: str, parent: str, wanted: list[dict]) -> dict | None:
        baseline = self.journal.get(key + ":before", {}).get("result")
        if baseline is None:
            # Old journals did not record a parent snapshot. They remain conservative.
            return None
        child_ids = self.client.block(doc, parent).get("children", [])
        before = baseline["children"]
        if child_ids[: len(before)] != before or len(child_ids) != len(before) + len(wanted):
            raise UncertainWrite("追加结果尚未确认或远端内容已改变，已停止恢复以避免重复写入。")
        actual = [self.client.block(doc, child) for child in child_ids[len(before) :]]
        for created, expected in zip(actual, wanted, strict=True):
            if created.get("parent_id") != parent:
                raise UncertainWrite("追加块的归属已改变，请核对远端文档。")
            candidate = created
            if expected.get("block_type") == 23 and candidate.get("block_type") == 33:
                children = candidate.get("children", [])
                if len(children) != 1:
                    return None
                candidate = self.client.block(doc, children[0])
                if candidate.get("parent_id") != created["block_id"]:
                    return None
            if candidate.get("block_type") != expected.get("block_type"):
                return None
            if text_signature(expected) is not None:
                if not text_matches(candidate, expected):
                    return None
            elif "table" in expected:
                shape = candidate.get("table", {}).get("property", {})
                if any(shape.get(k) != v for k, v in expected["table"]["property"].items()):
                    return None
            elif not any(field in expected for field in ("file", "image")):
                return None
        return {"children": actual}

    def children(
        self, key: str, doc: str, parent: str, blocks: list[dict], *, cell: bool = False
    ) -> list[dict]:
        if not cell and key not in self.journal:
            # Persist the exact prior child order before the append, so a lost response
            # can be confirmed by readback without issuing the append again.
            self.journal[key + ":before"] = {
                "state": "done",
                "result": {"children": self.client.block(doc, parent).get("children", [])},
            }
            self.save(self.journal)
        result = self.effect(
            key,
            lambda: self.client.request(
                "POST",
                f"/docx/v1/documents/{doc}/blocks/{parent}/children",
                params={"document_revision_id": -1},
                json={"children": blocks, "index": -1},
            ),
            (lambda: self.reconcile_cell(key, doc, parent, blocks))
            if cell
            else (lambda: self.reconcile_append(key, doc, parent, blocks)),
        )
        children = result.get("children", [])
        if len(children) != len(blocks):
            raise UncertainWrite("飞书返回的创建块数量不符，请核对文档。")
        return children

    def media(
        self, key: str, doc: str, source: Path, kind: str, filename: str = "", *, parent: str = ""
    ) -> dict:
        parent = parent or doc
        block_type = 27 if kind == "image" else 23
        created = self.children(
            key + ":create", doc, parent, [{"block_type": block_type, kind: {}}]
        )[0]
        node = created
        for _ in range(4):
            if node.get("block_type") == block_type:
                break
            children = node.get("children", [])
            if len(children) != 1:
                raise UncertainWrite("飞书文件容器结构异常，请核对附件占位块。")
            child = children[0]
            node = child if isinstance(child, dict) else self.client.block(doc, child)
        if node.get("block_type") != block_type:
            raise UncertainWrite("未找到实际文件块，已停止上传。")
        block_id = node["block_id"]
        uploaded = self.effect(
            key + ":upload", lambda: self.client.upload(block_id, source, kind, filename)
        )
        token = uploaded.get("file_token")
        if not token:
            raise UncertainWrite("素材上传结果缺少文件标识。")

        def reconcile_binding():
            block = self.client.block(doc, block_id)
            return {"block": block} if block.get(kind, {}).get("token") == token else None

        self.effect(
            key + ":bind",
            lambda: self.client.request(
                "PATCH",
                f"/docx/v1/documents/{doc}/blocks/{block_id}",
                params={"document_revision_id": -1},
                json={f"replace_{kind}": {"token": token}},
            ),
            reconcile_binding,
        )
        return {
            "id": block_id,
            "kind": kind,
            "token": token,
            "root": created["block_id"],
            "parent": parent,
        }

    def publish(
        self,
        parsed: ParsedDocument,
        source: Path,
        assets: Path,
        title: str,
        target: Target,
        digest: str,
        filename: str,
    ) -> dict:
        validate_upload_files(parsed, source, assets)
        list_ancestors = 0
        for element in parsed.elements:
            if element.list_depth > list_ancestors:
                raise UserError("转换结果中的列表层级缺少父项，请重新获取后核对。")
            list_ancestors = element.list_depth + (element.kind in {"bullet", "ordered"})
            if element.runs and "".join(run.text for run in element.runs) != element.text:
                raise UserError("转换结果的富文本与正文不一致，请重新获取后核对。")
            if element.table_runs and (
                [["".join(run.text for run in cell) for cell in row] for row in element.table_runs]
                != element.rows
            ):
                raise UserError("转换结果的表格富文本与正文不一致，请重新获取后核对。")
            if element.kind == "code":
                if not element.code_reviewed or not element.text.strip():
                    raise UserError("请先在预览中保存所有代码片段的校对结果。")
                if len(element.text) > 20000:
                    raise UserError("单个代码片段超过 20000 字符，请拆分后再发布。")
        document = self.effect(
            "document",
            lambda: self.client.request("POST", "/docx/v1/documents", json={"title": title}),
        )
        doc = document.get("document", {}).get("document_id")
        if not doc:
            raise UncertainWrite("飞书未返回文档标识，请检查是否已创建文档。")
        self.progress("publishing", "正在写入文档", document_id=doc)
        expected: list[dict] = []
        roots: list[str] = []
        list_parents: list[str] = []
        asset_digests = {asset.name: asset.digest for asset in parsed.assets}

        for index, element in enumerate(parsed.elements):
            key = f"element-{index}"
            parent = list_parents[element.list_depth - 1] if element.list_depth else doc
            list_parents = list_parents[: element.list_depth]
            self.progress(
                "publishing", f"写入内容 {index + 1}/{len(parsed.elements)}", document_id=doc
            )
            if element.kind == "image":
                media = self.media(key, doc, assets / element.asset, "image", parent=parent)
                if parsed.source_kind == "wechat":
                    media["asset_digest"] = asset_digests[element.asset]
                expected.append(media)
                if parent == doc:
                    roots.append(media["root"])
            elif element.kind == "table":
                rows = element.rows
                if not rows or not rows[0] or any(len(r) != len(rows[0]) for r in rows):
                    raise UserError("转换结果中表格不规则，已停止发布。")
                table = self.children(
                    key,
                    doc,
                    parent,
                    [
                        {
                            "block_type": 31,
                            "table": {
                                "property": {"row_size": len(rows), "column_size": len(rows[0])}
                            },
                        }
                    ],
                )[0]
                if parent == doc:
                    roots.append(table["block_id"])
                table = self.client.block(doc, table["block_id"])
                cells = table.get("table", {}).get("cells") or table.get("children", [])
                if len(cells) != len(rows) * len(rows[0]):
                    raise UncertainWrite("表格单元格数量不符，已停止发布。")
                for cell_index, text in enumerate(cell for row in rows for cell in row):
                    if element.table_runs:
                        row_index, column_index = divmod(cell_index, len(rows[0]))
                        nodes = element_blocks(
                            Element(
                                kind="text",
                                text=text,
                                runs=element.table_runs[row_index][column_index],
                            )
                        )
                    else:
                        nodes = text_blocks(text)
                    created = self.children(
                        f"{key}:cell-{cell_index}", doc, cells[cell_index], nodes, cell=True
                    )
                    expected.extend(
                        {
                            "id": c["block_id"],
                            "kind": "text",
                            "text": block_text(n),
                            "parent": cells[cell_index],
                            "rich_text": text_signature(n),
                        }
                        for c, n in zip(created, nodes, strict=True)
                    )
                expected.append(
                    {
                        "id": table["block_id"],
                        "kind": "table",
                        "cells": cells,
                        "parent": parent,
                    }
                )
            else:
                nodes = element_blocks(element)
                for start in range(0, len(nodes), 50):
                    batch = nodes[start : start + 50]
                    created = self.children(f"{key}:{start}", doc, parent, batch)
                    if parent == doc:
                        roots.extend(c["block_id"] for c in created)
                    expected.extend(
                        {
                            "id": c["block_id"],
                            "kind": "code" if element.kind == "code" else "text",
                            "text": block_text(n),
                            "parent": parent,
                            "block_type": n["block_type"],
                            "rich_text": text_signature(n),
                            "sequence": n.get("ordered", {}).get("style", {}).get("sequence"),
                            **(
                                {"language": n["code"]["style"].get("language")}
                                if element.kind == "code"
                                else {}
                            ),
                        }
                        for c, n in zip(created, batch, strict=True)
                    )
                if element.kind in {"bullet", "ordered"}:
                    list_parents.append(created[-1]["block_id"])

        original_key = "original-pdf" if parsed.source_kind == "pdf" else "original-wechat"
        attachment = self.media(original_key, doc, source, "file", filename)
        expected.append(attachment)
        roots.append(attachment["root"])
        self.progress("verifying", "核对正文、表格、素材及原附件", document_id=doc)
        self.verify(doc, expected, roots)
        if self.client.download_digest(attachment["token"]) != digest:
            raise UserError("原附件下载摘要不一致，已停止归档。")
        self.journal["verified"] = {
            "state": "done",
            "result": {"expected": expected, "roots": roots},
        }
        self.save(self.journal)
        self.progress("archiving", "正在保存到目标知识库", document_id=doc)

        def reconcile_move():
            try:
                node = self.client.node(doc, "docx")
            except UserError:
                return None
            if (
                node.get("space_id") == target.space_id
                and node.get("parent_node_token") == target.node_token
                and node.get("obj_token") == doc
            ):
                return {"wiki_token": node["node_token"]}
            return None

        moved = self.effect(
            "move",
            lambda: self.client.request(
                "POST",
                f"/wiki/v2/spaces/{target.space_id}/nodes/move_docs_to_wiki",
                json={
                    "obj_type": "docx",
                    "obj_token": doc,
                    "parent_wiki_token": target.node_token,
                    "apply": False,
                },
            ),
            reconcile_move,
        )
        if moved.get("applied"):
            raise UserError("飞书只创建了迁入申请，尚未归档；请检查知识库编辑权限。")
        node = self.confirm_move(doc, target, moved)
        self.progress("verifying", "核对归档后的正文与附件读取权限", document_id=doc)
        self.verify(doc, expected, roots)
        if self.client.download_digest(attachment["token"]) != digest:
            raise UserError("归档后原附件摘要不一致，请核对知识库文档。")
        return {
            "document_id": doc,
            "wiki_token": node["node_token"],
            "url": f"https://{target.host}/wiki/{node['node_token']}",
        }

    def verify(self, doc: str, expected: list[dict], roots: list[str]):
        blocks = {b["block_id"]: b for b in self.client.blocks(doc)}
        nested_children: dict[str, list[str]] = {}
        downloaded: dict[str, str] = {}
        for item in expected:
            block = blocks.get(item["id"], {})
            kind = item["kind"]
            if not block:
                raise UserError("核对失败：远端缺少已写入的内容。")
            parent = item.get("parent")
            if parent:
                outer_id = item.get("root", item["id"])
                if blocks.get(outer_id, {}).get("parent_id") != parent:
                    raise UserError("核对失败：远端内容的父级或列表层级已改变。")
                if parent != doc:
                    nested_children.setdefault(parent, []).append(outer_id)
            if kind in {"text", "code"}:
                if "rich_text" in item and item.get("block_type") in {12, 13}:
                    # Leaf list items must also stay leaves; added children cannot be
                    # hidden by a successful top-level-order comparison.
                    nested_children.setdefault(item["id"], [])
                if (
                    kind == "text"
                    and item.get("block_type") is not None
                    and block.get("block_type") != item["block_type"]
                ):
                    raise UserError("核对失败：远端标题等级或文字块类型已改变。")
                if block_text(block) != item["text"] or block.get("parent_id") != item["parent"]:
                    raise UserError("核对失败：远端文字、单元格内容或顺序已改变。")
                if "rich_text" in item and text_signature(block) != item["rich_text"]:
                    raise UserError("核对失败：远端富文本格式或超链接已改变。")
                if item.get("sequence") is not None and (
                    block.get("ordered", {}).get("style", {}).get("sequence") != item["sequence"]
                ):
                    raise UserError("核对失败：远端有序列表的起始序号已改变。")
                if kind == "code" and (
                    block.get("block_type") != 14
                    or (
                        item.get("language") is not None
                        and block.get("code", {}).get("style", {}).get("language")
                        != item["language"]
                    )
                ):
                    raise UserError("核对失败：远端代码块类型或语言不一致。")
            elif kind == "table":
                cells = block.get("table", {}).get("cells") or block.get("children", [])
                if block.get("block_type") != 31 or cells != item["cells"]:
                    raise UserError("核对失败：远端表格结构不一致。")
            elif block.get(kind, {}).get("token") != item["token"]:
                raise UserError("核对失败：图片或文件附件未正确关联。")
            if kind == "image" and item.get("asset_digest"):
                token = item["token"]
                if token not in downloaded:
                    downloaded[token] = self.client.download_digest(token)
                if downloaded[token] != item["asset_digest"]:
                    raise UserError("核对失败：远端图片原始素材摘要不一致，未确认无损保存。")
        for parent, wanted in nested_children.items():
            cell = blocks.get(parent, {})
            actual = cell.get("children", [])
            if (
                cell.get("block_type") == 32
                and len(actual) == len(wanted) + 1
                and actual[0] not in wanted
                and empty_text_block(blocks.get(actual[0], {}))
            ):
                actual = actual[1:]
            if cell.get("block_type") not in {12, 13, 32} or actual != wanted:
                raise UserError(
                    "核对失败：单元格或嵌套列表内容数量、顺序不一致，可能存在重复写入。"
                )
        root = blocks.get(doc) or self.client.block(doc, doc)
        if root.get("children", []) != roots:
            raise UserError("核对失败：文档顶层内容数量或顺序不一致。")

    def confirm_move(self, doc: str, target: Target, moved: dict) -> dict:
        for _ in range(30):
            if moved.get("task_id"):
                self.client.request(
                    "GET", f"/wiki/v2/tasks/{moved['task_id']}", params={"task_type": "move"}
                )
            try:
                node = self.client.node(
                    moved.get("wiki_token") or doc, "wiki" if moved.get("wiki_token") else "docx"
                )
            except UserError:
                if not moved.get("task_id"):
                    raise
                time.sleep(1)
                continue
            if (
                node.get("space_id") == target.space_id
                and node.get("parent_node_token") == target.node_token
                and node.get("obj_token") == doc
            ):
                return node
            time.sleep(1)
        raise UserError("归档尚未确认完成。请稍后点击重试核对位置，不会重新创建文档。")
