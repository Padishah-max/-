#!/usr/bin/env bash
set -e
python3 -V || true

# Create & use venv (PEP 668 safe)
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -V

python -m pip install --upgrade pip
python -m pip install --no-cache-dir -r requirements.txt

exec python -u main.py
