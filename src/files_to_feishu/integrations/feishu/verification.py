"""Read-only matching of a remote document against the current publication plan."""

from typing import NoReturn

from ...models import UserError
from .blocks import empty_text_block, text_matches
from .client import FeishuClient
from .plan import PublicationPlan


class PlanVerifier:
    """Consume the actual tree in plan order, never historical success expectations."""

    def __init__(self, client: FeishuClient, document_id: str):
        self.client = client
        self.doc = document_id
        self.blocks = {block["block_id"]: block for block in client.blocks(document_id)}
        self.positions: dict[str, int] = {document_id: 0}
        self.downloads: dict[str, str] = {}

    @staticmethod
    def fail() -> NoReturn:
        raise UserError("核对失败：已有文档与本次确认的发布计划不一致；未创建新文档。")

    def block(self, block_id: str) -> dict:
        block = self.blocks.get(block_id)
        if block is None:
            self.fail()
        return block

    def take(self, parent: str, count: int = 1) -> list[dict]:
        container = self.block(parent)
        children = container.get("children", [])
        start = self.positions.get(parent, 0)
        if start + count > len(children):
            self.fail()
        self.positions[parent] = start + count
        result = [self.block(child) for child in children[start : start + count]]
        if any(block.get("parent_id") != parent for block in result):
            self.fail()
        return result

    def text(self, parent: str, wanted: tuple[dict, ...]) -> list[dict]:
        actual = self.take(parent, len(wanted))
        if not all(text_matches(a, b) for a, b in zip(actual, wanted, strict=True)):
            self.fail()
        return actual

    def media(self, parent: str, kind: str, digest: str) -> None:
        node = self.take(parent)[0]
        block_type = 27 if kind == "image" else 23
        if kind == "file" and node.get("block_type") == 33:
            children = node.get("children", [])
            if len(children) != 1:
                self.fail()
            inner = self.block(children[0])
            if inner.get("parent_id") != node["block_id"]:
                self.fail()
            node = inner
        token = node.get(kind, {}).get("token")
        if node.get("block_type") != block_type or not token or node.get("children"):
            self.fail()
        if token not in self.downloads:
            self.downloads[token] = self.client.download_digest(token)
        if self.downloads[token] != digest:
            raise UserError("核对失败：已有文档的图片或原附件与本次确认的摘要不一致。")

    def verify(self, plan: PublicationPlan) -> None:
        if self.client.document_title(self.doc) != plan.context["title"]:
            raise UserError("核对失败：已有文档的标题与本次确认的标题不一致。")
        root = self.block(self.doc)
        if root.get("block_type") != 1:
            self.fail()
        list_parents: list[str] = []
        for index, item in enumerate(plan.elements):
            element = item.element
            parent = list_parents[element.list_depth - 1] if element.list_depth else self.doc
            list_parents = list_parents[: element.list_depth]
            if element.kind == "image":
                self.media(parent, "image", plan.uploads[f"element-{index}"])
            elif element.kind == "table":
                table = self.take(parent)[0]
                shape = table.get("table", {}).get("property", {})
                wanted = item.blocks[0]["table"]["property"]
                cells = table.get("table", {}).get("cells") or table.get("children", [])
                if (
                    table.get("block_type") != 31
                    or any(shape.get(key) != value for key, value in wanted.items())
                    or len(cells) != len(item.cells)
                    or len(set(cells)) != len(cells)
                    or table.get("children", cells) != cells
                ):
                    self.fail()
                for cell_id, wanted_nodes in zip(cells, item.cells, strict=True):
                    cell = self.block(cell_id)
                    children = cell.get("children", [])
                    if cell.get("block_type") != 32 or cell.get("parent_id") != table["block_id"]:
                        self.fail()
                    self.positions[cell_id] = 0
                    if len(children) == len(wanted_nodes) + 1 and empty_text_block(
                        self.block(children[0])
                    ):
                        if self.block(children[0]).get("parent_id") != cell_id:
                            self.fail()
                        self.positions[cell_id] = 1
                    self.text(cell_id, wanted_nodes)
            else:
                created = self.text(parent, item.blocks)
                for block in created:
                    if block.get("block_type") in {12, 13}:
                        self.positions.setdefault(block["block_id"], 0)
                    elif block.get("children"):
                        self.fail()
                if element.kind in {"bullet", "ordered"}:
                    list_parents.append(created[-1]["block_id"])
        original_key = "original-pdf" if plan.context["source_kind"] == "pdf" else "original-wechat"
        self.media(self.doc, "file", plan.uploads[original_key])
        for parent, count in self.positions.items():
            if count != len(self.block(parent).get("children", [])):
                self.fail()
