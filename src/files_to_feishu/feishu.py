import hashlib
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings
from .models import Target, UncertainWrite, UserError

BASE = "https://open.feishu.cn/open-apis"


def parse_wiki_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url.strip())
    host = parsed.hostname or ""
    match = re.fullmatch(r"/wiki/([A-Za-z0-9]+)/*", parsed.path)
    if (
        parsed.scheme != "https"
        or not (host == "feishu.cn" or host.endswith(".feishu.cn"))
        or parsed.username
        or parsed.password
        or parsed.netloc != host
        or not match
    ):
        raise UserError(
            "请输入 HTTPS 飞书知识库父页面链接，例如 https://公司.feishu.cn/wiki/节点。"
        )
    return host, match.group(1)


class FeishuClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
        self.token = ""
        self.expires = 0.0
        self.lock = threading.RLock()

    def close(self):
        self.client.close()

    def _token(self):
        with self.lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            if not self.settings.configured:
                raise UserError("请在本地 .env 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET 后重启。")
            try:
                response = self.client.post(
                    f"{BASE}/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self.settings.feishu_app_id,
                        "app_secret": self.settings.feishu_app_secret.get_secret_value(),
                    },
                )
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise UserError("无法连接飞书鉴权服务，请检查网络后重试。") from exc
            if data.get("code") != 0 or not data.get("tenant_access_token"):
                raise UserError(
                    f"飞书鉴权失败（code={data.get('code')}），请检查应用凭证与发布状态。"
                )
            self.token = data["tenant_access_token"]
            self.expires = time.monotonic() + max(0, data.get("expire", 0) - 120)
            return self.token

    def request(self, method: str, path: str, **kwargs) -> dict:
        writing = method != "GET"
        for attempt in range(4):
            try:
                response = self.client.request(
                    method,
                    BASE + path,
                    headers={"Authorization": f"Bearer {self._token()}"},
                    **kwargs,
                )
            except httpx.TransportError as exc:
                if writing:
                    raise UncertainWrite(
                        "飞书写入响应丢失，操作可能已生效，已停止自动重试。"
                    ) from exc
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise UserError("飞书读取失败，请检查网络。") from exc
            if response.status_code >= 500 and writing:
                raise UncertainWrite("飞书服务端异常，无法确定写入是否生效；请核对远端文档。")
            try:
                body = response.json()
            except ValueError as exc:
                error_type = UncertainWrite if writing else UserError
                raise error_type("飞书返回无法识别的响应，请核对任务状态。") from exc
            code = body.get("code")
            if response.status_code == 429 or code == 99991400:
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise UserError("飞书接口持续限流，请稍后重试。")
            if code in {99991663, 99991664, 99991665, 99991668} and attempt == 0:
                self.expires = 0
                continue
            if response.status_code >= 500 and attempt < 3:
                time.sleep(2**attempt)
                continue
            if not response.is_success or code != 0:
                raise UserError(
                    f"飞书接口失败（HTTP {response.status_code}，code={code}）。"
                    "请检查应用接口权限、知识库成员权限及文件大小。"
                )
            return body.get("data") or {}
        raise UserError("飞书请求未完成，请稍后重试。")

    def resolve(self, url: str) -> Target:
        host, token = parse_wiki_url(url)
        node = self.node(token)
        if not node.get("space_id") or not node.get("node_token"):
            raise UserError("飞书未返回有效的知识库节点。")
        return Target(
            space_id=node["space_id"],
            node_token=node["node_token"],
            title=node.get("title", "未命名页面"),
            host=host,
        )

    def node(self, token: str, obj_type: str = "wiki") -> dict:
        return self.request(
            "GET", "/wiki/v2/spaces/get_node", params={"token": token, "obj_type": obj_type}
        )["node"]

    def blocks(self, document_id: str) -> list[dict]:
        items = []
        token = ""
        seen = set()
        while True:
            params: dict[str, str | int] = {"page_size": 500}
            if token:
                params["page_token"] = token
            data = self.request("GET", f"/docx/v1/documents/{document_id}/blocks", params=params)
            items.extend(data.get("items", []))
            if not data.get("has_more"):
                return items
            token = data.get("page_token", "")
            if not token or token in seen:
                raise UserError("飞书文档分页响应不完整，无法验证所有内容。")
            seen.add(token)

    def block(self, doc_id: str, block_id: str) -> dict:
        return self.request("GET", f"/docx/v1/documents/{doc_id}/blocks/{block_id}")["block"]

    def upload(self, block_id: str, source: Path, kind: str, filename: str = "") -> dict:
        # Use bytes so authentication/rate-limit retries do not reuse an exhausted stream.
        payload = source.read_bytes()
        if not payload or len(payload) > 20 * 1024 * 1024:
            raise UserError("素材为空或超过飞书单次上传 20 MB 限制。")
        return self.request(
            "POST",
            "/drive/v1/medias/upload_all",
            data={
                "file_name": filename or source.name,
                "parent_type": f"docx_{kind}",
                "parent_node": block_id,
                "size": str(len(payload)),
            },
            files={"file": (filename or source.name, payload, "application/octet-stream")},
        )

    def download_digest(self, token: str) -> str:
        try:
            response = self.client.get(
                f"{BASE}/drive/v1/medias/{token}/download",
                headers={"Authorization": f"Bearer {self._token()}"},
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UserError("无法下载原附件进行完整性核验，请检查素材下载权限。") from exc
        return hashlib.sha256(response.content).hexdigest()
