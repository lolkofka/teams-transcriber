#!/usr/bin/env bash
# Установка внешнего обработчика на Linux-сервер. Запускать НА СЕРВЕРЕ, положив рядом processor_server.py:
#   bash install_processor.sh [МОДЕЛЬ] [ТОКЕН]
# Модель по умолчанию large-v3-turbo; токен, если не задан, генерируется и печатается в конце.
set -euo pipefail

MODEL="${1:-large-v3-turbo}"
DIR="$HOME/teams-processor"
mkdir -p "$DIR"

# скрипт мог быть запущен из другой папки — перенесём его рядом с собой
SRC="$(cd "$(dirname "$0")" && pwd)"
[ -f "$SRC/processor_server.py" ] && [ "$SRC" != "$DIR" ] && cp "$SRC/processor_server.py" "$DIR/"
cd "$DIR"

if [ ! -f processor_server.py ]; then
  echo "processor_server.py не найден в $DIR — скопируй его сюда (scp) и запусти скрипт снова." >&2
  exit 1
fi

if [ -n "${2:-}" ]; then printf '%s' "$2" > token.txt; fi
if [ ! -s token.txt ]; then python3 -c "import secrets,sys;sys.stdout.write(secrets.token_urlsafe(24))" > token.txt; fi
chmod 600 token.txt
TOKEN="$(cat token.txt)"

echo "== системные пакеты"
if command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv python3-pip
fi

echo "== виртуальное окружение (faster-whisper ~200 МБ)"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q faster-whisper numpy

DEVICE=cpu
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then DEVICE=cuda; fi
THREADS="$(nproc)"
echo "== устройство: $DEVICE, ядер: $THREADS, модель: $MODEL"

echo "== служба systemd (слушает только localhost, наружу порт не открывается)"
sudo tee /etc/systemd/system/teams-processor.service >/dev/null <<UNIT
[Unit]
Description=Teams transcriber audio processor
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/processor_server.py --model $MODEL --device $DEVICE --threads $THREADS --port 8756 --host 127.0.0.1 --token $TOKEN
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo chmod 600 /etc/systemd/system/teams-processor.service
sudo systemctl daemon-reload
sudo systemctl enable --now teams-processor
sleep 5
sudo systemctl --no-pager --lines=8 status teams-processor || true

echo
echo "== готово. Токен: $TOKEN"
echo "Первый запрос дольше обычного: модель скачивается (~1.6 ГБ)."
echo "С домашнего ПК подними туннель и укажи в приложении http://127.0.0.1:8756 :"
echo "  ssh -N -L 8756:127.0.0.1:8756 $USER@СЕРВЕР"
