import copy
import time
from collections.abc import Callable
from pathlib import Path

from ...models import ParsedDocument, Target, UncertainWrite, UserError
from .client import FeishuClient


def text_blocks(text: str, kind: str = "text", level: int = 1) -> list[dict]:
    block_type, field = {
        "text": (2, "text"),
        "bullet": (12, "bullet"),
        "ordered": (13, "ordered"),
        "heading": (2 + level, f"heading{level}"),
    }[kind]
    return [
        {
            "block_type": block_type,
            field: {"elements": [{"text_run": {"content": text[start : start + 1500]}}]},
        }
        for start in range(0, max(len(text), 1), 1500)
    ]


def block_text(block: dict) -> str:
    for field in ["text", "bullet", "ordered"] + [f"heading{i}" for i in range(1, 10)]:
        if field in block:
            return "".join(
                e.get("text_run", {}).get("content", "") for e in block[field].get("elements", [])
            )
    return ""


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

    def children(self, key: str, doc: str, parent: str, blocks: list[dict]) -> list[dict]:
        result = self.effect(
            key,
            lambda: self.client.request(
                "POST",
                f"/docx/v1/documents/{doc}/blocks/{parent}/children",
                params={"document_revision_id": -1},
                json={"children": blocks, "index": -1},
            ),
        )
        children = result.get("children", [])
        if len(children) != len(blocks):
            raise UncertainWrite("飞书返回的创建块数量不符，请核对文档。")
        return children

    def media(self, key: str, doc: str, source: Path, kind: str, filename: str = "") -> dict:
        block_type = 27 if kind == "image" else 23
        created = self.children(key + ":create", doc, doc, [{"block_type": block_type, kind: {}}])[
            0
        ]
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
        return {"id": block_id, "kind": kind, "token": token, "root": created["block_id"]}

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

        for index, element in enumerate(parsed.elements):
            key = f"element-{index}"
            self.progress(
                "publishing", f"写入内容 {index + 1}/{len(parsed.elements)}", document_id=doc
            )
            if element.kind == "image":
                media = self.media(key, doc, assets / element.asset, "image")
                expected.append(media)
                roots.append(media["root"])
            elif element.kind == "table":
                rows = element.rows
                if not rows or not rows[0] or any(len(r) != len(rows[0]) for r in rows):
                    raise UserError("转换结果中表格不规则，已停止发布。")
                table = self.children(
                    key,
                    doc,
                    doc,
                    [
                        {
                            "block_type": 31,
                            "table": {
                                "property": {"row_size": len(rows), "column_size": len(rows[0])}
                            },
                        }
                    ],
                )[0]
                roots.append(table["block_id"])
                table = self.client.block(doc, table["block_id"])
                cells = table.get("table", {}).get("cells") or table.get("children", [])
                if len(cells) != len(rows) * len(rows[0]):
                    raise UncertainWrite("表格单元格数量不符，已停止发布。")
                for cell_index, text in enumerate(cell for row in rows for cell in row):
                    nodes = text_blocks(text)
                    created = self.children(
                        f"{key}:cell-{cell_index}", doc, cells[cell_index], nodes
                    )
                    expected.extend(
                        {
                            "id": c["block_id"],
                            "kind": "text",
                            "text": block_text(n),
                            "parent": cells[cell_index],
                        }
                        for c, n in zip(created, nodes, strict=True)
                    )
                expected.append({"id": table["block_id"], "kind": "table", "cells": cells})
            else:
                nodes = text_blocks(element.text, element.kind, element.level)
                for start in range(0, len(nodes), 50):
                    batch = nodes[start : start + 50]
                    created = self.children(f"{key}:{start}", doc, doc, batch)
                    roots.extend(c["block_id"] for c in created)
                    expected.extend(
                        {"id": c["block_id"], "kind": "text", "text": block_text(n), "parent": doc}
                        for c, n in zip(created, batch, strict=True)
                    )

        attachment = self.media("original-pdf", doc, source, "file", filename)
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
        for item in expected:
            block = blocks.get(item["id"], {})
            kind = item["kind"]
            if not block:
                raise UserError("核对失败：远端缺少已写入的内容。")
            if kind == "text":
                if block_text(block) != item["text"] or block.get("parent_id") != item["parent"]:
                    raise UserError("核对失败：远端文字、单元格内容或顺序已改变。")
            elif kind == "table":
                cells = block.get("table", {}).get("cells") or block.get("children", [])
                if cells != item["cells"]:
                    raise UserError("核对失败：远端表格结构不一致。")
            elif block.get(kind, {}).get("token") != item["token"]:
                raise UserError("核对失败：图片或文件附件未正确关联。")
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
