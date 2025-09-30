#!/usr/bin/env bash
set -e
python3 -V || true

# venv
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -V

# deps
python -m pip install --upgrade pip
python -m pip install --no-cache-dir -r requirements.txt

# run
exec python -u main.py
