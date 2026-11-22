FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh

RUN pip install --no-cache-dir . \
    && chmod +x scripts/docker-entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
