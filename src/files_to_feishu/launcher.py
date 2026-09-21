"""Prepare missing local resources and launch exactly one project server."""

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from importlib.metadata import version
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from .config import Settings

URL = "http://127.0.0.1:8765"
APP_VERSION = version("files-to-feishu")


def server_identity(settings: Settings) -> dict[str, str]:
    return {
        "service": "files-to-feishu",
        "version": APP_VERSION,
        "data_id": hashlib.sha256(str(settings.data_dir.resolve()).encode("utf-8")).hexdigest(),
    }


def running_here(settings: Settings, *, expected_version: str = APP_VERSION) -> bool:
    """Never stop an unknown process or silently open another checkout's data."""
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.5):
            pass
    except ConnectionRefusedError:
        return False
    try:
        with build_opener(ProxyHandler({})).open(URL + "/api/health", timeout=2) as response:
            status = json.load(response)
        expected = {**server_identity(settings), "version": expected_version}
        if not isinstance(status, dict) or any(status.get(k) != v for k, v in expected.items()):
            raise ValueError("服务身份或版本不匹配")
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(
            "8765 端口已有其他服务或旧版本运行。请先在原终端按 Ctrl+C 停止，再重新启动；"
            "脚本不会自动终止已有进程。"
        ) from exc
    return True


def prepare_resources(settings: Settings, *, wechat_only: bool) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        installed = Path(playwright.chromium.executable_path).is_file()
    if not installed:
        print("首次准备：下载公众号验证浏览器 Chromium…", flush=True)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    if not wechat_only and not (settings.docling_artifacts_path / ".ready").is_file():
        print("首次准备：下载 PDF 本地模型，可能需要几分钟…", flush=True)
        script = Path(__file__).resolve().parents[2] / "scripts/download_models.py"
        subprocess.run([sys.executable, str(script)], check=True)


def open_when_ready(settings: Settings) -> None:
    for _ in range(60):
        try:
            if running_here(settings):
                webbrowser.open(URL)
                return
        except (RuntimeError, OSError):
            pass
        time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description="一键启动 PDF / 公众号 → 飞书知识库")
    parser.add_argument(
        "--wechat-only", action="store_true", help="仅准备公众号功能，跳过 PDF 模型"
    )
    parser.add_argument(
        "--no-setup", action="store_true", help="使用已准备的环境，不下载依赖或模型"
    )
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    settings = Settings()
    try:
        if running_here(settings):
            print(f"当前版本已在运行，直接访问 {URL}")
            if not args.no_open:
                webbrowser.open(URL)
            return
        if not args.no_setup:
            prepare_resources(settings, wechat_only=args.wechat_only)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"启动未完成：{exc}\n配置与排查方法见 README.md。", file=sys.stderr)
        raise SystemExit(1) from exc
    if not settings.configured:
        print("可先转换和预览；发布前请填写 .env 中的飞书凭证并重启。", flush=True)
    print(f"本地工具：{URL}\n请保留终端窗口；按 Ctrl+C 停止服务。", flush=True)
    if not args.no_open:
        threading.Thread(target=open_when_ready, args=(settings,), daemon=True).start()
    import uvicorn

    uvicorn.run("files_to_feishu.app:create_app", factory=True, host="127.0.0.1", port=8765)
