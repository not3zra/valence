FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"
ENV UV_NO_CACHE=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh

COPY --from=uv /uv /uvx /bin/

RUN uv sync --frozen --no-dev \
    && chmod +x scripts/docker-entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["./scripts/docker-entrypoint.sh"]
