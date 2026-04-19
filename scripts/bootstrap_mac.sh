#!/usr/bin/env bash
# Bootstrap the Qoder-native self-supervisor on macOS (works on Linux too).
#
# Idempotent: re-running is safe. Does the minimum needed to make the
# workflow runnable end-to-end on a fresh machine.
#
# Steps:
#   1. git init (on branch 'main' when supported) if the dir is not a repo
#   2. verify python3 is available and at least 3.9
#   3. create .venv (if missing) and install pytest into it
#   4. verify qodercli is available
#   5. ensure artifacts/, .qoder/commands/, .qoder/skills/, and .qoder/state/tasks/ exist
#   6. run preflight --fix to normalise .gitignore and re-check the env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "[bootstrap] repo root: $REPO_ROOT"

# --- 1. Git -----------------------------------------------------------------
if [ ! -d ".git" ]; then
  echo "[bootstrap] initializing git repository"
  if ! git init -b main >/dev/null 2>&1; then
    git init >/dev/null
    git symbolic-ref HEAD refs/heads/main 2>/dev/null || true
  fi
fi

# --- 2. Python 3 ------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "[bootstrap][error] python3 not found on PATH"
  echo "[bootstrap][hint]  install Python 3.9+ from https://www.python.org"
  echo "[bootstrap][hint]  or via Homebrew: brew install python"
  exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "[bootstrap] python3 version: $PY_VERSION"

PY_MAJ="$(python3 -c 'import sys; print(sys.version_info[0])')"
PY_MIN="$(python3 -c 'import sys; print(sys.version_info[1])')"
if [ "$PY_MAJ" -lt 3 ] || { [ "$PY_MAJ" -eq 3 ] && [ "$PY_MIN" -lt 9 ]; }; then
  echo "[bootstrap][error] python 3.9+ required, found $PY_VERSION"
  exit 1
fi

# --- 3. venv + pytest -------------------------------------------------------
if [ ! -d ".venv" ]; then
  echo "[bootstrap] creating .venv"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[bootstrap] checking pytest availability in .venv"
if python -c 'import pytest' >/dev/null 2>&1; then
  echo "[bootstrap] pytest already available in .venv"
else
  echo "[bootstrap] pytest missing in .venv; installing"
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet pytest
fi

# --- 4. Qoder CLI -----------------------------------------------------------
if ! command -v qodercli >/dev/null 2>&1; then
  echo "[bootstrap][error] qodercli not found on PATH"
  echo "[bootstrap][hint]  install qodercli and make sure it is callable"
  exit 1
fi
echo "[bootstrap] qodercli version: $(qodercli --version | tail -n 1)"

# --- 5. Required directories ------------------------------------------------
mkdir -p artifacts .qoder/commands .qoder/skills/self-supervisor-v1 .qoder/state/tasks
touch artifacts/.gitkeep

# --- 6. Preflight --fix -----------------------------------------------------
echo "[bootstrap] running preflight --fix"
python scripts/preflight.py --fix >/dev/null || true

echo "[bootstrap] done. Activate the venv with: source .venv/bin/activate"
echo "[bootstrap] next: python scripts/preflight.py   # verify environment"
