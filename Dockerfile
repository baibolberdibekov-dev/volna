FROM denoland/deno:bin-2.9.8 AS deno
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_NO_UPDATE_CHECK=1 \
    DENO_NO_PROMPT=1 \
    DENO_DIR=/tmp/deno-cache \
    DATA_DIR=/app/data \
    PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY app ./app
COPY static ./static
COPY run.py ./
RUN mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/api/health', timeout=3)"
CMD ["python", "run.py"]
