import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup
from PIL import Image

from files_to_feishu.converters.wechat.browser import VerificationBrowser
from files_to_feishu.converters.wechat.converter import WechatConverter, offline_html
from files_to_feishu.converters.wechat.network import SafeFetcher, normalize_url, validate_https
from files_to_feishu.converters.wechat.parser import article_state, parse_article
from files_to_feishu.models import Asset, UserError

URL = "https://mp.weixin.qq.com/s/original-fixture"
HEADER = """<h1 id="activity-name">原创测试文章</h1><span id="js_name">测试号</span>
<span id="js_author_name">作者</span><span id="publish_time">2026-09-21</span>"""


def html(body: str) -> str:
    return HEADER + '<div id="js_content">' + body + "</div>"


def asset_loader(url: str) -> Asset:
    return Asset(name="image-test.gif", media_type="image/gif", digest="test", source_url=url)


def test_rich_content_order_lists_table_and_literal_code() -> None:
    document = parse_article(
        html("""
    <h2>小标题</h2><p>Hello <strong>bold <em>and italic</em></strong> world
    <del>removed</del> <code>obj.x</code> <a href="https://example.com">link</a>.</p>
    <p>before<img data-src="https://mmbiz.qpic.cn/test.gif" alt="演示">after</p>
    <blockquote><p>quote <b>important</b></p></blockquote>
    <ol start="3"><li>three<ul><li>nested</li></ul></li><li>four</li></ol>
    <table><tr><th>A</th><th>B</th></tr><tr><td><a href="https://example.com">cell</a></td><td>2</td></tr></table>
    <pre data-language="typescript"><code>function run() {
  const x = 1;
    return x;
}</code></pre>
    """),
        URL,
        asset_loader,
    )
    assert document.pages is None and document.page_images == []
    assert all(item.page is None for item in document.elements)
    assert document.metadata.title == "原创测试文章"
    assert document.metadata.account == "测试号"
    assert document.metadata.author == "作者"
    assert document.metadata.published_at == "2026-09-21"
    assert [item.kind for item in document.elements[:7]] == [
        "heading",
        "text",
        "text",
        "image",
        "text",
        "text",
        "quote",
    ]
    rich = document.elements[1]
    assert rich.text.startswith("Hello bold and italic world")
    assert any(run.bold and run.italic and run.text == "and italic" for run in rich.runs)
    assert any(run.strike for run in rich.runs)
    assert any(run.inline_code and run.text == "obj.x" for run in rich.runs)
    assert any(run.link == "https://example.com" for run in rich.runs)
    lists = [item for item in document.elements if item.kind in {"ordered", "bullet"}]
    assert [(item.text, item.list_depth, item.list_start) for item in lists] == [
        ("three", 0, 3),
        ("nested", 1, None),
        ("four", 0, 4),
    ]
    table = next(item for item in document.elements if item.kind == "table")
    assert table.rows == [["A", "B"], ["cell", "2"]]
    assert table.table_runs[1][0][0].link == "https://example.com"
    code = next(item for item in document.elements if item.kind == "code")
    assert code.text == "function run() {\n  const x = 1;\n    return x;\n}"
    assert code.language == "typescript" and code.code_reviewed


def test_no_inferred_language_or_missing_metadata() -> None:
    document = parse_article(
        '<div id="js_content"><pre>assert False</pre></div>', URL, asset_loader
    )
    assert document.elements[0].language == "plaintext"
    assert document.metadata.title == document.metadata.author == ""
    assert document.notices


def test_failed_image_media_complex_table_are_explicit() -> None:
    def missing(url: str) -> Asset:
        raise UserError("测试缺图")

    document = parse_article(
        html("""
    <p>text</p><img data-src="https://example.com/missing.jpg">
    <video title="示范视频" poster="https://example.com/cover.jpg"></video>
    <mp-miniprogram data-title="程序入口"></mp-miniprogram>
    <table><tr><td colspan="2">wide</td></tr><tr><td>one</td><td>two</td></tr></table>
    <svg><text>shape</text></svg>
    """),
        URL,
        missing,
    )
    reasons = [notice.reason for notice in document.notices]
    assert sum("图片未完整获取" in reason for reason in reasons) == 2
    assert any("示范视频" in reason for reason in reasons)
    assert any("程序入口" in reason for reason in reasons)
    assert any("复杂表格" in reason for reason in reasons)
    assert any("矢量图" in reason for reason in reasons)
    assert any(run.link == URL for item in document.elements for run in item.runs)


@pytest.mark.parametrize(
    ("body", "state"),
    [
        ("环境异常，完成验证后即可继续访问。去验证", "verification"),
        ("该内容已被发布者删除", "deleted"),
        (html('<p>preview</p><div class="pay_read_area">buy</div>'), "paid"),
        ("<h1>Unavailable</h1>", "unavailable"),
    ],
)
def test_access_interstitial_is_never_an_article(body: str, state: str) -> None:
    assert article_state(body) == state
    with pytest.raises(UserError):
        parse_article(body, URL, asset_loader)


def test_offline_snapshot_removes_executable_and_remote_content() -> None:
    source = html("""<script>alert('bad')</script><p onclick="bad()" style="background:url(https://evil)">
    hi <a href="javascript:alert(1)">link</a><img src="https://remote/image" onerror="bad()"></p>
    <iframe src="https://remote/frame"></iframe><svg onload="bad()"></svg><form>bad</form>""")
    document = parse_article(source, URL, asset_loader)
    snapshot = offline_html(source, document)
    soup = BeautifulSoup(snapshot, "html.parser")
    assert not soup.find(["script", "iframe", "svg", "form"])
    assert not soup.find(attrs={"onclick": True})
    assert not soup.find(attrs={"onerror": True})
    assert "javascript:" not in snapshot and "background:" not in snapshot
    assert soup.img is not None and soup.img["src"] == "assets/image-test.gif"
    assert "Content-Security-Policy" in snapshot


def fetcher_for(handler) -> SafeFetcher:
    return SafeFetcher(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=lambda host: ["93.184.216.34"],
    )


def test_url_canonicalization_and_ssrf_redirect_guard() -> None:
    assert normalize_url(URL + "?scene=1#x") == URL
    long = "https://mp.weixin.qq.com/s?sn=hash&mid=1&idx=2&__biz=abc&scene=2"
    assert normalize_url(long) == "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=2&sn=hash"
    for bad in (
        "http://mp.weixin.qq.com/s/abc",
        "https://evil/s/abc",
        "https://user@mp.weixin.qq.com/s/abc",
        "https://mp.weixin.qq.com:8443/s/abc",
    ):
        with pytest.raises(UserError):
            normalize_url(bad)
    for target in (
        "https://127.0.0.1/a",
        "file:///etc/passwd",
        "http://example.com",
        "https://example.com:8080",
    ):
        with pytest.raises(UserError):
            validate_https(target)
    with pytest.raises(UserError):
        validate_https("https://example.com/x", lambda host: ["127.0.0.1"])
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    fetcher = fetcher_for(redirect)
    with pytest.raises(UserError):
        fetcher.get(URL, 1000, article=True)
    assert len(calls) == 1
    assert calls[0].url.host == "93.184.216.34"
    assert calls[0].headers["host"] == "mp.weixin.qq.com"
    assert calls[0].extensions["sni_hostname"] == "mp.weixin.qq.com"
    assert "authorization" not in calls[0].headers


def test_download_limits_redirect_loop_and_timeout() -> None:
    too_large = fetcher_for(lambda request: httpx.Response(200, content=b"x" * 30))
    with pytest.raises(UserError, match="大小"):
        too_large.get(URL, 20)
    loop = fetcher_for(lambda request: httpx.Response(302, headers={"location": URL}))
    with pytest.raises(UserError, match="次数"):
        loop.get(URL, 100)

    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(UserError, match="网络"):
        fetcher_for(timeout).get(URL, 100)


def test_gif_archive_is_original_deterministic_and_bounded(tmp_path: Path) -> None:
    image = io.BytesIO()
    Image.new("RGB", (3, 3), "red").save(
        image, format="GIF", save_all=True, append_images=[Image.new("RGB", (3, 3), "blue")]
    )
    gif = image.getvalue()
    source = html('<p>first</p><img data-src="https://mmbiz.qpic.cn/test.gif"><p>last</p>')

    def handler(request):
        return httpx.Response(
            200, content=source.encode() if request.headers["host"] == "mp.weixin.qq.com" else gif
        )

    converter = WechatConverter(fetcher=fetcher_for(handler))
    document = converter.start(URL, tmp_path, lambda message: None)
    assert document is not None and len(document.assets) == 1
    asset = document.assets[0]
    assert asset.media_type == "image/gif" and asset.digest == hashlib.sha256(gif).hexdigest()
    assert (tmp_path / "assets" / asset.name).read_bytes() == gif
    before = (tmp_path / "source.zip").read_bytes()
    converter.start(URL, tmp_path, lambda message: None)
    assert (tmp_path / "source.zip").read_bytes() == before
    with zipfile.ZipFile(tmp_path / "source.zip") as archive:
        assert archive.read("assets/" + asset.name) == gif
        assert json.loads(archive.read("manifest.json"))["source"]["url"] == URL
        assert b"assets/image-" in archive.read("article.html")
    converter = WechatConverter(fetcher=fetcher_for(handler), max_total_bytes=1)
    missing = converter.start(URL, tmp_path / "limited", lambda message: None)
    assert missing is not None and not missing.assets and missing.notices


class FakeBrowser(VerificationBrowser):
    def __init__(self, max_html_bytes: int, timeout: float) -> None:
        super().__init__(max_html_bytes, timeout)
        self.value = "环境异常 完成验证"
        self.closed = False

    def open(self, url: str) -> None:
        self.url = url

    def content(self) -> str:
        return self.value

    def current_url(self) -> str:
        return self.url

    def close(self) -> None:
        self.closed = True


def test_manual_verification_continue_cancel_and_new_process(tmp_path: Path) -> None:
    fetcher = fetcher_for(lambda request: httpx.Response(200, text="环境异常 完成验证"))
    browser = FakeBrowser(1000, 10)
    converter = WechatConverter(fetcher=fetcher, browser_factory=lambda size, timeout: browser)
    assert converter.start(URL, tmp_path, lambda message: None) is None
    assert converter.resume(tmp_path, lambda message: None) is None
    browser.value = html("<p>verified</p>")
    result = converter.resume(tmp_path, lambda message: None)
    assert result is not None and result.elements[0].text == "verified" and browser.closed
    assert converter.browser is None
    with pytest.raises(UserError, match="重新获取"):
        converter.resume(tmp_path, lambda message: None)
    converter.start(URL, tmp_path, lambda message: None)
    converter.cancel()
    assert browser.closed and converter.browser is None
    restarted = WechatConverter(fetcher=fetcher)
    with pytest.raises(UserError, match="重新获取"):
        restarted.resume(tmp_path, lambda message: None)


def test_list_image_caption_and_following_paragraph_keep_order() -> None:
    document = parse_article(
        html("""<ul><li>first<img data-src="https://mmbiz.qpic.cn/a.gif"
    alt="caption">after<ul><li>nested</li></ul><p>tail</p></li></ul>
    <p>outside</p>"""),
        URL,
        asset_loader,
    )
    assert [(item.kind, item.text, item.list_depth) for item in document.elements[:-2]] == [
        ("bullet", "first", 0),
        ("image", "", 1),
        ("text", "caption", 1),
        ("text", "after", 1),
        ("bullet", "nested", 1),
        ("text", "tail", 1),
        ("text", "outside", 0),
    ]


def test_code_language_class_and_ordered_value_restart() -> None:
    document = parse_article(
        html("""<pre><code class="language-js">  let x = 1
</code></pre>
    <ol start="3"><li>three</li><li value="8">eight</li><li>nine</li></ol>"""),
        URL,
        asset_loader,
    )
    assert document.elements[0].language == "javascript"
    assert document.elements[0].text == "  let x = 1\n"
    assert [item.list_start for item in document.elements[1:4]] == [3, 8, 9]


@pytest.mark.parametrize(
    "url", ["https://mp.weixin.qq.com:bad/s/test", "https://[mp.weixin.qq.com/s/test"]
)
def test_malformed_url_is_a_user_error(url: str) -> None:
    with pytest.raises(UserError):
        normalize_url(url)


def test_cookie_jar_is_not_forwarded_to_same_ip_other_host() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="ok", headers={"set-cookie": "session=private; Path=/"})

    fetcher = fetcher_for(handler)
    fetcher.get(URL, 100)
    fetcher.get("https://example.com/image", 100)
    assert "cookie" not in seen[1].headers
    fetcher.request(
        "POST",
        "https://mp.weixin.qq.com/verify",
        100,
        headers={"cookie": "ephemeral=yes", "authorization": "secret"},
        content=b"answer",
    )
    assert seen[2].headers["cookie"] == "ephemeral=yes"
    assert "authorization" not in seen[2].headers


def test_slow_stream_uses_overall_deadline(monkeypatch) -> None:
    from files_to_feishu.converters.wechat import network

    times = iter([0.0, 0.0, 4.0])
    monkeypatch.setattr(network.time, "monotonic", lambda: next(times, 4.0))
    fetcher = fetcher_for(lambda request: httpx.Response(200, content=b"data"))
    fetcher.timeout = 1
    with pytest.raises(UserError, match="超时"):
        fetcher.get(URL, 100)


def test_block_and_ancestor_styles_survive_image_splits_and_cell_paragraphs() -> None:
    document = parse_article(
        html("""<p style="font-weight: bold">bold paragraph</p>
    <strong>before<img data-src="https://mmbiz.qpic.cn/a.gif">after</strong>
    <table><tr><td style="font-style:italic"><p>A</p><p>B</p></td></tr></table>"""),
        URL,
        asset_loader,
    )
    assert document.elements[0].runs[0].bold
    assert document.elements[1].runs[0].bold
    assert document.elements[3].runs[0].bold
    table = document.elements[4]
    assert table.rows[0][0].strip() == "A\nB"
    assert table.table_runs[0][0][0].italic
    assert all(not element.web_locator for element in document.elements)


def test_long_code_is_split_without_modification() -> None:
    code = "    value = 1\n" * 3000
    document = parse_article(html("<pre>" + code + "</pre>"), URL, asset_loader)
    blocks = [item for item in document.elements if item.kind == "code"]
    assert len(blocks) > 1 and all(len(item.text) <= 20000 for item in blocks)
    assert "".join(item.text for item in blocks) == code
    assert any("较长代码" in notice.reason for notice in document.notices)


def test_same_image_bytes_at_two_urls_retains_both_archive_references(tmp_path: Path) -> None:
    image = io.BytesIO()
    Image.new("RGB", (3, 3)).save(image, format="PNG")
    source = html('<img src="https://mmbiz.qpic.cn/a"><img src="https://mmbiz.qpic.cn/b">')

    def handler(request):
        body = (
            source.encode() if request.headers["host"] == "mp.weixin.qq.com" else image.getvalue()
        )
        return httpx.Response(200, content=body)

    converter = WechatConverter(fetcher=fetcher_for(handler))
    result = converter.start(URL, tmp_path, lambda message: None)
    assert result is not None and len(result.assets) == 2
    assert result.assets[0].name == result.assets[1].name
    with zipfile.ZipFile(tmp_path / "source.zip") as archive:
        snapshot = BeautifulSoup(archive.read("article.html"), "html.parser")
        assert len(snapshot.find_all("img")) == 2


def test_failed_asset_downloads_also_consume_total_budget(tmp_path: Path) -> None:
    source = html("".join(f'<img src="https://mmbiz.qpic.cn/{number}">' for number in range(4)))
    asset_requests = []

    def handler(request):
        if request.headers["host"] == "mp.weixin.qq.com":
            return httpx.Response(200, content=source.encode())
        asset_requests.append(request)
        return httpx.Response(200, content=b"x" * 20)

    converter = WechatConverter(
        fetcher=fetcher_for(handler), max_asset_bytes=10, max_total_bytes=20
    )
    result = converter.start(URL, tmp_path, lambda message: None)
    assert result is not None and len(asset_requests) == 2
    assert len(result.notices) == 4


def test_custom_media_and_background_image_are_explicit() -> None:
    document = parse_article(
        html("""<p style="background-image:url(https://example.com/x)">text</p>
    <mp-common-videosnap data-title="视频号"></mp-common-videosnap>"""),
        URL,
        asset_loader,
    )
    assert any("背景图片" in notice.reason for notice in document.notices)
    assert any("视频号" in notice.reason for notice in document.notices)


def test_quoted_access_errors_in_article_are_not_interstitials() -> None:
    document = parse_article(
        html("<p>关于提示：该内容已被发布者删除，付费后可阅读全文，环境异常。</p>"),
        URL,
        asset_loader,
    )
    assert document.elements[0].text.startswith("关于提示")


def test_verification_cannot_silently_import_another_article(tmp_path: Path) -> None:
    fetcher = fetcher_for(lambda request: httpx.Response(200, text="环境异常 完成验证"))
    browser = FakeBrowser(1000, 10)
    converter = WechatConverter(fetcher=fetcher, browser_factory=lambda size, timeout: browser)
    assert converter.start(URL, tmp_path, lambda message: None) is None
    browser.value = html("<p>another article</p>")
    browser.url = "https://mp.weixin.qq.com/s/another-article"
    with pytest.raises(UserError, match="另一篇文章"):
        converter.resume(tmp_path, lambda message: None)
    assert browser.closed and not (tmp_path / "source.zip").exists()
