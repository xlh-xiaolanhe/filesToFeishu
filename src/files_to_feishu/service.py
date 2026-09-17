import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .config import Settings
from .feishu import FeishuClient
from .models import ParsedDocument, Target, UncertainWrite, UserError
from .parser import DoclingParser, inspect_pdf
from .publisher import Publisher
from .store import ACTIVE, Store


class JobService:
    def __init__(self, settings: Settings, store: Store, client: FeishuClient, parser=None):
        self.settings, self.store, self.client = settings, store, client
        self.parser = parser or DoclingParser(
            settings.docling_artifacts_path, settings.max_bytes, settings.max_pages
        )
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-worker")
        self.lock = threading.Lock()

    def folder(self, job_id: str) -> Path:
        self.store.get(job_id)
        return self.settings.data_dir / "jobs" / job_id

    def receive(self, filename: str, content: bytes) -> dict:
        filename = Path(filename.replace("\\", "/")).name[:180]
        if not filename.lower().endswith(".pdf"):
            raise UserError("请选择一个 PDF 文件。")
        if len(content) > self.settings.max_bytes:
            raise UserError("文件超过 20 MB 限制。")
        with self.lock:
            if any(j["status"] in ACTIVE for j in self.store.list()):
                raise UserError("已有任务正在处理，请等待完成。")
            job = self.store.create(filename, hashlib.sha256(content).hexdigest())
            folder = self.folder(job["id"])
            folder.mkdir(parents=True)
            source = folder / "source.pdf"
            source.write_bytes(content)
            try:
                inspect_pdf(source, self.settings.max_bytes, self.settings.max_pages)
            except UserError as exc:
                self.fail(job["id"], exc)
                raise
            self.executor.submit(self.parse, job["id"])
        return job

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

    def parsed(self, job_id: str) -> ParsedDocument:
        path = self.folder(job_id) / "parsed.json"
        if not path.is_file():
            raise UserError("尚无转换预览，请完成模型配置后重新上传。")
        return ParsedDocument.model_validate_json(path.read_text(encoding="utf-8"))

    def submit_publish(self, job_id: str, url: str, title: str) -> dict:
        self.parsed(job_id)
        with self.lock:
            job = self.store.get(job_id)
            if job["status"] in {"publishing", "verifying", "archiving"}:
                return job
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
            claimed = self.store.claim(
                job_id,
                target=target.model_dump(),
                app_id=self.settings.feishu_app_id,
                title=title,
                requested_title=job["requested_title"],
            )
            self.executor.submit(self.publish, job_id)
        return claimed

    def publish(self, job_id: str):
        try:
            job = self.store.get(job_id)
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
                    and previous["digest"] == job["digest"]
                    and previous.get("app_id") == job["app_id"]
                    and previous.get("target", {}).get("node_token") == target.node_token
                    and previous.get("target", {}).get("space_id") == target.space_id
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
                    if self.client.download_digest(attachment["token"]) != job["digest"]:
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
                    )
                    return
            folder = self.folder(job_id)
            result = publisher.publish(
                self.parsed(job_id),
                folder / "source.pdf",
                folder / "assets",
                job["title"],
                target,
                job["digest"],
                job["filename"],
            )
            self.store.update(
                job_id, status="succeeded", progress="已保存并核对知识库位置", **result
            )
        except Exception as exc:
            self.fail(job_id, exc)

    def fail(self, job_id: str, exc: Exception):
        message = (
            str(exc)
            if isinstance(exc, UserError)
            else (f"处理失败（{type(exc).__name__}），请检查本地依赖、模型和任务文件后重试。")
        )
        logging.getLogger(__name__).warning("job=%s error_type=%s", job_id, type(exc).__name__)
        self.store.update(
            job_id,
            status="needs_review" if isinstance(exc, UncertainWrite) else "failed",
            error=message,
            progress="处理已停止",
        )

    def close(self):
        self.executor.shutdown(wait=True)
        self.client.close()
