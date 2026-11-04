# Stock Portfolio Manager

A self-hosted portfolio ledger for Hong Kong and US securities, designed for a single owner with multiple brokerage accounts. Monetary values use exact decimal arithmetic and historical exchange rates, not floating-point estimates.

> **Development status:** foundation implementation in progress. This is not yet a complete portfolio application or a production release. No live provider coverage or target NAS validation is claimed.

## Local development

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 24 LTS, npm, and a local filesystem for SQLite (not NFS/SMB).

```sh
uv sync --locked
npm ci
cp .env.example .env
uv run spm-manage migrate
uv run uvicorn spm_api.main:app --host 127.0.0.1 --port 8000
# In another terminal:
npm run dev
```

The development web server uses `http://localhost:5173` and proxies `/api` to the local API. Configuration is read from the process environment, not automatically from `.env`; Docker Compose reads `.env`. No owner, default password, provider credentials, or financial records are seeded.

## Container development environment

The three-service topology can run the foundation on a local machine:

```sh
docker compose up -d --build --wait
docker compose ps
docker compose exec api spm-manage check
# Open http://localhost:8080
docker compose down
```

Only the web gateway is bound to loopback. API and worker run separately, share the local SQLite volume, and use non-root containers with read-only root filesystems. Base images are pinned by digest. Named `data` and `backups` volumes survive `down`; do not use `down --volumes` for data you need to retain. The worker currently provides leadership and heartbeat supervision; market synchronization is not implemented yet.

For a different web port, set both `WEB_PORT` and `PUBLIC_ORIGIN`. When using the sample `.env` for Compose, change `PUBLIC_ORIGIN` to `http://localhost:8080` (the local Vite default is port 5173). Production requires `APP_ENV=production` and an HTTPS public origin behind a trusted reverse proxy; authentication and full production deployment instructions are still being implemented.

Before a schema change, the migration command checks integrity and retains a mode-0600 local SQLite rollback snapshot in the data directory. This snapshot is sensitive and **not** an encrypted disaster-recovery backup. Encrypted portable backups and safe full restore are later implementation stages.

## Checks

```sh
make check
```

Tests use disposable databases. The checked-in reference contract and golden fixtures do not depend on private specification files. The executable OpenAPI and generated TypeScript describe only implemented operations; CI verifies generation drift.

## Data and privacy

- SQLite uses WAL, foreign keys, full synchronization, and versioned Alembic migrations.
- API responses are marked `Cache-Control: no-store`.
- `.env`, credentials, databases, backups, local development notes, and source specification files are excluded from Git.
- Market data licensing is separate from the application license. Missing credentials or unavailable data must never be represented as successful synchronization.
- Public health endpoints return status only. They do not expose account data or configuration.

## License

[MIT](LICENSE). This application records investments; it does not execute trades or provide investment or tax advice.

Deployment, recovery, and full user instructions will be expanded alongside their verified implementations. Do not deploy this development foundation with real financial data.
