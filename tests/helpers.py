"""Explicit user-review requests for successful publish workflow tests."""

from collections.abc import Mapping

from fastapi.testclient import TestClient


def reviewed_payload(
    client: TestClient, job_id: str, payload: Mapping[str, object]
) -> dict[str, object]:
    current = client.get(f"/api/jobs/{job_id}")
    assert current.status_code == 200, current.text
    preview = current.json()
    review = client.post(
        f"/api/jobs/{job_id}/review",
        json={
            "url": payload["url"],
            "title": payload.get("title", ""),
            "review_token": preview["review_token"],
            "expected_revision": preview["content_revision"],
        },
    )
    assert review.status_code == 200, review.text
    return {
        **payload,
        "confirmed": True,
        "review_token": preview["review_token"],
        "confirmation_token": review.json()["confirmation_token"],
    }
