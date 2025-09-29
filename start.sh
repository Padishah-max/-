#!/usr/bin/env bash
set -e

# 1) Диагностика
python3 -V || true

# 2) Создаём и используем виртуальное окружение (обход PEP 668)
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate

# 3) Обновляем pip и ставим зависимости внутрь venv
python -m pip install --upgrade pip
python -m pip install --no-cache-dir -r requirements.txt

# 4) Запускаем бота из venv
exec python -u main.py
