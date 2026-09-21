"""Read-only preflight before uv can change dependencies of a running server."""

import socket
import sys
import tomllib
import webbrowser
from pathlib import Path


def main() -> int:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.5):
            pass
    except ConnectionRefusedError:
        return 10  # No server: the shell may prepare/repair the environment.
    try:
        from files_to_feishu.config import Settings
        from files_to_feishu.launcher import URL, running_here

        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        if not running_here(Settings(), expected_version=project["project"]["version"]):
            return 10
    except (ImportError, RuntimeError, OSError, ValueError) as exc:
        print(f"启动前检查未通过：{exc}。请先停止原服务，再重新启动。", file=sys.stderr)
        return 1
    print(f"当前版本已在运行，保留其环境与任务，直接访问 {URL}")
    if "--no-open" not in sys.argv:
        webbrowser.open(URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
