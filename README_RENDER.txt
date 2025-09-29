# Render deploy (runtime pinned to Python 3.11)

Files:
- runtime.txt -> python-3.11.9 (Render будет использовать Python 3.11)
- requirements.txt -> python-telegram-bot==20.7
- start.sh -> устанавливает зависимости и запускает
- main.py -> бот (webhook, PTB v20)

Render settings:
- Start Command: bash start.sh
- Build Command: (leave empty)
- After deploy check Logs: should print "python-telegram-bot version: 20.7" and PUBLIC URL
