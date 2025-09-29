#!/usr/bin/env bash
set -e
python3 -V || true
pip3 uninstall -y python-telegram-bot telegram || true
pip3 install --upgrade --no-cache-dir -r requirements.txt
python3 -u main.py
