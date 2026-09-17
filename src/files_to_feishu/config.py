from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_parent_url: str = ""
    data_dir: Path = Path(".data")
    docling_artifacts_path: Path = Path(".models")
    max_bytes: int = 20 * 1024 * 1024
    max_pages: int = 100

    @property
    def configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret.get_secret_value())
