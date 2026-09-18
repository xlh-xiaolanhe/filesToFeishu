#!/bin/bash
# Start from this checkout even when invoked from another directory or Finder.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$PROJECT_DIR"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    cat >&2 <<'EOF'
未找到项目的 Python 虚拟环境。请先在项目目录完成首次准备：
  uv sync --extra parser --locked
  uv run --extra parser python scripts/download_models.py
配置方法见 docs/guides/pdf-to-feishu.md，然后重新运行 ./start.sh。
EOF
    exit 1
fi

echo "正在启动 PDF → 飞书知识库……"
echo "启动成功后访问：http://127.0.0.1:8765"
echo "请保留此终端窗口；按 Ctrl+C 停止服务。"
exec "$PROJECT_DIR/.venv/bin/python" -m files_to_feishu
