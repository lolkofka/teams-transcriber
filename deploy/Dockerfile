# Внешний обработчик звука (faster-whisper) в контейнере.
#
# CPU:  docker compose up -d --build
# GPU:  в docker-compose.yml раскомментировать BASE nvidia/cuda, DEVICE=cuda и секцию deploy.resources
ARG BASE=python:3.11-slim
FROM ${BASE}

# в образе CUDA питона нет, в python:slim он уже есть
RUN if ! command -v python3 >/dev/null 2>&1; then \
      apt-get update && \
      apt-get install -y --no-install-recommends python3 python3-pip && \
      rm -rf /var/lib/apt/lists/*; \
    fi; \
    if ! command -v python >/dev/null 2>&1; then ln -s "$(command -v python3)" /usr/local/bin/python; fi

RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir faster-whisper numpy

WORKDIR /app
COPY processor_server.py /app/processor_server.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# модели кэшируются в томе, иначе 1.6 ГБ качались бы при каждом пересоздании контейнера
ENV HF_HOME=/models \
    MODEL=large-v3-turbo \
    DEVICE=cpu \
    THREADS=0 \
    MAX_BEAM=5 \
    TOKEN=""
VOLUME ["/models"]
EXPOSE 8756

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
  CMD python -c "import os,urllib.request as u;u.urlopen(u.Request('http://127.0.0.1:8756/health',headers={'X-Token':os.environ.get('TOKEN','')}),timeout=5)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
