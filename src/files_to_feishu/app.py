import fcntl
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .integrations.feishu import FeishuClient
from .job_models import RevisionConflict, allowed_actions
from .launcher import server_identity
from .models import CodeLanguage, UserError
from .notifications import Notification
from .service import JobService
from .store import JobNotFound, Store

HERE = Path(__file__).parent


class TargetRequest(BaseModel):
    url: str = Field(max_length=2048)
    title: str = Field(default="", max_length=200)


class ReviewRequest(TargetRequest):
    review_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=1)


class PublishRequest(TargetRequest):
    confirmed: bool = False
    review_token: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    confirmation_token: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class RecoveryRequest(BaseModel):
    key: str = Field(min_length=1, max_length=150)
    candidate: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=1)


class ArticleRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    batch_id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")


class CodeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    language: CodeLanguage = "plaintext"
    expected_revision: int = Field(ge=1)


def public(job: dict) -> dict:
    return {
        **{
            k: v
            for k, v in job.items()
            if k
            not in {
                "journal",
                "app_id",
                "notifications",
                "publish_snapshot",
                "reviewed_snapshot",
                "confirmation_token",
            }
        },
        "notifications": [
            Notification.model_validate(receipt).public()
            for receipt in job.get("notifications", {}).values()
        ],
        "allowed_actions": allowed_actions(job),
        "content_locked": bool(
            job.get("has_journal")
            or job.get("journal")
            or job.get("document_id")
            or job.get("publish_snapshot")
            or job["status"] == "publish_queued"
        ),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # The legacy process must be stopped BEFORE schema migration, not only lifespan startup.
    with (settings.data_dir / "server.lock").open("a") as migration_lock:
        try:
            fcntl.flock(migration_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("已有服务使用此数据目录，请先停止旧服务再升级。") from exc
        store = Store(settings.data_dir / "jobs.sqlite3")
    service = JobService(settings, store, FeishuClient(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with (settings.data_dir / "server.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("已有服务使用此数据目录，请勿启动多个进程。") from exc
            store.recover()
            try:
                yield
            finally:
                service.close()
                fcntl.flock(lock, fcntl.LOCK_UN)

    app = FastAPI(title="内容 → 飞书知识库", lifespan=lifespan)
    app.state.service = service
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (origin and origin != str(request.base_url).rstrip("/")) or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                return JSONResponse({"detail": "仅允许本地页面发起操作。"}, status_code=403)
            try:
                size = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"detail": "无效请求长度。"}, status_code=400)
            if size > settings.max_bytes + 1024 * 1024:
                return JSONResponse({"detail": "文件超过 20 MB 限制。"}, status_code=413)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(UserError)
    async def user_error(request: Request, exc: UserError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RevisionConflict)
    async def revision_conflict(request: Request, exc: RevisionConflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(JobNotFound)
    async def job_not_found(request: Request, exc: JobNotFound) -> JSONResponse:
        return JSONResponse({"detail": "任务不存在或已删除。"}, status_code=404)

    def get_job(job_id: str):
        if not re.fullmatch("[a-f0-9]{32}", job_id):
            raise HTTPException(404, "任务不存在。")
        try:
            return store.get(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在。") from exc

    @app.get("/api/health")
    def health():
        return {
            **server_identity(settings),
            "feishu_configured": settings.configured,
            "models_ready": (settings.docling_artifacts_path / ".ready").is_file(),
            "default_target": settings.feishu_parent_url,
            "max_bytes": settings.max_bytes,
            "notifications_enabled": settings.feishu_notify_enabled,
        }

    @app.get("/")
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @app.post("/api/targets/resolve")
    def resolve(body: TargetRequest):
        return service.client.resolve(body.url)

    @app.post("/api/jobs", status_code=202)
    def upload(
        file: UploadFile,
        batch_id: Annotated[str, Form(pattern=r"^(?:[a-f0-9]{32})?$")] = "",
    ):
        try:
            return public(
                service.receive(
                    file.filename or "", file.file.read(settings.max_bytes + 1), batch_id
                )
            )
        finally:
            file.file.close()

    @app.get("/api/jobs")
    def jobs(limit: int = 50, offset: int = 0, batch_id: str | None = None):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "分页参数无效。")
        return [
            public(j)
            for j in store.list_summaries(
                limit=limit,
                offset=offset,
                batch_id=batch_id,
            )
        ]

    @app.get("/api/job-history")
    def history(limit: int = 50, offset: int = 0, batch_id: str | None = None):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(422, "分页参数无效。")
        total = store.count(batch_id=batch_id)
        items = store.list_summaries(limit=limit, offset=offset, batch_id=batch_id)
        return {
            "items": [public(j) for j in items],
            "total": total,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
        }

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str) -> dict[str, bool]:
        if not re.fullmatch("[a-f0-9]{32}", job_id):
            raise HTTPException(404, "任务不存在。")
        service.delete(job_id)
        return {"deleted": True}

    @app.post("/api/jobs/wechat", status_code=202)
    def article(body: ArticleRequest):
        return public(service.receive_wechat(body.url, body.batch_id))

    @app.post("/api/jobs/{job_id}/continue", status_code=202)
    def continue_article(job_id: str):
        get_job(job_id)
        return public(service.continue_wechat(job_id))

    @app.post("/api/jobs/{job_id}/cancel", status_code=202)
    def cancel_article(job_id: str):
        get_job(job_id)
        return public(service.cancel_wechat(job_id))

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        with service.lock:
            result = public(get_job(job_id))
            if result.get("parsed"):
                parsed = service.parsed(job_id)
                result = public(get_job(job_id))
                result["preview"] = parsed.model_dump()
                result["review_token"] = service.review_token(parsed)
            if result["status"] == "needs_review":
                result["recovery_steps"] = [
                    {"key": key, "kind": "document" if key == "document" else "upload"}
                    for key, value in get_job(job_id)["journal"].items()
                    if isinstance(value, dict)
                    and value.get("state") == "pending"
                    and (key == "document" or key.endswith(":upload"))
                ]
            return result

    @app.post("/api/jobs/{job_id}/review")
    def confirm_review(job_id: str, body: ReviewRequest):
        get_job(job_id)
        return service.confirm_review(
            job_id,
            body.url,
            body.title,
            body.review_token,
            body.expected_revision,
        )

    @app.post("/api/jobs/{job_id}/recover")
    def recover(job_id: str, body: RecoveryRequest):
        get_job(job_id)
        return public(
            service.recover_candidate(
                job_id,
                body.key,
                body.candidate,
                body.expected_revision,
            )
        )

    @app.post("/api/jobs/{job_id}/notifications/{event}/retry", status_code=202)
    def retry_notification(job_id: str, event: str):
        get_job(job_id)
        return public(service.retry_notification(job_id, event))

    @app.get("/api/jobs/{job_id}/assets/{name}")
    def asset(job_id: str, name: str):
        get_job(job_id)
        try:
            path, media_type = service.asset_file(job_id, name)
        except (FileNotFoundError, UserError) as exc:
            raise HTTPException(404, "素材不存在或尚未获取。") from exc
        return FileResponse(path, media_type=media_type)

    @app.get("/api/jobs/{job_id}/original")
    def original(job_id: str):
        entry = get_job(job_id)
        try:
            path, media_type = service.original_file(job_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, "原件尚未获取。") from exc
        return FileResponse(path, media_type=media_type, filename=entry["filename"])

    @app.post("/api/jobs/{job_id}/publish", status_code=202)
    def publish(job_id: str, body: PublishRequest):
        get_job(job_id)
        return public(
            service.submit_publish(
                job_id,
                body.url,
                body.title,
                body.confirmed,
                body.review_token,
                body.confirmation_token,
            )
        )

    @app.post("/api/jobs/{job_id}/code/{index}")
    def save_code(job_id: str, index: int, body: CodeRequest):
        get_job(job_id)
        return public(
            service.save_code(
                job_id,
                index,
                body.text,
                body.language,
                body.expected_revision,
            )
        )

    return app
