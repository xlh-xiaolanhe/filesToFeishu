from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    feishu_app_id: str = ""
    feishu_app_secret: SecretStr = SecretStr("")
    feishu_parent_url: str = ""
    feishu_notify_enabled: bool = False
    feishu_notify_receive_id_type: Literal["open_id", "user_id", "union_id", "email", "chat_id"] = (
        "open_id"
    )
    feishu_notify_receive_id: str = ""
    data_dir: Path = Path(".data")
    docling_artifacts_path: Path = Path(".models")
    max_bytes: int = 20 * 1024 * 1024
    max_pages: int = 100
    wechat_max_html_bytes: int = 8 * 1024 * 1024
    wechat_max_asset_bytes: int = 20 * 1024 * 1024
    wechat_max_total_bytes: int = 100 * 1024 * 1024
    wechat_timeout: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.feishu_app_id and self.feishu_app_secret.get_secret_value())
