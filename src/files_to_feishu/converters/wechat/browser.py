"""An ephemeral verification browser, independent of the user's browser profile."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page, Playwright

from ...models import UserError
from .network import SafeFetcher

_SUFFIXES = (".qq.com", ".weixin.qq.com", ".qpic.cn", ".qlogo.cn", ".gtimg.com", ".tencent.com")


class VerificationBrowser:
    """All methods must run on the same acquisition worker thread."""

    def __init__(self, max_html_bytes: int, timeout: float) -> None:
        self.max_html_bytes = max_html_bytes
        self.timeout = timeout
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._fetcher = SafeFetcher(timeout)
        self._downloaded = 0

    def open(self, url: str) -> None:
        from playwright.sync_api import Error, Route, sync_playwright

        def guard(route: Route) -> None:
            try:
                target = route.request.url
                host = urlsplit(target).hostname or ""
                if not any(host.endswith(suffix) or host == suffix[1:] for suffix in _SUFFIXES):
                    route.abort()
                    return
                remaining = self.max_html_bytes * 20 - self._downloaded
                if remaining <= 0:
                    route.abort()
                    return
                resource = self._fetcher.request(
                    route.request.method,
                    target,
                    min(self.max_html_bytes, remaining),
                    headers=route.request.all_headers(),
                    content=route.request.post_data_buffer or b"",
                )
                self._downloaded += len(resource.body)
                route.fulfill(status=resource.status, headers=resource.headers, body=resource.body)
            except (UserError, ValueError, Error):
                route.abort()

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._launch(self._playwright)
            self._context = self._browser.new_context(
                accept_downloads=False,
                service_workers="block",
                viewport={"width": 1000, "height": 850},
            )
            self._context.route("**/*", guard)
            self._context.route_web_socket("**/*", lambda websocket: websocket.close())
            self._page = self._context.new_page()
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
        except Error as exc:
            self.close()
            raise UserError(
                "无法打开独立验证浏览器。请运行 uv run playwright install chromium 后重新获取。"
            ) from exc

    def _launch(self, runtime: Playwright) -> Browser:
        return runtime.chromium.launch(headless=False)

    def content(self) -> str:
        from playwright.sync_api import Error

        if self._page is None:
            raise UserError("验证会话已结束，请重新获取文章。")
        try:
            if self._page.is_closed():
                raise UserError("验证窗口已关闭，请重新获取文章。")
            html = self._page.content()
            if len(html.encode("utf-8")) > self.max_html_bytes:
                raise UserError("文章超过 HTML 大小限制。")
            return html
        except Error as exc:
            raise UserError("无法读取验证窗口，请完成验证后再继续获取。") from exc

    def current_url(self) -> str:
        if self._page is None or self._page.is_closed():
            raise UserError("验证窗口已关闭，请重新获取文章。")
        return self._page.url

    def pump(self) -> None:
        from playwright.sync_api import Error

        if self._page is None or self._page.is_closed():
            raise UserError("验证窗口已关闭，请重新获取文章。")
        try:
            self._page.wait_for_timeout(100)
        except Error as exc:
            raise UserError("验证窗口不可用，请重新获取文章。") from exc

    def close(self) -> None:
        from playwright.sync_api import Error

        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
        except Error:
            # A user may have already closed the window; still stop the driver.
            pass
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self._fetcher.close()
            self._page = self._context = self._browser = self._playwright = None


BrowserFactory = Callable[[int, float], VerificationBrowser]
