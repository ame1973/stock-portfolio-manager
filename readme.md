# Stock Portfolio Manager

A self-hosted portfolio ledger for Hong Kong and US securities, designed for a single owner with multiple brokerage accounts. Monetary values use exact decimal arithmetic and historical exchange rates, not floating-point estimates.

> **Development status:** authentication and master-data management are implemented. The financial ledger, providers, valuations, and encrypted offline/backup features are not implemented yet. This is not a complete portfolio application or a production release. No live provider coverage or target NAS validation is claimed.

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

For a different web port, set both `WEB_PORT` and `PUBLIC_ORIGIN`. When using the sample `.env` for Compose, change `PUBLIC_ORIGIN` to `http://localhost:8080` (the local Vite default is port 5173). Production requires `APP_ENV=production` and an HTTPS public origin behind a trusted reverse proxy; target NAS validation and full production deployment/recovery instructions are still pending.

Before a schema change, the migration command checks integrity and retains a mode-0600 local SQLite rollback snapshot in the data directory. This snapshot is sensitive and **not** an encrypted disaster-recovery backup. Encrypted portable backups and safe full restore are later implementation stages.

## Owner authentication

The authentication API and browser initialization/login/logout, master-data management, and password/session settings are implemented. The financial features remain under development.

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

## Master data API

Portfolios, brokers, accounts, securities, identifiers, and provider symbols have authenticated create/list/update workflows. Lists use bounded keyset pagination (`limit` 1–100, `cursor`) and literal-text search (`q`). Record updates use `If-Match: "<version>"` and return a new version/ETag. An outdated version returns 412; an idempotency key reused with different input returns 409.

- Reporting currency is **HKD only**. Trading/cash currencies are HKD/USD, with distinct exchange identities. `0005` stays a string, and `BRK.B` keeps its dot.
- Supported exchanges are listed at `/api/v1/exchanges`. No arbitrary unsupported exchange or currency is created automatically.
- Brokers/accounts are owner-scoped. Deletion endpoints archive them; dependent accounts, financial history, identifiers, and evidence are not hard-deleted.
- Exchange/security metadata is an installation-wide catalogue for the single-owner product. This is not a multi-tenant authorization model.
- New securities are unverified. Manual verification requires an explanation. Creating a security does not guess provider aliases, contact a provider, or promise live coverage.
- Identifier and provider-alias periods cannot overlap ambiguously. Audited period corrections use the nested mapping PATCH endpoints with `If-Match` and an explanation. Ticker validity defaults to the exchange-local creation date unless explicitly supplied; historical validity is not invented.

Account creation can optionally post an initial cash balance. Supply `opening_cash` as a decimal string and an explicit non-future `opening_cash_date`; USD also requires a locked `opening_fx_rate_hkd`. This creates one audited starting-balance adjustment atomically, **not** an external deposit. Completeness defaults to UNKNOWN and must be asserted separately after reconciliation. Opening cash metadata is never added a second time to ledger balances. Corrections to an existing starting balance use ledger adjustment workflows, not account metadata edits.

All monetary inputs reject floats, scientific notation, excessive fractional digits, and INT64 overflow. SQLite guards reject REAL values in scaled financial columns even on a direct SQL path. No input is silently rounded.

## Using the browser

The interface defaults to Traditional Chinese and follows the operating system's light/dark preference. After issuing a local setup token, open the web address, enter the token, choose a unique passphrase, and confirm it. Later visits use the normal login form. Passwords and setup tokens are never placed in browser storage.

1. Open `/settings/portfolios` and create a portfolio. Reporting currency is fixed to HKD. Leave cash completeness unknown until the entire relevant cash history has actually been reconciled.
2. Open `/settings/brokers` and create a broker. Dividend collection fees are exact decimal strings and affect future estimates only, never past receipts.
3. Open `/settings/accounts`, select the portfolio and an active broker, and choose HKD or USD. Reference lists support bounded name search; changing the search clears the previous selection so an invisible account association cannot be submitted.
4. Leave an unknown opening balance blank; blank is not zero. An explicit balance needs its effective date, and a USD balance also needs the historical HKD conversion rate. There is no default FX rate. Existing starting balances cannot be rewritten through this metadata form; ledger corrections will be provided in the financial stage.
5. Open `/settings/securities` to add an exchange-specific security. Preserve the actual ticker, including leading zeros and dots. Choose its quote currency and, when known, an explicit first identifier date. Manual verification needs an evidence explanation; it does not establish provider coverage.
6. Use a security's detail link to manage dated identifiers and provider symbols. Period corrections require a reason and the version read by the browser. Yahoo and yfinance belong to the same source family, not independent corroboration.
7. Open `/settings/security` to reauthenticate, change the password, or revoke a listed session. Changing the password revokes all sessions, including the current one, and returns to login.

Brokers and accounts can be archived and explicitly reopened after confirmation. Historical relationships are retained. Lists show the current page, not an all-record total. Failed saves retain input; a 412 means another writer changed the record, so cancel, reload, and reconcile before trying again. Unchanged retries retain their idempotency key. No save is reported as successful before the server confirms it.

A disconnected page may display already loaded in-memory records with a warning; refresh and saves need a connection. This is **not** encrypted offline/PWA support, and no writes are automatically queued. Logout, session expiry, and password change cancel private reads and clear private query/mutation state. Offline snapshots/drafts, transactions, valuations, dividends, providers, and portable backup/restore remain future stages.

## Trusted HTTPS proxy

For production, the NAS proxy must terminate TLS and **overwrite** `X-Forwarded-Proto` with `https` before forwarding to the loopback-only web port. Never publish the API port or attach untrusted containers to the application's private Compose network. Uvicorn trusts only the gateway's reserved internal IP (`WEB_INTERNAL_IP`, default `172.30.93.10`); Nginx overwrites `X-Forwarded-For` rather than accepting a caller-selected rate-limit address. `APP_SUBNET` defaults to `172.30.93.0/24`. Change both network settings together if they overlap a LAN/VPN/Docker network, or when running multiple instances concurrently. When a NAS proxy is present, the default login limit may therefore group devices behind that proxy into one bucket. The application also validates the exact public Origin and rejects insecure authentication requests.

## Checks

```sh
make check
```

Tests use disposable databases. The checked-in reference contract and golden fixtures do not depend on private specification files. The executable OpenAPI and generated TypeScript describe only implemented operations; CI verifies generation drift.

For browser onboarding tests, use a **fresh isolated** Compose project and loopback address; never point tests at a real installation. The test captures its one-time token directly into process memory and disables traces/videos so credentials are not printed in test output.

```sh
npx playwright install chromium
# Choose a new project name each run; existing named volumes are not silently erased.
WEB_PORT=18083 PUBLIC_ORIGIN=http://localhost:18083 \
  docker compose -p spm-e2e-local-001 up -d --build --wait
SPM_E2E_COMPOSE_PROJECT=spm-e2e-local-001 \
SPM_E2E_BASE_URL=http://localhost:18083 npm run test:e2e
docker compose -p spm-e2e-local-001 down
```

The serial browser scenarios verify one-time initialization, authenticated rendering at 360/768/1440px, light/dark views, exact opening-cash input with required historical FX, non-overwriting 412 conflicts, archive/reopen without orphaning linked accounts, exchange-specific leading-zero/dotted tickers, dated mappings, disconnected read warnings, reauthentication, session revocation, and password-change revoke-all. Modal keyboard focus, background scroll locking, and the unchanged strict CSP are exercised against the actual Nginx/API/worker deployment. Screenshots after token consumption are local artifacts under `.local/`, excluded from Git. Use a fresh test project for each run: these scenarios intentionally change the synthetic owner's password. Additional financial and encrypted-offline workflows will be implemented and tested in later stages.

## Data and privacy

- SQLite uses WAL, foreign keys, full synchronization, and versioned Alembic migrations.
- API responses are marked `Cache-Control: no-store`.
- `.env`, credentials, databases, backups, local development notes, and source specification files are excluded from Git.
- Market data licensing is separate from the application license. Missing credentials or unavailable data must never be represented as successful synchronization.
- Public health endpoints return status only. They do not expose account data or configuration.

## License

[MIT](LICENSE). This application records investments; it does not execute trades or provide investment or tax advice.

Deployment, recovery, and full user instructions will be expanded alongside their verified implementations. Do not deploy this development foundation with real financial data.
