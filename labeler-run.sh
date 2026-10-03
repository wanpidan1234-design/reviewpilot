#!/usr/bin/env bash
# ReviewPilot jev 打标器入口：独立于 engine/observer，补充 JEV_API_KEY。
# 缺少环境变量时读取并 eval ~/.bashrc 中匹配的 export 行；运行前须审核这些行。
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "${JEV_API_KEY:-}" ]; then
  eval "$(grep -m1 '^export JEV_API_KEY=' "$HOME/.bashrc")"
fi

export PATH="$DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin"
exec "$DIR/.venv/bin/python" "$DIR/labeler.py" "$@"
