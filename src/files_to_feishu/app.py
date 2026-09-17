from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="PDF → 飞书知识库")

    @app.get("/api/health")
    def health():
        return {"feishu_configured": settings.configured}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return "<html lang='zh-CN'><title>PDF → 飞书知识库</title><h1>PDF → 飞书知识库</h1></html>"

    return app
