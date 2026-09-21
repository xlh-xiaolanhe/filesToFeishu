#!/bin/bash
# Start from this checkout even when invoked from another directory or Finder.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$PROJECT_DIR"

NO_SETUP=false
WECHAT_ONLY=false
for option in "$@"; do
    case "$option" in
        --no-setup) NO_SETUP=true ;;
        --wechat-only) WECHAT_ONLY=true ;;
        --no-open) ;;
        --help|-h)
            echo "用法：./start.sh [--wechat-only] [--no-setup] [--no-open]"
            echo "默认同步锁定依赖、准备缺失模型和浏览器、打开本地网页。"
            echo "--wechat-only 跳过 PDF 准备；--no-setup 不下载；--no-open 不打开浏览器。"
            exit 0 ;;
        *) echo "未知参数：${option}；请运行 ./start.sh --help。" >&2; exit 2 ;;
    esac
done

if [[ "$NO_SETUP" == false ]]; then
    if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
        if "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/check_running.py" "$@"; then
            exit 0
        else
            PREFLIGHT_STATUS=$?
            if [[ "$PREFLIGHT_STATUS" != 10 ]]; then exit "$PREFLIGHT_STATUS"; fi
        fi
    fi
    if ! command -v uv >/dev/null 2>&1; then
        echo "未找到 uv。请先安装 uv（例如 brew install uv），然后再次双击 start.command。" >&2
        echo "配置方法见 README.md；已有环境可运行 ./start.sh --no-setup。" >&2
        exit 1
    fi
    echo "正在同步项目锁定依赖…"
    if [[ "$WECHAT_ONLY" == true ]]; then
        uv sync --locked
    else
        uv sync --extra parser --locked
    fi
fi

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    cat >&2 <<'EOF'
未找到项目的 Python 虚拟环境。请先在项目目录完成首次准备：
  uv sync --extra parser --locked
  uv run --extra parser python scripts/download_models.py
配置方法见 README.md，然后重新运行 ./start.sh。
EOF
    exit 1
fi

if [[ ! -e "$PROJECT_DIR/.env" && -f "$PROJECT_DIR/.env.example" ]]; then
    # Noclobber also preserves a config created concurrently by another launcher.
    (set -o noclobber; cat "$PROJECT_DIR/.env.example" > "$PROJECT_DIR/.env") 2>/dev/null || true
fi

echo "正在启动 PDF / 公众号 → 飞书知识库……"
exec "$PROJECT_DIR/.venv/bin/python" -m files_to_feishu "$@"
