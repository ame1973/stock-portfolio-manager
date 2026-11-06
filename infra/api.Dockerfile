FROM ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 AS uv
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_NO_CACHE=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock readme.md LICENSE ./
COPY apps/api ./apps/api
COPY apps/worker ./apps/worker
COPY alembic.ini ./
COPY migrations ./migrations
ARG ENABLE_YFINANCE=false
RUN case "$ENABLE_YFINANCE" in \
        true) uv sync --frozen --no-dev --extra yahoo-sdk ;; \
        false) uv sync --frozen --no-dev ;; \
        *) printf 'ENABLE_YFINANCE must be true or false\n' >&2; exit 2 ;; \
    esac \
    && groupadd --gid 10001 spm \
    && useradd --uid 10001 --gid spm --no-create-home spm \
    && mkdir -p /data /backups \
    && chown 10001:10001 /data /backups \
    && chmod 700 /data /backups
USER 10001:10001
EXPOSE 8000
CMD ["sh", "-c", "umask 077; spm-manage migrate && exec uvicorn spm_api.main:app --host 0.0.0.0 --port 8000 --no-access-log"]
