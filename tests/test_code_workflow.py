import copy

from fastapi.testclient import TestClient

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings
from files_to_feishu.models import CodeSource, Element, ParsedDocument
from tests.integrations.feishu.test_publisher import MemoryFeishu
from tests.test_workflow import wait


def test_code_review_persistence_publish_lock_and_changed_conversion_dedup(tmp_path, pdf_bytes):
    app = create_app(Settings(data_dir=tmp_path, feishu_app_id="test", feishu_app_secret="secret"))
    service = app.state.service
    service.client.close()
    remote = MemoryFeishu()
    service.client = remote
    source = [Element(kind="image", page=1, asset="figure-1.png")]

    def parser(source_file, assets, progress):
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "figure-1.png").write_bytes(b"fake image bytes")
        return ParsedDocument(pages=1, elements=copy.deepcopy(source))

    service.parser = parser
    payload = {"url": "https://test.feishu.cn/wiki/parent", "title": "Code"}
    with TestClient(app) as client:

        def upload():
            job = client.post("/api/jobs", files={"file": ("sample.pdf", pdf_bytes)}).json()
            wait(client, job["id"], {"ready"})
            return job["id"]

        def publish(job_id):
            assert client.post(f"/api/jobs/{job_id}/publish", json=payload).status_code == 202
            result = wait(client, job_id, {"succeeded", "failed"})
            assert result["status"] == "succeeded", result
            return result

        old_id = upload()
        publish(old_id)
        assert remote.created == 1
        source[:] = [
            Element(
                kind="code",
                page=1,
                text="const n = 1",
                code_origin="ocr",
                code_sources=[CodeSource(page=1), CodeSource(page=2)],
            )
        ]
        new_id = upload()
        assert client.post(f"/api/jobs/{new_id}/publish", json=payload).status_code == 400
        assert remote.created == 1
        edit = {"text": "const n = 1\n  n.toUperCase()", "language": "javascript"}
        assert client.post(f"/api/jobs/{new_id}/code/0", json=edit).status_code == 200
        restored = client.get(f"/api/jobs/{new_id}").json()["preview"]["elements"][0]
        assert restored["text"] == edit["text"]
        assert restored["code_reviewed"] is True
        assert restored["code_origin"] == "manual"
        assert [item["page"] for item in restored["code_sources"]] == [1, 2]
        publish(new_id)
        assert remote.created == 2  # New code content must not reuse the old image document.
        assert client.post(f"/api/jobs/{new_id}/code/0", json=edit).status_code == 400
        source[:] = [Element.model_validate(restored)]
        duplicate_id = upload()
        publish(duplicate_id)
        assert remote.created == 2
        image_id = upload()
        # Even an uncertain first write (no known document ID yet) freezes preview edits.
        service.store.update(image_id, journal={"document": {"state": "pending"}})
        assert client.post(f"/api/jobs/{image_id}/code/0", json=edit).status_code == 400


def test_manual_image_to_code_rejects_invalid_content_and_preserves_reference(tmp_path, pdf_bytes):
    app = create_app(Settings(data_dir=tmp_path))
    service = app.state.service
    job = service.store.create("sample.pdf", "digest")
    folder = service.folder(job["id"])
    folder.mkdir(parents=True)
    (folder / "parsed.json").write_text(
        ParsedDocument(
            pages=1,
            elements=[
                Element(kind="image", page=1, asset="figure-1.png"),
                Element(kind="text", page=1, text="ordinary text"),
            ],
        ).model_dump_json()
    )
    service.store.update(job["id"], parsed=True, status="ready")
    with TestClient(app) as client:
        base = f"/api/jobs/{job['id']}/code/"
        for index, body, status in [
            (0, {"text": "   "}, 400),
            (0, {"text": "x" * 20001}, 422),
            (0, {"text": "code", "language": "invalid"}, 422),
            (1, {"text": "code"}, 400),
            (9, {"text": "code"}, 400),
        ]:
            assert client.post(base + str(index), json=body).status_code == status
        assert (
            client.post(base + "0", json={"text": "<script>alert(1)</script>"}).status_code == 200
        )
        result = service.parsed(job["id"]).elements[0]
        assert result.kind == "code"
        assert result.asset == "figure-1.png"
        assert result.text == "<script>alert(1)</script>"
