#!/usr/bin/env bash
set -e
python3 -V || true
pip3 install -r requirements.txt
python3 -u main.py
