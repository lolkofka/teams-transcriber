#!/bin/sh
# Точка входа контейнера: собирает аргументы из переменных окружения.
set -eu

THREADS="${THREADS:-0}"
if [ "$THREADS" = "0" ]; then
  THREADS="$(nproc)"
fi

if [ -z "${TOKEN:-}" ]; then
  echo "TOKEN не задан: укажи его в .env" >&2
  exit 2
fi

echo "[docker] model=${MODEL:-large-v3-turbo} device=${DEVICE:-cpu} threads=$THREADS max_beam=${MAX_BEAM:-5}"

exec python -X utf8 /app/processor_server.py \
  --model "${MODEL:-large-v3-turbo}" \
  --device "${DEVICE:-cpu}" \
  --threads "$THREADS" \
  --max-beam "${MAX_BEAM:-5}" \
  --host 0.0.0.0 \
  --port 8756 \
  --token "$TOKEN"
