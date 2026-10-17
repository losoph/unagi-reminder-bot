FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# Бот работает не от root: uid/gid совпадают с ops/docker/entrypoint.sh.
RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin bot

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# install -m: бит исполнения не зависит от прав файла в рабочей копии (Google Drive их теряет).
RUN install -m 0755 ops/docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /app/storage \
    && chown bot:bot /app/storage

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "main.py"]
