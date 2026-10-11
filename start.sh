#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
    echo "Creating virtual environment..."
    "$PY" -m venv .venv
fi
if ! cmp -s requirements.txt .venv/installed-requirements.txt; then
    echo "Installing dependencies..."
    .venv/bin/python -m pip install --disable-pip-version-check -q --require-hashes -r requirements.txt
    cp requirements.txt .venv/installed-requirements.txt
fi
exec .venv/bin/python main.py "$@"
