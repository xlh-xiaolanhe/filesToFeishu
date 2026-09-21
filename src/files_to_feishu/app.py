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
from .launcher import server_identity
from .models import CodeLanguage, UserError
from .service import JobService
from .store import Store

HERE = Path(__file__).parent


class PublishRequest(BaseModel):
    url: str = Field(max_length=2048)
    title: str = Field(default="", max_length=200)
    confirmed: bool = False
    review_token: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


class ArticleRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    batch_id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")


class CodeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    language: CodeLanguage = "plaintext"


def public(job: dict) -> dict:
    return {
        **{k: v for k, v in job.items() if k not in {"journal", "app_id"}},
        "content_locked": bool(
            job.get("journal") or job.get("document_id") or job["status"] == "publish_queued"
        ),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
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
        }

    @app.get("/")
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html")

    @app.post("/api/targets/resolve")
    def resolve(body: PublishRequest):
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
    def jobs():
        return [public(j) for j in store.list()]

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
        result = public(get_job(job_id))
        if result.get("parsed"):
            parsed = service.parsed(job_id)
            result["preview"] = parsed.model_dump()
            result["review_token"] = service.review_token(parsed)
        return result

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
            service.submit_publish(job_id, body.url, body.title, body.confirmed, body.review_token)
        )

    @app.post("/api/jobs/{job_id}/code/{index}")
    def save_code(job_id: str, index: int, body: CodeRequest):
        get_job(job_id)
        return public(service.save_code(job_id, index, body.text, body.language))

    return app
