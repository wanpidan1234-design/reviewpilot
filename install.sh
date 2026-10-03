#!/bin/sh
# Install a source checkout only. Never starts a workflow or registers a service.
set -eu
umask 077
REVIEWPILOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
case "${1:-}" in
  --help|-h)
    printf '%s\n' 'ReviewPilot source installer' \
      'Usage: [PYTHON=/path/to/python3] ./install.sh [--verify-only]' \
      'Creates a venv, installs pinned binary wheels from official PyPI, and copies the empty example configuration if absent.' \
      'Does not clone repositories, read credentials, run tests/workflows, or configure services.'
    exit 0 ;;
  --verify-only) REVIEWPILOT_VERIFY_ONLY=1 ;;
  '') REVIEWPILOT_VERIFY_ONLY=0 ;;
  *) printf '%s\n' 'Unknown option; use --help.' >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then
  printf '%s\n' 'Too many arguments; use --help.' >&2
  exit 2
fi
if [ "$REVIEWPILOT_VERIFY_ONLY" -eq 0 ]; then
  REVIEWPILOT_PYTHON=${PYTHON:-python3}
  "$REVIEWPILOT_PYTHON" -I -B -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python >=3.10 is required; select an existing interpreter with PYTHON.")'
  if [ ! -e "$REVIEWPILOT_DIR/.venv" ]; then
    "$REVIEWPILOT_PYTHON" -I -B -m venv "$REVIEWPILOT_DIR/.venv"
  fi
  PIP_CONFIG_FILE=/dev/null "$REVIEWPILOT_DIR/.venv/bin/python" -I -B -m pip \
    --isolated --disable-pip-version-check --no-input install \
    --no-cache-dir --no-deps --only-binary=:all: --index-url https://pypi.org/simple \
    -r "$REVIEWPILOT_DIR/requirements.txt"
  if [ ! -e "$REVIEWPILOT_DIR/config.yml" ]; then
    cp "$REVIEWPILOT_DIR/config.example.yml" "$REVIEWPILOT_DIR/config.yml"
  fi
fi
if [ ! -x "$REVIEWPILOT_DIR/.venv/bin/python" ]; then
  printf '%s\n' 'No local virtual environment; nothing has been started.' >&2
  exit 1
fi
"$REVIEWPILOT_DIR/.venv/bin/python" -I -B - <<'PY'
import importlib.metadata
import sys
assert sys.version_info >= (3, 10), "Python >=3.10 is required"
for name, expected in (("PyYAML", "6.0.3"), ("ruff", "0.16.6")):
    actual = importlib.metadata.version(name)
    if actual != expected:
        raise SystemExit(f"{name}: expected {expected}, found {actual}")
    print(f"{name} {actual}")
print("ReviewPilot dependency metadata verified; no application modules imported.")
PY
