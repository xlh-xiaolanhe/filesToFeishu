import httpx
import pytest

from files_to_feishu.config import Settings
from files_to_feishu.integrations.feishu import FeishuClient, parse_wiki_url
from files_to_feishu.models import UncertainWrite, UserError


@pytest.mark.parametrize(
    "url",
    [
        "http://company.feishu.cn/wiki/abc",
        "https://feishu.cn.evil.test/wiki/abc",
        "https://company.feishu.cn/docx/abc",
        "https://user@company.feishu.cn/wiki/abc",
    ],
)
def test_invalid_target_urls_are_rejected(url):
    with pytest.raises(UserError):
        parse_wiki_url(url)


def test_a_timed_out_document_creation_is_never_blindly_retried():
    writes = []

    def handle(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        writes.append(request)
        raise httpx.ReadTimeout("lost response", request=request)

    client = FeishuClient(
        Settings(feishu_app_id="app", feishu_app_secret="secret"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(UncertainWrite):
        client.request("POST", "/docx/v1/documents", json={"title": "sample"})
    assert len(writes) == 1


@pytest.mark.parametrize("persistent", [False, True])
def test_non_json_rate_limit_is_retried_without_uncertain_write(monkeypatch, persistent):
    monkeypatch.setattr("files_to_feishu.integrations.feishu.client.time.sleep", lambda _: None)
    calls = []

    def handle(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": "token",
                    "expire": 7200,
                },
            )
        calls.append(request)
        if persistent or len(calls) == 1:
            return httpx.Response(429, content=b"Too Many Requests")
        return httpx.Response(200, json={"code": 0, "data": {"children": []}})

    client = FeishuClient(
        Settings(feishu_app_id="app", feishu_app_secret="secret"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    if persistent:
        with pytest.raises(UserError, match="限流") as caught:
            client.request("POST", "/docx/v1/documents/doc/blocks/doc/children", json={})
        assert not isinstance(caught.value, UncertainWrite)
        assert len(calls) == 4
    else:
        assert client.request("POST", "/docx/v1/documents/doc/blocks/doc/children", json={}) == {
            "children": []
        }
        assert len(calls) == 2


def test_rate_limit_refresh_pagination_and_original_bytes(tmp_path, monkeypatch):
    import hashlib
    from email.parser import BytesParser
    from email.policy import default

    monkeypatch.setattr("files_to_feishu.integrations.feishu.client.time.sleep", lambda _: None)
    tokens, calls, uploaded = [], [], []
    payload = b"%PDF- original bytes \x00\xff\n"
    source = tmp_path / "source.pdf"
    source.write_bytes(payload)

    def handle(request):
        path = request.url.path
        if "tenant_access_token" in path:
            tokens.append(1)
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": f"token{len(tokens)}", "expire": 7200}
            )
        calls.append(path)
        if path.endswith("upload_all"):
            multipart = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            parts = {
                p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                for p in multipart.iter_parts()
            }
            assert parts["file"] == payload
            assert parts["parent_node"] == b"file-block"
            assert parts["parent_type"] == b"docx_file"
            uploaded.append(parts["file"])
            if len(uploaded) == 1:
                return httpx.Response(429, json={"code": 99991400})
            return httpx.Response(200, json={"code": 0, "data": {"file_token": "file"}})
        if path.endswith("download"):
            return httpx.Response(200, content=payload)
        if path.endswith("blocks"):
            if len(tokens) == 1:
                return httpx.Response(200, json={"code": 99991663})
            second = request.url.params.get("page_token") == "next"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [{"block_id": "second" if second else "first"}],
                        "has_more": not second,
                        "page_token": "next",
                    },
                },
            )
        raise AssertionError(path)

    client = FeishuClient(
        Settings(feishu_app_id="app", feishu_app_secret="secret"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    assert client.upload("file-block", source, "file")["file_token"] == "file"
    assert len(uploaded) == 2
    assert client.download_digest("file") == hashlib.sha256(payload).hexdigest()
    assert [b["block_id"] for b in client.blocks("doc")] == ["first", "second"]
    assert len(tokens) == 2


def test_download_retries_rate_limit_and_refreshes_token(monkeypatch):
    import hashlib

    monkeypatch.setattr("files_to_feishu.integrations.feishu.client.time.sleep", lambda _: None)
    attempts = []

    def handle(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "fresh", "expire": 7200}
            )
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, json={"code": 99991400})
        if len(attempts) == 2:
            return httpx.Response(401, json={"code": 99991663})
        return httpx.Response(200, content=b"original")

    client = FeishuClient(
        Settings(feishu_app_id="app", feishu_app_secret="secret"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    assert client.download_digest("file") == hashlib.sha256(b"original").hexdigest()
    assert len(attempts) == 3


def test_missing_scope_error_names_the_required_permission():
    def handle(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        return httpx.Response(
            400,
            json={
                "code": 99991672,
                "msg": "Access denied. Required: [docx:document, docx:document:create]",
            },
        )

    client = FeishuClient(
        Settings(feishu_app_id="app", feishu_app_secret="secret"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(UserError, match="docx:document.*发布"):
        client.request("POST", "/docx/v1/documents", json={"title": "test"})


def test_wiki_membership_error_is_distinct_from_api_scope():
    def handle(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        return httpx.Response(
            400, json={"code": 131006, "msg": "permission denied: wiki space permission denied"}
        )

    client = FeishuClient(
        Settings(feishu_app_id="app", feishu_app_secret="secret"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(UserError, match="知识库成员权限.*接口权限不同"):
        client.request("POST", "/wiki/v2/spaces/space/nodes/move_docs_to_wiki", json={})


def test_download_redirect_never_forwards_feishu_credentials_to_asset_host():
    import hashlib

    requests = []

    def handle(request):
        requests.append(request)
        if "tenant_access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "private-token", "expire": 7200}
            )
        if request.url.host == "open.feishu.cn":
            assert request.headers["authorization"] == "Bearer private-token"
            return httpx.Response(302, headers={"Location": "https://media.example.org/file.zip"})
        assert "authorization" not in request.headers
        assert b"private-token" not in request.content
        assert b"private-secret" not in request.content
        return httpx.Response(200, content=b"original-zip")

    client = FeishuClient(
        Settings(feishu_app_id="app", feishu_app_secret="private-secret"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    assert client.download_digest("file") == hashlib.sha256(b"original-zip").hexdigest()
    assert len(requests) == 3


def test_recovery_metadata_and_bot_identity_use_current_application():
    import json

    calls = []

    def handle(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "app-token", "expire": 7200}
            )
        assert request.headers["authorization"] == "Bearer app-token"
        calls.append(request)
        if request.url.path.endswith("metas/batch_query"):
            assert request.url.params["user_id_type"] == "open_id"
            assert json.loads(request.content) == {
                "request_docs": [{"doc_token": "doc", "doc_type": "docx"}]
            }
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "metas": [
                            {
                                "doc_token": "doc",
                                "doc_type": "docx",
                                "title": "My title",
                                "owner_id": "bot-id",
                            }
                        ]
                    },
                },
            )
        assert request.url.path.endswith("bot/v3/info")
        return httpx.Response(200, json={"code": 0, "bot": {"open_id": "bot-id"}})

    client = FeishuClient(
        Settings(feishu_app_id="app", feishu_app_secret="secret"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    assert client.document_metadata("doc")["owner_id"] == client.bot_open_id() == "bot-id"
    assert len(calls) == 2


@pytest.mark.parametrize("response", [{"metas": []}, {"metas": [{"doc_token": "other"}]}])
def test_incomplete_metadata_is_not_recovery_evidence(response):
    def handle(request):
        if "tenant_access_token" in request.url.path:
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "token", "expire": 7200}
            )
        return httpx.Response(200, json={"code": 0, "data": response})

    client = FeishuClient(
        Settings(feishu_app_id="app", feishu_app_secret="secret"),
        httpx.Client(transport=httpx.MockTransport(handle)),
    )
    with pytest.raises(UserError, match="完整元数据"):
        client.document_metadata("doc")
