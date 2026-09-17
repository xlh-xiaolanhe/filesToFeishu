from fastapi.testclient import TestClient

from files_to_feishu.app import create_app
from files_to_feishu.config import Settings


def test_local_app_explains_missing_configuration_without_exposing_secrets(tmp_path):
    settings = Settings(data_dir=tmp_path, feishu_app_id="", feishu_app_secret="secret-value")
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["feishu_configured"] is False
        assert "secret-value" not in response.text
        assert client.get("/").status_code == 200
