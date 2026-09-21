import copy
import re
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from ...models import ParsedDocument, Target, UncertainWrite, UserError
from .blocks import (
    block_text,
    empty_text_block,
    plain_text_block,
    text_matches,
    text_signature,
)
from .client import FeishuClient
from .plan import (
    JOURNAL_VERSION,
    PLAN_VERSION,
    EffectRecord,
    PublicationPlan,
    build_plan,
    canonical_digest,
    file_digest,
)
from .verification import PlanVerifier


class Publisher:
    """Durable write journal: checkpoints are persisted before each remote mutation."""

    def __init__(self, client: FeishuClient, journal: dict, save: Callable, progress: Callable):
        self.client = client
        self.journal = copy.deepcopy(journal)
        self.save = save
        self.progress = progress
        self.plan: PublicationPlan | None = None
        for entry in self.journal.values():
            try:
                EffectRecord.model_validate(entry)
            except ValidationError as exc:
                raise UserError("发布日志格式或版本不受支持，已保留现场并停止写入。") from exc
        if (
            self.journal.get("plan", {}).get("result", {}).get("version", PLAN_VERSION)
            != PLAN_VERSION
        ):
            raise UserError("发布计划版本不受支持，已保留现场并停止写入。")

    def prepare_plan(
        self,
        parsed: ParsedDocument,
        source: Path,
        assets: Path,
        title: str,
        target: Target,
        digest: str,
        filename: str,
        *,
        app_id: str = "",
    ) -> PublicationPlan:
        settings = getattr(self.client, "settings", None)
        if settings is not None and app_id and app_id != settings.feishu_app_id:
            raise UserError("当前应用与原发布快照的应用身份不一致，已停止发布。")
        if not app_id:
            app_id = settings.feishu_app_id if settings is not None else ""
        plan = build_plan(parsed, source, assets, title, target, digest, filename, app_id)
        prior = self.journal.get("plan", {}).get("result")
        if prior is not None and prior.get("fingerprint") != plan.fingerprint:
            raise UserError("发布计划与已锁定快照不一致，已停止续接；请保留任务并核对内容。")
        self.plan = plan
        if prior is None:
            self.journal["plan"] = {
                "schema_version": JOURNAL_VERSION,
                "state": "done",
                "result": {"fingerprint": plan.fingerprint, **plan.context},
            }
            self.save(self.journal)
        return plan

    def effect(
        self,
        key: str,
        operation: Callable[[], dict],
        reconcile: Callable[[], dict | None] | None = None,
        *,
        context: dict | None = None,
        validate: Callable[[dict], bool] | None = None,
    ) -> dict:
        entry = self.journal.get(key)
        if entry:
            if context and entry.get("input") and entry["input"] != context:
                raise UserError("写入步骤与已保存的输入依据不一致，已停止恢复。")
            if entry["state"] == "done":
                return entry["result"]
            if reconcile is not None:
                result = reconcile()
                if result is not None:
                    self.journal[key] = {**entry, "state": "done", "result": result}
                    self.save(self.journal)
                    return result
            raise UncertainWrite(
                f"步骤 {key} 的上次写入结果不确定。已停止，避免重复写入；请检查远端文档。"
            )
        entry = {
            "schema_version": JOURNAL_VERSION,
            "state": "pending",
            "input": context or {},
            "started_at": time.time(),
        }
        self.journal[key] = entry
        self.save(self.journal)
        try:
            result = operation()
            if validate is not None and not validate(result):
                raise UncertainWrite("飞书返回的写入标识或结构不完整，请核对状态后恢复。")
        except UncertainWrite:
            raise
        except UserError:
            del self.journal[key]
            self.save(self.journal)
            raise
        self.journal[key] = {**entry, "state": "done", "result": result}
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
        if "children" in baseline:
            # Version zero used complete prefixes. Preserve readback compatibility.
            count = len(baseline["children"])
            matches = child_ids[:count] == baseline["children"]
        else:
            count = baseline["count"]
            matches = canonical_digest(child_ids[:count]) == baseline["sha256"]
        if not matches or len(child_ids) != count + len(wanted):
            raise UncertainWrite("追加结果尚未确认或远端内容已改变，已停止恢复以避免重复写入。")
        actual = [self.client.block(doc, child) for child in child_ids[count:]]
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
            before = self.client.block(doc, parent).get("children", [])
            self.journal[key + ":before"] = {
                "schema_version": JOURNAL_VERSION,
                "state": "done",
                "result": {"count": len(before), "sha256": canonical_digest(before)},
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
            validate=lambda result: (
                len(result.get("children", [])) == len(blocks)
                and all(child.get("block_id") for child in result.get("children", []))
            ),
        )
        children = result.get("children", [])
        if len(children) != len(blocks):
            raise UncertainWrite("飞书返回的创建块数量不符，请核对文档。")
        for child, wanted in zip(children, blocks, strict=True):
            if text_signature(wanted) is not None and not text_matches(child, wanted):
                raise UserError("已保存的写入记录与当前内容不一致，已在后续写入前停止恢复。")
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
        source_digest = file_digest(source, "待上传素材")
        if self.plan and source_digest != self.plan.uploads[key]:
            raise UserError("素材在发布计划生成后发生变化，已停止上传。")
        context = {
            "plan": self.plan.fingerprint if self.plan else "",
            "document_id": doc,
            "parent_id": parent,
            "root_id": created["block_id"],
            "block_id": block_id,
            "kind": kind,
            "sha256": source_digest,
        }
        uploaded = self.effect(
            key + ":upload",
            lambda: self.client.upload(block_id, source, kind, filename),
            context=context,
            validate=lambda result: bool(result.get("file_token")),
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

    def recover_candidate(self, key: str, candidate: str, *, app_id: str) -> None:
        """Associate one explicitly supplied object after readback, never publish it.

        The service must first prepare the frozen plan and serialize this operation
        with publishing. The subsequent normal publish still verifies every block,
        attachment and destination; this method does not mark the job successful.
        """
        plan = self.plan
        if plan is None or not app_id or plan.context["app_id"] != app_id:
            raise UserError("请先核对并加载原任务的发布快照及应用身份。")
        entry = self.journal.get(key, {})
        if entry.get("state") != "pending":
            raise UserError("只能关联写入结果不确定的步骤。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", candidate):
            raise UserError("请输入候选文档 ID 或素材 token，不要填写完整链接。")
        if key == "document":
            context = {"plan": plan.fingerprint, "title": plan.context["title"], "app_id": app_id}
            self._check_recovery_context(entry, context)
            metadata = self.client.document_metadata(candidate)
            if (
                metadata.get("doc_token") != candidate
                or metadata.get("doc_type") != "docx"
                or metadata.get("title") != plan.context["title"]
                or metadata.get("owner_id") != self.client.bot_open_id()
            ):
                raise UserError("候选文档的标识、标题或应用所有权不一致，未关联。")
            if entry.get("started_at"):
                try:
                    created_at = float(metadata.get("create_time", 0))
                except (TypeError, ValueError) as exc:
                    raise UserError("候选文档缺少可核验的创建时间，未关联。") from exc
                if created_at < entry["started_at"] - 5 or created_at > time.time() + 5:
                    raise UserError("候选文档的创建时间与本次写入不符，未关联。")
            blocks = self.client.blocks(candidate)
            if (
                len(blocks) != 1
                or blocks[0].get("block_id") != candidate
                or blocks[0].get("block_type") != 1
                or blocks[0].get("children")
            ):
                raise UserError("候选文档不是空白文档，未关联，也不会覆盖现有内容。")
            result: dict = {"document": {"document_id": candidate}}
        elif key.endswith(":upload"):
            context = self._upload_recovery_context(key, plan)
            self._check_recovery_context(entry, context)
            self._check_media_location(context, candidate)
            if self.client.download_digest(candidate) != context["sha256"]:
                raise UserError("候选素材与本次待上传文件的摘要不一致，未关联。")
            result = {"file_token": candidate}
        else:
            raise UserError("此步骤不支持人工关联；请使用核对状态续接完成回读。")
        self.journal[key] = {
            **entry,
            "schema_version": JOURNAL_VERSION,
            "state": "done",
            "input": context,
            "result": result,
            "recovery": {"method": "manual_readback", "candidate": candidate, "at": time.time()},
        }
        self.save(self.journal)

    @staticmethod
    def _check_recovery_context(entry: dict, context: dict) -> None:
        if entry.get("input") and entry["input"] != context:
            raise UserError("候选对象与原写入步骤的上下文不一致，未关联。")

    def _upload_recovery_context(self, key: str, plan: PublicationPlan) -> dict:
        base_key = key.removesuffix(":upload")
        if base_key not in plan.uploads:
            raise UserError("上传步骤不属于本次计划，未关联。")
        document = self.journal.get("document", {})
        doc = document.get("result", {}).get("document", {}).get("document_id")
        created = self.journal.get(base_key + ":create", {}).get("result", {}).get("children", [])
        if document.get("state") != "done" or not doc or len(created) != 1:
            raise UserError("原上传步骤缺少可核验的文档或占位块，未关联。")
        root = created[0]
        kind = "file" if base_key.startswith("original-") else "image"
        block_type = 23 if kind == "file" else 27
        node = root
        for _ in range(4):
            if node.get("block_type") == block_type:
                break
            children = node.get("children", [])
            if len(children) != 1:
                raise UserError("原上传占位块结构异常，未关联。")
            child = children[0]
            node = child if isinstance(child, dict) else self.client.block(doc, child)
        if node.get("block_type") != block_type or not root.get("parent_id"):
            raise UserError("原上传占位块归属不完整，未关联。")
        return {
            "plan": plan.fingerprint,
            "document_id": doc,
            "parent_id": root["parent_id"],
            "root_id": root["block_id"],
            "block_id": node["block_id"],
            "kind": kind,
            "sha256": plan.uploads[base_key],
        }

    def _check_media_location(self, context: dict, candidate: str) -> None:
        doc, root_id, block_id = (context["document_id"], context["root_id"], context["block_id"])
        root = self.client.block(doc, root_id)
        block = self.client.block(doc, block_id)
        if root.get("parent_id") != context["parent_id"]:
            raise UserError("素材占位块的位置已改变，未关联。")
        if root_id != block_id and (
            root.get("children") != [block_id] or block.get("parent_id") != root_id
        ):
            raise UserError("素材容器与原上传块的归属不一致，未关联。")
        if block.get("block_type") != (27 if context["kind"] == "image" else 23) or block.get(
            context["kind"], {}
        ).get("token") not in {None, "", candidate}:
            raise UserError("素材占位块类型或已有绑定不一致，未关联。")
        child = root_id
        parent = context["parent_id"]
        for _ in range(12):
            container = self.client.block(doc, parent)
            if child not in container.get("children", []):
                break
            if parent == doc:
                return
            child, parent = parent, container.get("parent_id", "")
            if not parent:
                break
        raise UserError("无法证明素材占位块属于原任务文档，未关联。")

    def publish(
        self,
        parsed: ParsedDocument,
        source: Path,
        assets: Path,
        title: str,
        target: Target,
        digest: str,
        filename: str,
        *,
        app_id: str = "",
    ) -> dict:
        plan = self.prepare_plan(
            parsed, source, assets, title, target, digest, filename, app_id=app_id
        )
        document = self.effect(
            "document",
            lambda: self.client.request("POST", "/docx/v1/documents", json={"title": title}),
            context={"plan": plan.fingerprint, "title": title, "app_id": plan.context["app_id"]},
            validate=lambda result: bool(result.get("document", {}).get("document_id")),
        )
        doc = document.get("document", {}).get("document_id")
        if not doc:
            raise UncertainWrite("飞书未返回文档标识，请检查是否已创建文档。")
        self.progress("publishing", "正在写入文档", document_id=doc)
        expected: list[dict] = []
        roots: list[str] = []
        list_parents: list[str] = []
        asset_digests = {asset.name: asset.digest for asset in parsed.assets}

        for index, planned in enumerate(plan.elements):
            element = planned.element
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
                table = self.children(key, doc, parent, list(planned.blocks))[0]
                if parent == doc:
                    roots.append(table["block_id"])
                table = self.client.block(doc, table["block_id"])
                cells = table.get("table", {}).get("cells") or table.get("children", [])
                if len(cells) != len(rows) * len(rows[0]):
                    raise UncertainWrite("表格单元格数量不符，已停止发布。")
                for cell_index, planned_nodes in enumerate(planned.cells):
                    nodes = list(planned_nodes)
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
                nodes = list(planned.blocks)
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
        self.verify_plan(doc, plan)
        self.journal["verified"] = {
            "schema_version": JOURNAL_VERSION,
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
        self.verify_plan(doc, plan)
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

    def verify_plan(self, doc: str, plan: PublicationPlan) -> None:
        """Prove current content matches a candidate, including legacy documents."""
        PlanVerifier(self.client, doc).verify(plan)

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
