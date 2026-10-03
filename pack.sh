#!/bin/sh
# Package committed source only; ignored runtime data is never discovered by tar.
set -eu
REVIEWPILOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
case "${1:-}" in
  --help|-h) printf '%s\n' 'Usage: ./pack.sh [output.tar.gz]' 'Archives the current Git HEAD as reviewpilot/; uncommitted edits are excluded.'; exit 0 ;;
esac
if [ "$#" -gt 1 ]; then
  printf '%s\n' 'Too many arguments.' >&2
  exit 2
fi
REVIEWPILOT_OUTPUT=${1:-"$REVIEWPILOT_DIR/../reviewpilot-v0.1.0.tar.gz"}
REVIEWPILOT_GIT_ROOT=$(git -C "$REVIEWPILOT_DIR" rev-parse --show-toplevel)
REVIEWPILOT_GIT_ROOT=$(CDPATH= cd -- "$REVIEWPILOT_GIT_ROOT" && pwd -P)
if [ "$REVIEWPILOT_GIT_ROOT" != "$REVIEWPILOT_DIR" ]; then
  printf '%s\n' 'Refusing to package an enclosing repository; this source directory must be the Git root.' >&2
  exit 1
fi
git -C "$REVIEWPILOT_DIR" rev-parse --verify HEAD >/dev/null
git -C "$REVIEWPILOT_DIR" archive --format=tar.gz --prefix=reviewpilot/ \
  --output="$REVIEWPILOT_OUTPUT" HEAD
printf '%s\n' "$REVIEWPILOT_OUTPUT"
