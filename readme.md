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

For a different web port, set both `WEB_PORT` and `PUBLIC_ORIGIN`. When using the sample `.env` for Compose, change `PUBLIC_ORIGIN` to `http://localhost:8080` (the local Vite default is port 5173). Production requires `APP_ENV=production` and an HTTPS public origin behind a trusted reverse proxy; browser workflows and full production deployment instructions are still being implemented.

Before a schema change, the migration command checks integrity and retains a mode-0600 local SQLite rollback snapshot in the data directory. This snapshot is sensitive and **not** an encrypted disaster-recovery backup. Encrypted portable backups and safe full restore are later implementation stages.

## Owner authentication

The authentication API is implemented; the browser onboarding screen and portfolio master-data workflows are still under development.

After migration, explicitly issue a cryptographically random setup token from a trusted local terminal. It is valid for ten minutes, consumed once, and never returned by an HTTP endpoint or automatically written to service/container logs. To create or replace it locally:

```sh
uv run spm-manage setup-token
# Or, for the container deployment:
docker compose exec api spm-manage setup-token
```

`POST /api/v1/setup/initialize` accepts `setup_token` and a unique password/passphrase of 12–200 characters. Setup is permanently disabled once an owner exists. `POST /api/v1/auth/login` accepts the password; it does not return a bearer token. Both requests require an `Origin` header matching `PUBLIC_ORIGIN`.

Authentication uses revocable, HttpOnly, SameSite=Strict session cookies, with Secure required for an HTTPS public origin. Session maximum age is 30 days and idle expiry is seven days. Passwords use Argon2id (64 MiB, three iterations, parallelism four); concurrent hashing is bounded to two operations per API process. Persistent per-client and global attempt limits and increasing failure backoff apply before password verification.

Authenticated mutations require `X-CSRF-Token` (obtained from `/api/v1/auth/csrf`) and an `Idempotency-Key` of 16–200 characters. Session secrets and CSRF secrets are independently random and stored only as hashes. Password-change and reauthentication request fingerprints are keyed with the session secret; no fast unkeyed password-verification hash is retained. Never put credentials in a URL or browser storage.

- `/auth/me`, `/auth/sessions`: inspect the current owner and paginated sessions.
- `DELETE /auth/sessions/{id}`: revoke a session; `POST /auth/logout`: revoke the current session and clear its cookies.
- `POST /auth/reauth`: confirm the password for sensitive operations; confirmation is valid for five minutes.
- `POST /auth/change-password`: confirm the old password, set a new one, and revoke **all** existing sessions.

All paths above have the `/api/v1` prefix. Full generated request/response types are in `packages/contracts/`. Development-only interactive API documentation is at the API server's `/docs`; it is disabled in production.

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
