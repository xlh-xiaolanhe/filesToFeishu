"""Real browser regression; opt in on machines with Chrome installed."""

import os

import httpx
import pytest
from playwright.sync_api import Browser, Playwright

from files_to_feishu.converters.wechat.browser import VerificationBrowser
from files_to_feishu.converters.wechat.network import SafeFetcher
from files_to_feishu.models import UserError


class HeadlessVerificationBrowser(VerificationBrowser):
    def _launch(self, runtime: Playwright) -> Browser:
        return runtime.chromium.launch(channel="chrome", headless=True)


@pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="opt-in isolated Chrome test")
def test_isolated_verifier_pumps_routes_preserves_ephemeral_cookies_and_blocks_private() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/verify":
            return httpx.Response(200, text="verified")
        return httpx.Response(
            200,
            text="""<!doctype html><body>challenge<script>
        setTimeout(async () => {
          const response = await fetch('/verify', {method: 'POST', body: 'answer'});
          document.body.innerText = await response.text();
          fetch('http://127.0.0.1/private').catch(() => {});
        }, 150);
        </script></body>""",
            headers={
                "content-type": "text/html",
                "set-cookie": "verification=ephemeral; Path=/; Secure",
            },
        )

    browser = HeadlessVerificationBrowser(1024 * 1024, 10)
    browser._fetcher.close()
    browser._fetcher = SafeFetcher(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=lambda host: ["93.184.216.34"],
    )
    try:
        browser.open("https://mp.weixin.qq.com/s/test-fixture")
        for _ in range(20):
            browser.pump()
            if ">verified<" in browser.content():
                break
        assert ">verified<" in browser.content()
        verification = next(request for request in requests if request.url.path == "/verify")
        assert verification.headers["cookie"] == "verification=ephemeral"
        assert verification.content == b"answer"
        assert all(request.headers["host"] == "mp.weixin.qq.com" for request in requests)
        assert all(request.url.host == "93.184.216.34" for request in requests)
        assert browser._context is not None
        assert len(browser._context.pages) == 1
    finally:
        browser.close()
    assert browser._page is None and browser._context is None
    with pytest.raises(UserError, match="重新获取"):
        browser.content()
