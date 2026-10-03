#!/usr/bin/env bash
# ReviewPilot lifecycle observer; reads GitHub and sends configured notifications.
# Inherits CLI routing without reading shell credential declarations.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
NVM_NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1 || true)"
NODE_BIN="${NVM_NODE_BIN:-$(dirname "$(readlink -f "$(command -v node 2>/dev/null || echo /usr/bin/node)")")}"
# 优先使用 OpenClaw runtime 安装，以减少 CLI 与 gateway 版本不匹配。
OPENCLAW_RUNTIME_BIN="$(ls -d "$HOME"/.local/share/openclaw-runtime/node-*/bin 2>/dev/null | sort -V | tail -1 || true)"
export PATH="${OPENCLAW_RUNTIME_BIN:+$OPENCLAW_RUNTIME_BIN:}$DIR/.venv/bin:$NODE_BIN:/usr/local/bin:/usr/bin:/bin"
exec "$DIR/.venv/bin/python" "$DIR/observer.py" "$@"
