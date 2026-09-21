import hashlib
import logging
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import ValidationError

from .config import Settings
from .content import PublishSnapshot, article_digest, digest_json
from .converters.pdf import DoclingParser, inspect_pdf
from .converters.wechat import WechatConverter, normalize_url
from .integrations.feishu import FeishuClient, Publisher
from .integrations.feishu.plan import build_plan
from .job_models import RevisionConflict
from .models import Asset, CodeLanguage, ParsedDocument, Target, UncertainWrite, UserError
from .notifications import EVENT_LABELS, Notification, prepare_notification
from .store import ACTIVE, RUNNING, Store


class JobService:
    def __init__(self, settings: Settings, store: Store, client: FeishuClient, parser=None):
        self.settings, self.store, self.client = settings, store, client
        self.parser = parser or DoclingParser(
            settings.docling_artifacts_path, settings.max_bytes, settings.max_pages
        )
        self.wechat = WechatConverter(
            max_html_bytes=settings.wechat_max_html_bytes,
            max_asset_bytes=settings.wechat_max_asset_bytes,
            max_total_bytes=settings.wechat_max_total_bytes,
            timeout=settings.wechat_timeout,
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="import-worker")
        self.lock = threading.Lock()
        self.closing = threading.Event()

    def folder(self, job_id: str, *, include_deleted: bool = False) -> Path:
        self.store.get(job_id, include_deleted=include_deleted)
        return self.settings.data_dir / "jobs" / job_id

    def delete(self, job_id: str) -> None:
        with self.lock:
            self.store.delete(job_id)

    def _check_capacity(self) -> None:
        if self.closing.is_set():
            raise UserError("服务正在关闭，请重新启动后再提交任务。")
        if self.store.count(statuses=list(ACTIVE)) >= 50:
            raise UserError("最多同时保留 50 个待处理任务，请等待队列完成后再提交。")

    def _schedule_locked(self) -> None:
        """Claim just one durable task; verification keeps ownership until continue/cancel."""
        if self.closing.is_set():
            return
        if self.store.count(statuses=list(RUNNING)):
            return
        job = self.store.next_queued()
        if job is None:
            return
        if job["status"] == "publish_queued":
            self.store.update(job["id"], status="publishing", progress="检查发布状态")
            self.executor.submit(self.publish, job["id"])
        elif job.get("source_kind", "pdf") == "wechat":
            self.store.update(job["id"], status="fetching", progress="正在获取公众号文章")
            self.executor.submit(self.fetch_wechat, job["id"], False)
        else:
            self.store.update(job["id"], status="parsing", progress="开始本地转换")
            self.executor.submit(self.parse, job["id"])

    def _advance(self) -> None:
        with self.lock:
            self._schedule_locked()

    def finish(self, job_id: str, **changes) -> None:
        """Save the outcome and pending notification together before attempting delivery."""
        job = {**self.store.get(job_id), **changes}
        receipt = prepare_notification(self.settings, job)
        if receipt:
            changes["notifications"] = {
                **job.get("notifications", {}),
                receipt.event: receipt.model_dump(),
            }
        self.store.update(job_id, **changes)
        if receipt:
            try:
                self.executor.submit(self.send_notification, job_id, receipt.event)
            except RuntimeError:
                # Shutdown may have started while a conversion was finishing. The persisted
                # pending receipt remains retryable after restart, without altering the result.
                logging.getLogger(__name__).warning("job=%s notification remains pending", job_id)

    def send_notification(self, job_id: str, event: str) -> None:
        receipt = self.store.update_notification(
            job_id, event, {"pending"}, status="sending", error=""
        )
        if not receipt:
            return
        notification = Notification.model_validate(receipt)
        try:
            if not self.settings.feishu_notify_enabled:
                raise UserError("通知已关闭，未发送。")
            if not notification.receive_id or not self.settings.configured:
                raise UserError("请在 .env 配置通知接收人及飞书应用凭证，重启后重试通知。")
            if notification.app_id != self.settings.feishu_app_id:
                raise UserError("应用身份已变化，不能以另一应用重发原通知。")
            message_id = self.client.send_notification(
                notification.receive_id_type,
                notification.receive_id,
                notification.text,
                notification.uuid,
            )
        except UncertainWrite:
            self.store.update_notification(
                job_id,
                event,
                {"sending"},
                status="uncertain",
                error="通知可能已发送，但未收到确认。请在飞书核对，未自动重发。",
            )
        except UserError as exc:
            self.store.update_notification(
                job_id, event, {"sending"}, status="failed", error=str(exc)
            )
        except Exception:
            # An unexpected failure can occur after dispatch; do not guess that nothing was sent.
            self.store.update_notification(
                job_id,
                event,
                {"sending"},
                status="uncertain",
                error="通知发送异常，结果不确定。请在飞书核对，未自动重发。",
            )
        else:
            self.store.update_notification(
                job_id, event, {"sending"}, status="sent", message_id=message_id, error=""
            )

    def retry_notification(self, job_id: str, event: str) -> dict:
        with self.lock:
            job = self.store.get(job_id)
            if self.closing.is_set() or not self.settings.feishu_notify_enabled:
                raise UserError("通知未启用或服务正在关闭。")
            receipt = job.get("notifications", {}).get(event)
            if job["status"] != event:
                raise UserError("任务结果已变化，不再重发旧结果通知。")
            if event not in EVENT_LABELS or not receipt or receipt["status"] != "failed":
                raise UserError("只有明确发送失败的通知可重试；结果不确定时请先在飞书核对。")
            if not self.settings.configured or not self.settings.feishu_notify_receive_id.strip():
                raise UserError("请先配置飞书应用凭证与通知接收人并重启。")
            if receipt["app_id"] and receipt["app_id"] != self.settings.feishu_app_id:
                raise UserError("应用身份已变化，不能以另一应用重发原通知。")
            # Once addressed, a receipt can never be silently rerouted after a config change.
            if receipt["receive_id"] and (receipt["receive_id"], receipt["receive_id_type"]) != (
                self.settings.feishu_notify_receive_id.strip(),
                self.settings.feishu_notify_receive_id_type,
            ):
                raise UserError("接收人配置已变化，请恢复原配置后重试原通知。")
            claimed = self.store.update_notification(
                job_id,
                event,
                {"failed"},
                status="pending",
                error="",
                app_id=self.settings.feishu_app_id,
                receive_id=self.settings.feishu_notify_receive_id.strip(),
                receive_id_type=self.settings.feishu_notify_receive_id_type,
            )
            if claimed:
                self.executor.submit(self.send_notification, job_id, event)
            return self.store.get(job_id)

    def receive_wechat(self, url: str, batch_id: str = "") -> dict:
        url = normalize_url(url)
        with self.lock:
            self._check_capacity()
            job = self.store.create("公众号文章.zip", "", source_kind="wechat", batch_id=batch_id)
            self.folder(job["id"]).mkdir(parents=True)
            job = self.store.update(job["id"], source_url=url, progress="等待获取公众号文章")
            self._schedule_locked()
            return self.store.get(job["id"])

    def continue_wechat(self, job_id: str) -> dict:
        with self.lock:
            job = self.store.get(job_id)
            if job.get("source_kind") != "wechat" or job["status"] != "waiting_verification":
                raise UserError("此任务没有等待中的验证会话，请重新获取文章。")
            job = self.store.update(job_id, status="fetching", progress="正在读取验证后的正文")
            self.executor.submit(self.fetch_wechat, job_id, True)
            return job

    def cancel_wechat(self, job_id: str) -> dict:
        with self.lock:
            job = self.store.get(job_id)
            if job["status"] == "queued":
                job = self.store.update(job_id, status="cancelled", progress="已取消排队", error="")
                self._schedule_locked()
                return job
            if job.get("source_kind") != "wechat" or job["status"] not in {
                "fetching",
                "waiting_verification",
                "cancelling",
            }:
                raise UserError("当前没有可取消的文章获取任务。")
            if job["status"] == "cancelling":
                return job
            job = self.store.update(job_id, status="cancelling", progress="正在关闭临时验证会话")
            # Playwright is owned by the single worker; never close it from an HTTP thread.
            self.executor.submit(self.finish_cancel, job_id)
            return job

    def finish_cancel(self, job_id: str) -> None:
        try:
            self.wechat.cancel()
        finally:
            self.store.update(job_id, status="cancelled", progress="已取消获取", error="")
            self._advance()

    def fetch_wechat(self, job_id: str, resume: bool) -> None:
        def progress(message: str) -> None:
            self.store.update(job_id, load_journal=False, progress=message)

        try:
            folder = self.folder(job_id)
            if resume:
                parsed = self.wechat.resume(folder, progress)
            else:
                parsed = self.wechat.start(self.store.get(job_id)["source_url"], folder, progress)
            with self.lock:
                if self.store.get(job_id)["status"] == "cancelling":
                    return
                if parsed is None:
                    self.store.update(
                        job_id,
                        status="waiting_verification",
                        progress="请在独立浏览器窗口完成验证后点击继续获取",
                    )
                    if not self.closing.is_set():
                        self.executor.submit(self.pump_wechat, job_id)
                    return
                digest = article_digest(parsed)
                original = folder / "source.zip"
                if original.stat().st_size > 20 * 1024 * 1024:
                    raise UserError(
                        "离线网页 ZIP 超过飞书单文件上传的 20 MB 限制，无法完整保存；未发布正文。"
                    )
                original_digest = hashlib.sha256(original.read_bytes()).hexdigest()
                title = parsed.metadata.title or "公众号文章"
                filename = re.sub(r"[/\\\x00-\x1f]", "_", title)[:170] + ".zip"
                self.store.commit_content(
                    job_id,
                    parsed.model_dump_json(),
                    digest,
                    expected_revision=0,
                    status="ready",
                    progress="请核对正文、素材和转换提示后保存",
                    parsed=True,
                    digest=digest,
                    original_digest=original_digest,
                    source_url=parsed.metadata.url,
                    filename=filename,
                )
        except Exception as exc:
            self.wechat.cancel()
            if self.store.get(job_id)["status"] != "cancelling":
                self.fail(job_id, exc)
        finally:
            self._advance()

    def pump_wechat(self, job_id: str) -> None:
        """Dispatch browser validation events without monopolizing the task queue."""
        if self.closing.is_set() or self.store.get(job_id)["status"] != "waiting_verification":
            return
        try:
            self.wechat.pump()
        except Exception as exc:
            self.wechat.cancel()
            # An HTTP cancellation/continuation can be queued during the bounded pump.
            if self.store.get(job_id)["status"] == "waiting_verification":
                self.fail(job_id, exc)
                self._advance()
            return
        with self.lock:
            if (
                not self.closing.is_set()
                and self.store.get(job_id)["status"] == "waiting_verification"
            ):
                self.executor.submit(self.pump_wechat, job_id)

    def asset_file(self, job_id: str, name: str) -> tuple[Path, str]:
        parsed = self.parsed(job_id)
        entry = next((a for a in parsed.assets if a.name == name), None)
        if entry is None and parsed.source_kind == "pdf":
            # Historical previews predate manifests. Derive the allowlist from their model.
            names = set(parsed.page_images)
            names.update(e.asset for e in parsed.elements if e.asset)
            names.update(s.asset for e in parsed.elements for s in e.code_sources if s.asset)
            if name in names and re.fullmatch(r"(?:page|figure|code)-\d+\.png", name):
                entry = Asset(name=name, media_type="image/png", digest="")
        if entry is None or Path(name).name != name or name in {".", ".."}:
            raise FileNotFoundError(name)
        assets = (self.folder(job_id) / "assets").resolve()
        path = (assets / name).resolve()
        if (
            path.parent != assets
            or not path.is_file()
            or entry.media_type
            not in {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
            }
        ):
            raise FileNotFoundError(name)
        return path, entry.media_type

    def original_file(self, job_id: str) -> tuple[Path, str]:
        job = self.store.get(job_id)
        article = job.get("source_kind", "pdf") == "wechat"
        path = self.folder(job_id) / ("source.zip" if article else "source.pdf")
        if (article and not job.get("parsed")) or not path.is_file():
            raise FileNotFoundError(path.name)
        return path, "application/zip" if article else "application/pdf"

    def receive(self, filename: str, content: bytes, batch_id: str = "") -> dict:
        filename = Path(filename.replace("\\", "/")).name[:180]
        if not filename.lower().endswith(".pdf"):
            raise UserError("请选择一个 PDF 文件。")
        if len(content) > self.settings.max_bytes:
            raise UserError("文件超过 20 MB 限制。")
        with self.lock:
            self._check_capacity()
            job = self.store.create(
                filename, hashlib.sha256(content).hexdigest(), batch_id=batch_id
            )
            folder = self.folder(job["id"])
            folder.mkdir(parents=True)
            source = folder / "source.pdf"
            source.write_bytes(content)
            try:
                inspect_pdf(source, self.settings.max_bytes, self.settings.max_pages)
            except UserError as exc:
                self.fail(job["id"], exc)
                raise
            self._schedule_locked()
        return self.store.get(job["id"])

    def parse(self, job_id: str):
        def progress(message: str) -> None:
            self.store.update(job_id, load_journal=False, progress=message)

        try:
            self.store.update(job_id, status="parsing", progress="开始本地转换")
            folder = self.folder(job_id)
            parsed = self.parser(
                folder / "source.pdf",
                folder / "assets",
                progress,
            )
            self.store.commit_content(
                job_id,
                parsed.model_dump_json(),
                article_digest(parsed),
                expected_revision=0,
                status="ready",
                progress="请核对预览后保存",
                parsed=True,
            )
        except Exception as exc:
            self.fail(job_id, exc)
        finally:
            self._advance()

    def parsed(self, job_id: str, *, include_deleted: bool = False) -> ParsedDocument:
        content = self.store.get_content(job_id, include_deleted=include_deleted)
        if content is not None:
            try:
                parsed = ParsedDocument.model_validate_json(content["parsed_json"])
            except ValidationError as exc:
                raise UserError("内容格式或版本不受支持，已保留原记录，请升级程序后读取。") from exc
            if article_digest(parsed) != content["content_digest"]:
                raise UserError("内容存储校验失败，已停止；请保留任务文件并检查恢复现场。")
            return parsed
        # Legacy JSON is imported once. From then on SQLite is the sole authority;
        # a leftover parsed.json can never override a committed edit or its identity.
        path = self.folder(job_id, include_deleted=include_deleted) / "parsed.json"
        if not path.is_file():
            raise UserError("尚无转换预览，请完成来源获取；中断任务请重新获取或上传。")
        try:
            parsed = ParsedDocument.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            raise UserError("旧内容格式或版本不受支持，已保留原文件，请检查后重试。") from exc
        if include_deleted:
            return parsed
        try:
            self.store.commit_content(
                job_id,
                parsed.model_dump_json(),
                article_digest(parsed),
                expected_revision=0,
            )
        except RevisionConflict:
            return self.parsed(job_id)
        return parsed

    @staticmethod
    def review_token(parsed: ParsedDocument) -> str:
        return hashlib.sha256(parsed.model_dump_json().encode("utf-8")).hexdigest()

    def _snapshot(self, job_id: str, url: str, title: str) -> PublishSnapshot:
        parsed = self.parsed(job_id)
        job = self.store.get(job_id)
        target = self.client.resolve(url)
        effective_title = title.strip() or Path(job["filename"]).stem
        plan = build_plan(
            parsed,
            self.original_file(job_id)[0],
            self.folder(job_id) / "assets",
            effective_title,
            target,
            job.get("original_digest", job["digest"]),
            job["filename"],
            self.settings.feishu_app_id,
        )
        return PublishSnapshot(
            uploads=plan.uploads,
            content_revision=job["content_revision"],
            content_digest=article_digest(parsed),
            review_token=self.review_token(parsed),
            source_kind=parsed.source_kind,
            source_url=job.get("source_url", ""),
            source_digest=(
                article_digest(parsed) if parsed.source_kind == "wechat" else job["digest"]
            ),
            original_digest=job.get("original_digest", job["digest"]),
            filename=job["filename"],
            app_id=self.settings.feishu_app_id,
            title=effective_title,
            target=target,
        )

    def confirm_review(
        self,
        job_id: str,
        url: str,
        title: str,
        review_token: str,
        expected_revision: int,
    ) -> dict:
        with self.lock:
            parsed = self.parsed(job_id)
            job = self.store.get(job_id)
            if not review_token or review_token != self.review_token(parsed):
                raise RevisionConflict("预览内容已变化，请重新打开任务并核对后发布。")
            if expected_revision != job["content_revision"]:
                raise RevisionConflict("内容版本已变化，请重新核对预览。")
            snapshot = self._snapshot(job_id, url, title)
            if job.get("publish_snapshot"):
                previous = PublishSnapshot.from_record(job["publish_snapshot"])
                if snapshot != previous:
                    raise RevisionConflict("已开始发布的任务不能更换内容、标题、目标或应用。")
            if job.get("journal") and not job.get("publish_snapshot"):
                old = Target.model_validate(job["target"])
                if (old.space_id, old.node_token, job["app_id"], job["requested_title"]) != (
                    snapshot.target.space_id,
                    snapshot.target.node_token,
                    snapshot.app_id,
                    snapshot.title,
                ):
                    raise UserError("旧发布任务的应用、标题和目标必须与原记录一致。")
                # A human confirmation upgrades the legacy frozen-input contract;
                # it does not replay any pending remote operation.
                self.store.update(
                    job_id,
                    publish_snapshot=snapshot.model_dump(),
                    intent_digest=snapshot.intent_digest,
                )
            token = digest_json({"job_id": job_id, "snapshot": snapshot.model_dump()})
            self.store.update(
                job_id,
                reviewed_snapshot=snapshot.model_dump(),
                confirmation_token=token,
            )
            return {"confirmation_token": token, "content_revision": expected_revision}

    def submit_publish(
        self,
        job_id: str,
        url: str,
        title: str,
        confirmed: bool = False,
        review_token: str = "",
        confirmation_token: str = "",
    ) -> dict:
        with self.lock:
            parsed = self.parsed(job_id)
            job = self.store.get(job_id)
            if not confirmed or not review_token:
                raise UserError("请先核对正文、图片、代码和全部转换提示，并勾选确认后发布。")
            if review_token != self.review_token(parsed):
                raise RevisionConflict("预览内容已变化，请重新打开任务并核对后发布。")
            snapshot = self._snapshot(job_id, url, title)
            token = digest_json({"job_id": job_id, "snapshot": snapshot.model_dump()})
            if (
                not confirmation_token
                or confirmation_token != token
                or token != job.get("confirmation_token")
            ):
                raise RevisionConflict("发布确认已失效；请重新确认当前内容、标题和保存位置。")
            if (
                job.get("publish_snapshot")
                and PublishSnapshot.from_record(job["publish_snapshot"]) != snapshot
            ):
                raise RevisionConflict("已开始发布的任务不能更换内容、标题、目标或应用。")
            if any(
                e.kind == "code" and (not e.code_reviewed or not e.text.strip())
                for e in parsed.elements
            ):
                raise UserError("请先在预览中保存所有代码片段的校对结果。")
            if job["status"] in {"publish_queued", "publishing", "verifying", "archiving"}:
                return job
            self._check_capacity()
            for candidate in self.store.find_candidates(intent_digest=snapshot.intent_digest):
                previous = self.store.get(candidate["id"], include_deleted=True)
                if (
                    previous["id"] == job_id
                    or previous.get("intent_digest") != snapshot.intent_digest
                ):
                    continue
                if previous["status"] != "succeeded" and (
                    previous.get("journal") or previous.get("publish_snapshot")
                ):
                    raise UserError(
                        f"相同发布意图的任务 {previous['id']} 尚未完成。请打开该任务核对并续接，"
                        "避免重复创建文档。"
                    )
            for candidate in self._legacy_candidates(snapshot):
                if candidate["id"] != job_id and candidate["status"] != "succeeded":
                    raise UserError(
                        f"同来源的旧发布任务 {candidate['id']} 尚未核验完成。"
                        "请打开原任务重新确认并恢复，避免重复创建。"
                    )
            self.store.claim(
                job_id,
                target=snapshot.target.model_dump(),
                app_id=snapshot.app_id,
                title=snapshot.title,
                requested_title=snapshot.title,
                publish_snapshot=snapshot.model_dump(),
                intent_digest=snapshot.intent_digest,
            )
            self._schedule_locked()
        return self.store.get(job_id)

    def _legacy_candidates(self, snapshot: PublishSnapshot) -> list[dict]:
        if snapshot.source_kind == "wechat":
            candidates = self.store.find_candidates(
                source_kind="wechat",
                source_url=snapshot.source_url,
                app_id=snapshot.app_id,
                space_id=snapshot.target.space_id,
                parent_token=snapshot.target.node_token,
            )
        else:
            candidates = self.store.find_candidates(
                source_kind="pdf",
                digest=snapshot.source_digest,
                app_id=snapshot.app_id,
                space_id=snapshot.target.space_id,
                parent_token=snapshot.target.node_token,
            )
        return [j for j in candidates if not j.get("intent_digest") and j.get("has_journal")]

    def save_code(
        self,
        job_id: str,
        index: int,
        text: str,
        language: CodeLanguage,
        expected_revision: int,
    ):
        with self.lock:
            job = self.store.get(job_id)
            if job["status"] != "ready" or job["journal"] or job.get("document_id"):
                raise UserError("已开始发布或正在处理的任务不能修改代码；请重新获取来源。")
            parsed = self.parsed(job_id)
            job = self.store.get(job_id)
            if expected_revision != job["content_revision"]:
                raise RevisionConflict("代码已被其他窗口更新；草稿已保留，请重新加载后比较。")
            if not 0 <= index < len(parsed.elements) or parsed.elements[index].kind not in {
                "code",
                "image",
            }:
                raise UserError("只能将代码或图片区域保存为代码片段。")
            if not text.strip() or len(text) > 20000:
                raise UserError("代码内容不能为空，且每段最多 20000 字符。")
            element = parsed.elements[index]
            element.kind = "code"
            element.text = text
            element.language = language
            element.code_origin = "manual"
            element.code_reviewed = True
            changes = {"digest": article_digest(parsed)} if parsed.source_kind == "wechat" else {}
            return self.store.commit_content(
                job_id,
                parsed.model_dump_json(),
                article_digest(parsed),
                expected_revision=expected_revision,
                progress="代码校对已保存，请确认后发布",
                confirmation_token="",
                reviewed_snapshot=None,
                **changes,
            )

    def _publisher(self, job_id: str, job: dict) -> Publisher:
        return Publisher(
            self.client,
            job["journal"],
            lambda journal: self.store.update(job_id, load_journal=False, journal=journal),
            lambda status, progress, **extra: self.store.update(
                job_id, load_journal=False, status=status, progress=progress, **extra
            ),
        )

    def recover_candidate(
        self,
        job_id: str,
        key: str,
        candidate: str,
        expected_revision: int,
    ) -> dict:
        with self.lock:
            job = self.store.get(job_id)
            if job["status"] != "needs_review" or not job.get("publish_snapshot"):
                raise UserError("只有已冻结发布内容的待核对任务可以关联候选结果。")
            snapshot = PublishSnapshot.from_record(job["publish_snapshot"])
            if expected_revision != job["content_revision"]:
                raise RevisionConflict("任务版本已变化，请重新打开任务。")
            if snapshot.app_id != self.settings.feishu_app_id:
                raise UserError("应用身份已变化，不能关联另一应用的发布结果。")
            parsed = self.parsed(job_id)
            snapshot.assert_content(parsed, expected_revision)
            publisher = self._publisher(job_id, job)
            publisher.prepare_plan(
                parsed,
                self.original_file(job_id)[0],
                self.folder(job_id) / "assets",
                snapshot.title,
                snapshot.target,
                snapshot.original_digest,
                snapshot.filename,
                app_id=snapshot.app_id,
            )
            publisher.recover_candidate(key, candidate, app_id=snapshot.app_id)
            return self.store.update(job_id, error="", progress="候选结果已核验，请确认后继续发布")

    def publish(self, job_id: str):
        try:
            job = self.store.get(job_id)
            parsed = self.parsed(job_id)
            snapshot = PublishSnapshot.from_record(job["publish_snapshot"])
            snapshot.assert_content(parsed, job["content_revision"])
            target = snapshot.target
            current_plan = build_plan(
                parsed,
                self.original_file(job_id)[0],
                self.folder(job_id) / "assets",
                snapshot.title,
                target,
                snapshot.original_digest,
                snapshot.filename,
                snapshot.app_id,
            )
            if current_plan.uploads != snapshot.uploads:
                raise UserError("发布素材与确认时的摘要不一致，已停止，请保留现场并核对。")
            publisher = self._publisher(job_id, job)
            candidates = self.store.find_candidates(intent_digest=snapshot.intent_digest)
            candidates += self._legacy_candidates(snapshot)
            for candidate in candidates:
                if candidate["id"] != job_id and job["journal"]:
                    continue
                previous = self.store.get(candidate["id"], include_deleted=True)
                if not previous.get("wiki_token"):
                    continue
                if previous.get("publication_source_id"):
                    previous = self.store.get(
                        previous["publication_source_id"], include_deleted=True
                    )
                old_parsed = self.parsed(previous["id"], include_deleted=True)
                if previous.get("publish_snapshot"):
                    frozen = PublishSnapshot.from_record(previous["publish_snapshot"])
                    frozen.assert_content(old_parsed, previous["content_revision"])
                # Legacy successful imports lack an intent hash. Compare source semantics
                # first, then independently verify the remote against CURRENT planned blocks.
                if (
                    article_digest(old_parsed) != article_digest(parsed)
                    or previous.get("requested_title") != snapshot.title
                ):
                    continue
                original_digest = previous.get(
                    "published_original_digest", previous.get("original_digest", previous["digest"])
                )
                candidate_plan = build_plan(
                    parsed,
                    self.folder(previous["id"], include_deleted=True)
                    / ("source.zip" if snapshot.source_kind == "wechat" else "source.pdf"),
                    self.folder(job_id) / "assets",
                    snapshot.title,
                    target,
                    original_digest,
                    previous["filename"],
                    snapshot.app_id,
                )
                node = self.client.node(previous["wiki_token"])
                if (
                    node.get("parent_node_token") != target.node_token
                    or node.get("space_id") != target.space_id
                    or node.get("obj_token") != previous["document_id"]
                ):
                    raise UserError("已有导入记录的位置已变化，请核对远端文档。")
                publisher.verify_plan(previous["document_id"], candidate_plan)
                self.finish(
                    job_id,
                    status="succeeded",
                    progress="已核实并复用已有文档",
                    error="",
                    document_id=previous["document_id"],
                    wiki_token=previous["wiki_token"],
                    url=previous["url"],
                    journal=previous["journal"],
                    published_original_digest=original_digest,
                    publication_source_id=previous["id"],
                )
                return
            folder = self.folder(job_id)
            result = publisher.publish(
                parsed,
                self.original_file(job_id)[0],
                folder / "assets",
                snapshot.title,
                target,
                snapshot.original_digest,
                snapshot.filename,
                app_id=snapshot.app_id,
            )
            self.finish(
                job_id,
                status="succeeded",
                progress="已保存并核对知识库位置",
                published_original_digest=snapshot.original_digest,
                **result,
            )
        except Exception as exc:
            self.fail(job_id, exc)
        finally:
            self._advance()

    def fail(self, job_id: str, exc: Exception):
        message = (
            str(exc)
            if isinstance(exc, UserError)
            else (f"处理失败（{type(exc).__name__}），请检查本地依赖、模型和任务文件后重试。")
        )
        diagnostic = "".join(traceback.format_exception(exc))
        for secret in (
            self.settings.feishu_app_secret.get_secret_value(),
            getattr(self.client, "token", ""),
        ):
            if secret:
                diagnostic = diagnostic.replace(secret, "[REDACTED]")
        diagnostic = re.sub(r"(?i)Bearer\s+[^\s'\"]+", "Bearer [REDACTED]", diagnostic)
        logging.getLogger(__name__).warning("job=%s\n%s", job_id, diagnostic[-8000:])
        self.finish(
            job_id,
            status="needs_review" if isinstance(exc, UncertainWrite) else "failed",
            error=message,
            progress="处理已停止",
        )

    def close(self):
        with self.lock:
            self.closing.set()
            closing = self.executor.submit(self.wechat.close)
        self.executor.shutdown(wait=True)
        try:
            closing.result()
        finally:
            self.client.close()
