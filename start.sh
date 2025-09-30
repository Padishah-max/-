#!/usr/bin/env bash
set -e
echo 'Shell:' $(which bash)
python3 -V || true

# venv
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -V

# force reinstall PTB v20.7
python -m pip install --upgrade pip
python -m pip uninstall -y python-telegram-bot telegram || true
python -m pip install --no-cache-dir --force-reinstall "python-telegram-bot==20.7"
python -c "import telegram, sys; print('PTB after install:', getattr(telegram,'__version__','?'), 'from', getattr(telegram,'__file__','?'))"

exec python -u main.py
