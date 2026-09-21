import hashlib
import json
import logging
import re
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .config import Settings
from .converters.pdf import DoclingParser, inspect_pdf
from .converters.wechat import WechatConverter, normalize_url
from .integrations.feishu import FeishuClient, Publisher
from .models import Asset, CodeLanguage, ParsedDocument, Target, UncertainWrite, UserError
from .store import ACTIVE, RUNNING, Store


def article_digest(parsed: ParsedDocument) -> str:
    """Hash source/content, not temporary asset URLs or extraction-only locations."""
    identity = parsed.model_dump(
        mode="json",
        exclude={
            "assets": {"__all__": {"source_url"}},
            "elements": {"__all__": {"web_locator", "code_origin", "code_reviewed"}},
        },
    )
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


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

    def folder(self, job_id: str) -> Path:
        self.store.get(job_id)
        return self.settings.data_dir / "jobs" / job_id

    def _check_capacity(self) -> None:
        if self.closing.is_set():
            raise UserError("服务正在关闭，请重新启动后再提交任务。")
        if sum(j["status"] in ACTIVE for j in self.store.list()) >= 50:
            raise UserError("最多同时保留 50 个待处理任务，请等待队列完成后再提交。")

    def _schedule_locked(self) -> None:
        """Claim just one durable task; verification keeps ownership until continue/cancel."""
        if self.closing.is_set():
            return
        jobs = self.store.list()
        if any(j["status"] in RUNNING for j in jobs):
            return
        pending = sorted(
            (j for j in jobs if j["status"] in {"queued", "publish_queued"}),
            key=lambda j: j.get("queued_at", j["created_at"]),
        )
        if not pending:
            return
        job = pending[0]
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
            self.store.update(job_id, progress=message)

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
                (folder / "parsed.json").write_text(parsed.model_dump_json(), encoding="utf-8")
                self.store.update(
                    job_id,
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
            self.store.update(job_id, progress=message)

        try:
            self.store.update(job_id, status="parsing", progress="开始本地转换")
            folder = self.folder(job_id)
            parsed = self.parser(
                folder / "source.pdf",
                folder / "assets",
                progress,
            )
            (folder / "parsed.json").write_text(parsed.model_dump_json(), encoding="utf-8")
            self.store.update(job_id, status="ready", progress="请核对预览后保存", parsed=True)
        except Exception as exc:
            self.fail(job_id, exc)
        finally:
            self._advance()

    def parsed(self, job_id: str) -> ParsedDocument:
        path = self.folder(job_id) / "parsed.json"
        if not path.is_file():
            raise UserError("尚无转换预览，请完成来源获取；中断任务请重新获取或上传。")
        return ParsedDocument.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def review_token(parsed: ParsedDocument) -> str:
        return hashlib.sha256(parsed.model_dump_json().encode("utf-8")).hexdigest()

    def submit_publish(
        self, job_id: str, url: str, title: str, confirmed: bool = False, review_token: str = ""
    ) -> dict:
        with self.lock:
            job = self.store.get(job_id)
            if job.get("source_kind") == "wechat" and not job.get("parsed"):
                raise UserError("文章尚未完整获取，不能发布；请重新获取并核对预览。")
            parsed = self.parsed(job_id)
            if review_token and review_token != self.review_token(parsed):
                raise UserError("预览内容已变化，请重新打开任务并核对后发布。")
            if parsed.source_kind == "wechat" and not confirmed:
                raise UserError("请先核对文章正文、图片和全部转换提示，并勾选确认后发布。")
            if any(
                e.kind == "code" and (not e.code_reviewed or not e.text.strip())
                for e in parsed.elements
            ):
                raise UserError("请先在预览中保存所有代码片段的校对结果。")
            if job["status"] in {"publish_queued", "publishing", "verifying", "archiving"}:
                return job
            self._check_capacity()
            target = self.client.resolve(url)
            title = title.strip() or Path(job["filename"]).stem
            if job["journal"]:
                old = Target.model_validate(job["target"])
                if (old.space_id, old.node_token, job["app_id"], job["requested_title"]) != (
                    target.space_id,
                    target.node_token,
                    self.settings.feishu_app_id,
                    title,
                ):
                    raise UserError("已开始发布的任务不能更换目标、标题或应用，请重新上传。")
                title = job["title"]
            else:
                original_title = title
                for previous in self.store.list():
                    if (
                        previous.get("requested_title") == title
                        and previous["digest"] != job["digest"]
                        and previous.get("target", {}).get("node_token") == target.node_token
                    ):
                        title += datetime.now().strftime(" %Y%m%d-%H%M%S")
                        break
                job["requested_title"] = original_title
            self.store.claim(
                job_id,
                target=target.model_dump(),
                app_id=self.settings.feishu_app_id,
                title=title,
                requested_title=job["requested_title"],
            )
            self._schedule_locked()
        return self.store.get(job_id)

    def save_code(self, job_id: str, index: int, text: str, language: CodeLanguage):
        with self.lock:
            job = self.store.get(job_id)
            if job["status"] != "ready" or job["journal"] or job.get("document_id"):
                raise UserError("已开始发布或正在处理的任务不能修改代码；请重新获取来源。")
            parsed = self.parsed(job_id)
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
            path = self.folder(job_id) / "parsed.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(parsed.model_dump_json(), encoding="utf-8")
            temporary.replace(path)
            changes = {"digest": article_digest(parsed)} if parsed.source_kind == "wechat" else {}
            return self.store.update(job_id, progress="代码校对已保存，请确认后发布", **changes)

    def publish(self, job_id: str):
        try:
            job = self.store.get(job_id)
            parsed = self.parsed(job_id)
            target = Target.model_validate(job["target"])
            publisher = Publisher(
                self.client,
                job["journal"],
                lambda journal: self.store.update(job_id, journal=journal),
                lambda status, progress, **extra: self.store.update(
                    job_id, status=status, progress=progress, **extra
                ),
            )
            # A failed recheck must stop: creating a replacement could duplicate a document.
            for previous in self.store.list():
                if (
                    previous.get("wiki_token")
                    and previous.get("source_kind", "pdf") == job.get("source_kind", "pdf")
                    and previous.get("source_url", "") == job.get("source_url", "")
                    and previous["digest"] == job["digest"]
                    and previous.get("app_id") == job["app_id"]
                    and previous.get("target", {}).get("node_token") == target.node_token
                    and previous.get("target", {}).get("space_id") == target.space_id
                    # A new conversion or a corrected code snippet must not reuse an old
                    # image-only result. Existing started jobs still resume their journal.
                    and (
                        previous["id"] == job_id
                        or (
                            not job["journal"]
                            and (
                                parsed.source_kind == "wechat"
                                or self.parsed(previous["id"]).elements == parsed.elements
                            )
                        )
                    )
                ):
                    node = self.client.node(previous["wiki_token"])
                    if (
                        node.get("parent_node_token") != target.node_token
                        or node.get("space_id") != target.space_id
                        or node.get("obj_token") != previous["document_id"]
                    ):
                        raise UserError("已有导入记录的位置已变化，请核对远端文档。")
                    verified = previous["journal"]["verified"]["result"]
                    publisher.verify(previous["document_id"], **verified)
                    attachment = next(x for x in verified["expected"] if x["kind"] == "file")
                    original_digest = previous.get(
                        "published_original_digest",
                        previous.get("original_digest", previous["digest"]),
                    )
                    if self.client.download_digest(attachment["token"]) != original_digest:
                        raise UserError("已有文档的原附件核验失败，请核对远端文档。")
                    self.store.update(
                        job_id,
                        status="succeeded",
                        progress="已核实并复用已有文档",
                        error="",
                        document_id=previous["document_id"],
                        wiki_token=previous["wiki_token"],
                        url=previous["url"],
                        journal=previous["journal"],
                        published_original_digest=original_digest,
                    )
                    return
            folder = self.folder(job_id)
            result = publisher.publish(
                parsed,
                self.original_file(job_id)[0],
                folder / "assets",
                job["title"],
                target,
                job.get("original_digest", job["digest"]),
                job["filename"],
            )
            self.store.update(
                job_id,
                status="succeeded",
                progress="已保存并核对知识库位置",
                published_original_digest=job.get("original_digest", job["digest"]),
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
        self.store.update(
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
