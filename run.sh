#!/usr/bin/env bash
# ReviewPilot cron 入口：注入 codex 所需密钥 + 保证 gh/codex/openclaw/venv 在 PATH。
# 缺少环境变量时读取并 eval ~/.bashrc 中匹配的 export 行；运行前须审核这些行。
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "${CRS_KEY:-}" ]; then
  eval "$(grep -m1 '^export CRS_KEY=' "$HOME/.bashrc")"
fi

# jev 行内打标（deep_gate 自动升档）用；可选：缺失只损失自动升档，engine 会在日志里喊，
# 审查与合并不受影响（旁路 labeler-run.sh 自带注入，不依赖这里）。
if [ -z "${JEV_API_KEY:-}" ]; then
  eval "$(grep -m1 '^export JEV_API_KEY=' "$HOME/.bashrc" || true)"
fi

# 优先选取 nvm 中的最高版本 Node；所选版本须满足已安装 OpenClaw CLI 的要求。
NVM_NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
NODE_BIN="${NVM_NODE_BIN:-$(dirname "$(readlink -f "$(command -v node 2>/dev/null || echo /usr/bin/node)")")}"
# 优先使用 OpenClaw runtime 安装，以减少 CLI 与 gateway 版本不匹配。
# runtime bin 排最前，Node 仍从 nvm 取。
OPENCLAW_RUNTIME_BIN="$(ls -d "$HOME"/.local/share/openclaw-runtime/node-*/bin 2>/dev/null | sort -V | tail -1)"
export PATH="${OPENCLAW_RUNTIME_BIN:+$OPENCLAW_RUNTIME_BIN:}$DIR/.venv/bin:$NODE_BIN:/usr/local/bin:/usr/bin:/bin"

exec "$DIR/.venv/bin/python" "$DIR/engine.py" "$@"
