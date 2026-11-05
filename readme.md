# Stock Portfolio Manager

A self-hosted portfolio ledger for Hong Kong and US securities, designed for a single owner with multiple brokerage accounts. Monetary values use exact decimal arithmetic and historical exchange rates, not floating-point estimates.

> **Development status:** authentication, master-data management, trade/cash/expense APIs, and native-wallet balance reads are implemented. Financial forms, transfers/actions, providers, valuations, and encrypted offline/backup features remain under development. This is not a complete portfolio application or a production release. No live provider coverage or target NAS validation is claimed.

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

Before a schema change, the migration command checks integrity and retains a mode-0600 local SQLite rollback snapshot in the data directory. Existing ledger values are preserved; invalid ledger dates, ratios, revision lineage, or mismatched cash reversals cause an atomic refusal rather than an invented repair. Trade, cash, expense, security-transfer, and corporate-action facts cannot be rewritten or deleted directly: corrections retain history, and cash reversals link back to and exactly offset their originals. A cash posting must fit both INT64 signs so its reversal remains representable. This snapshot is sensitive and **not** an encrypted disaster-recovery backup. Encrypted portable backups and safe full restore are later implementation stages.

## Owner authentication

The authentication API and browser initialization/login/logout, master-data management, and password/session settings are implemented. Trade, manual cash, and operating-expense recording are currently available through the API only; financial browser forms remain under development.

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

Account creation can optionally post an initial cash balance. Supply `opening_cash` as a decimal string and an explicit non-future `opening_cash_date`; USD also requires a locked `opening_fx_rate_hkd`. This creates one audited **start-of-day** balance adjustment atomically, **not** an external deposit. Completeness defaults to UNKNOWN and must be asserted separately after reconciliation. Opening cash metadata is never added a second time to ledger balances. Backdated trade, cash, or expense changes before that date append separately audited adjustments using the original opening FX, preserving the declared native opening balance without rewriting any existing fact. Only a nonzero net change to the declared currency before the opening date triggers this rebase; a note-only or FX-only revision does not silently repair an unrelated opening problem. Ordinary trades on the opening date or later still change cash normally; an opening balance in one currency does not fund another. Direct opening-balance correction workflows are not yet available, and account metadata edits cannot overwrite the assertion.

All monetary inputs reject floats, scientific notation, excessive fractional digits, and INT64 overflow. SQLite guards reject REAL values in scaled financial columns even on a direct SQL path. No input is silently rounded.

## Accounting behavior

The internal accounting modules use immutable Decimal values and deterministic account/security replay. They implement moving-average cost, exact transfers, rational splits/reverse splits with fractional-residue review, locked historical HKD amounts, and strict ex-date cutoffs. Derived money is rounded HALF_UP only at persistence boundaries; inputs are never rounded into validity. Computed HKD/USD settlement defaults to cents, while explicit broker actuals retain their exact input precision and require an evidence explanation. Buy/sell fees are included once in cost/proceeds, not automatically posted again as operating expenses.

Pure dividend arithmetic keeps one current accrual per stable event/account, preserves actual partial receipts, rejects duplicate counting, and flags mixed-currency reconciliation rather than subtracting unlike amounts. Missing quotes/FX and incomplete cash return unavailable metrics with reasons, not fabricated zeros. The unmodified financial fixtures and boundary tests exercise this library, including revision/receipt income separation.

## Trade ledger API

Trade create/read/list/revise/void operations are available at `/api/v1/trades`. They require the authentication/CSRF/idempotency headers described above. This records financial history; it never submits an order to a broker. Financial browser forms and the remaining cash-transfer/security-transfer/corporate-action workflows are not available yet.

A synthetic `POST /api/v1/trades` body is shown below. Replace the IDs, dates, amounts, and rate with explicitly reviewed evidence; the sample FX is **not** an application default or a live quote.

```json
{
  "portfolio_id": "00000000-0000-4000-8000-000000000001",
  "account_id": "00000000-0000-4000-8000-000000000002",
  "security_id": "00000000-0000-4000-8000-000000000003",
  "side": "BUY",
  "trade_date": "2026-01-02",
  "quantity": "10.00000000",
  "price": "100.00000000",
  "fee": "1.000000",
  "tax": "0.000000",
  "currency": "USD",
  "fx_rate_hkd": "7.812345678901",
  "fx_source": "MANUAL",
  "note": "Synthetic documentation example"
}
```

- Account and portfolio must belong to the owner and match each other. The account must be active for a new/revised trade. Price, fee, tax, and settlement use the security's quote currency, regardless of the account's default currency. No currency conversion or cash funding is invented.
- `executed_at_utc`, when supplied, must be an aware RFC3339 timestamp whose exchange-local date matches `trade_date`. Unknown times remain unknown and sort before known times; the service never invents a time to make an oversell pass.
- Current/past USD trades need explicit historical FX before posting. HKD uses identity 1. Owner-entered `fx_source` labels remain manual provenance, not provider verification. Optional `actual_settlement` and `actual_hkd` retain exact six-place actuals and require an `override_reason` of at least ten characters after trimming. Actual HKD does not change the native cash currency.
- A future exchange-local trade date creates `DRAFT_SCHEDULED`: no current cash posting or holding effect, and missing USD FX stays null. Time passing or an idempotent retry does not activate it. Explicitly revise the draft after its date and provide the historical FX; overselling is rechecked on activation.
- `PATCH /trades/{id}` takes the **complete create payload plus `reason`**, with `If-Match: "<version>"`. It returns a new immutable row ID and incremented `revision`, retaining `root_id` and `created_sequence`; the old row becomes SUPERSEDED. Use the returned ID/version for later operations. Revisions never overwrite the original amount or FX.
- `DELETE /trades/{id}` takes a JSON body `{"reason":"A specific correction explanation"}` and `If-Match`. It marks the fact VOID without deleting it. Archived accounts can still have mistaken trades voided. An active settlement is reversed exactly once in its original native amount, booked HKD, currency, date, and locked FX.
- A correction that would orphan a later sale/transfer is rejected with 409 `INSUFFICIENT_SHARES`. Unallocated held corporate actions or fractional split residues require review. The entire affected history is replayed before commit; financial facts, reversals, opening adjustments, audit, idempotency response, invalidation, and data revision commit or roll back together.
- List filters include `portfolio_id`, `account_id`, `security_id`, `from_date`, `to_date`, `root_id`, `include_history`, and literal note/ticker search `q`. Pagination is bounded to 1–100 and ordered by descending trade date/ID. SUPERSEDED history is excluded unless requested; VOID and scheduled rows remain visible. `GET /trades/{id}` returns the current control state and ETag of that specific historical row.

An unchanged retry returns its original idempotent result without posting again; reread the record for its current state. Different input needs a new key. A stale version returns 412 without changing facts. Decimal input, conversion, aggregate position, or reversal overflow is rejected, never truncated. Passing these development tests does not make an installation suitable for real financial records.

## Cash and operating-expense APIs

`/api/v1/cash-entries` and `/api/v1/expenses` support authenticated list/create/read/full-revise/void operations. POST returns 201; individual reads and writes return the physical row's ETag. Revisions require the complete create body plus a trimmed `reason` of at least ten characters and `If-Match`; DELETE requires the same reason in a JSON body. Use the returned new ID/version after a revision, and retain the idempotency key for an unchanged retry.

A synthetic native deposit request is shown below. Substitute reviewed account/date/amount evidence; a missing USD rate is deliberately unknown, not a default rate.

```json
{
  "account_id": "00000000-0000-4000-8000-000000000002",
  "effective_date": "2026-01-02",
  "kind": "DEPOSIT",
  "amount": "100.000001",
  "currency": "USD",
  "fx_rate_hkd": null,
  "note": "Synthetic documentation example"
}
```

- A DEPOSIT is positive and external; a WITHDRAW is negative and external. An ADJUSTMENT is signed, nonzero, non-external, and requires `reason`. If supplied, `external_flow` must agree with this classification. Single-leg TRANSFER_IN/TRANSFER_OUT requests are rejected; paired transfer support is still pending.
- Cash amounts are actual native amounts, retained exactly to six places without silently rounding to cents. HKD uses identity FX 1. USD may retain null FX/booked HKD with `quality=MISSING_FX`; native receipt/charge evidence is still real even when its HKD recognition is not yet known. Explicitly revise to adopt a historical rate rather than rewriting the original posting. An `actual_hkd` override requires both historical FX and an evidence reason and does not change the native wallet currency.
- Cash/expense dates record actual movements and cannot be in the future. An account must be active for a new/revised record; correcting a mistaken record by voiding remains available after archive.
- Cash correction appends the original's exact reversal and a new primary journal row. `source_type=CASH_CORRECTION` and its immutable `source_id` identify the predecessor; `supersedes_id`/`superseded_by_id` expose that link. SQL permits one same-owner successor only after the manual predecessor has been reversed. Old dates, currency, amounts, FX and provenance remain intact.
- Trade, expense, receipt, transfer, opening and derived rebase cash is source-managed and cannot be edited/voided through the generic cash endpoint (`SOURCE_MANAGED`). Use its owning workflow. The journal list intentionally includes original, reversed and reversal rows, since native balances sum the complete history.
- An expense body uses `account_id`, `date`, `category`, `amount`, `currency`, optional historical `fx_rate_hkd`/`actual_hkd`, `reason`, and `note`. Positive amounts are charges; negative amounts are explicit refunds and require a reason. Zero expenses are rejected. Each expense posts its one signed native cash counterpart; revision/void retains its original fact and exact cash reversal. **Do not post trade fees again here:** they are already included in trade cost/proceeds.
- Cash and expense lists use descending effective date/ID, bounded cursors, literal `q` search, owner-scoped account/portfolio filters, currency and date filters. Expenses exclude SUPERSEDED rows unless `include_history=true`; cash keeps all journal effects. Cash also filters `kind`, `source_type` and `entry_role`.

### Native wallet balances, not an invented HKD valuation

`GET /api/v1/accounts/{id}/cash-balances?as_of=YYYY-MM-DD` returns separate observed/default HKD/USD wallets. This is the corrected effective-date view, not a reconstruction of what was known at an earlier audit timestamp. The optional date cannot be in the future.

- `recorded_balance` is the exact sum of recorded native movements, including reversals. It is not automatically the true cash balance.
- `balance` is supplied only when the owner explicitly asserted COMPLETE history, the requested period is not before the dated opening, and the opening provenance/assertion is intact. Otherwise it is null with `balance_quality` UNKNOWN, PARTIAL, BEFORE_OPENING or NEEDS_REVIEW. Merely recording a movement never declares completeness.
- `historical_booked_hkd_net` is an audit net of historical booking snapshots, **not live valuation of remaining foreign cash**. Missing unreversed bookings produce null/MISSING_FX. An exactly reversed unknown-FX pair cancels and does not permanently contaminate the current net.
- Unlike currencies are never added together, no current FX is guessed, and no aggregate HKD/NAV/TWR value is fabricated. Computed total overflow is rejected rather than clipped; a read total can represent signed INT64_MIN even though an individual reversible posting cannot.

These APIs share short atomic posting/reversal/audit/idempotency/invalidation transactions. Schema migration 0005 adds manual sign/flow, same-owner correction and unique-successor guards plus a source lookup index without changing existing financial values. Invalid existing correction graphs stop the upgrade atomically. Financial forms, paired transfers, corporate actions, valuations and direct correction of an opening assertion remain unfinished.

## Using the browser

The interface defaults to Traditional Chinese and follows the operating system's light/dark preference. After issuing a local setup token, open the web address, enter the token, choose a unique passphrase, and confirm it. Later visits use the normal login form. Passwords and setup tokens are never placed in browser storage.

1. Open `/settings/portfolios` and create a portfolio. Reporting currency is fixed to HKD. Leave cash completeness unknown until the entire relevant cash history has actually been reconciled.
2. Open `/settings/brokers` and create a broker. Dividend collection fees are exact decimal strings and affect future estimates only, never past receipts.
3. Open `/settings/accounts`, select the portfolio and an active broker, and choose HKD or USD. Reference lists support bounded name search; changing the search clears the previous selection so an invisible account association cannot be submitted.
4. Leave an unknown opening balance blank; blank is not zero. An explicit balance needs its effective date, and a USD balance also needs the historical HKD conversion rate. There is no default FX rate. Existing starting balances cannot be rewritten through this metadata form; direct correction of the opening assertion is still pending. Manual cash adjustments and expense corrections are API-only for now.
5. Open `/settings/securities` to add an exchange-specific security. Preserve the actual ticker, including leading zeros and dots. Choose its quote currency and, when known, an explicit first identifier date. Manual verification needs an evidence explanation; it does not establish provider coverage.
6. Use a security's detail link to manage dated identifiers and provider symbols. Period corrections require a reason and the version read by the browser. Yahoo and yfinance belong to the same source family, not independent corroboration.
7. Open `/settings/security` to reauthenticate, change the password, or revoke a listed session. Changing the password revokes all sessions, including the current one, and returns to login.

Brokers and accounts can be archived and explicitly reopened after confirmation. Historical relationships are retained. Lists show the current page, not an all-record total. Failed saves retain input; a 412 means another writer changed the record, so cancel, reload, and reconcile before trying again. Unchanged retries retain their idempotency key. No save is reported as successful before the server confirms it.

A disconnected page may display already loaded in-memory records with a warning; refresh and saves need a connection. This is **not** encrypted offline/PWA support, and no writes are automatically queued. Logout, session expiry, and password change cancel private reads and clear private query/mutation state. Financial browser forms, offline snapshots/drafts, valuations, dividends, providers, and portable backup/restore remain future stages.

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

The serial browser scenarios verify one-time initialization, authenticated rendering at 360/768/1440px, light/dark views, exact opening-cash input with required historical FX, non-overwriting 412 conflicts, archive/reopen without orphaning linked accounts, exchange-specific leading-zero/dotted tickers, dated mappings, disconnected read warnings, reauthentication, session revocation, and password-change revoke-all. Modal keyboard focus, background scroll locking, and the unchanged strict CSP are exercised against the actual Nginx/API/worker deployment. A third scenario uses authenticated **real HTTP requests**, not unavailable financial forms, to verify trade posting/retry, oversell rejection, stale versions, immutable revision/FX preservation, dependent-sale rollback, exact void/retry, opening preservation, and an unposted future draft. A fourth verifies missing-FX native deposits, immutable cash corrections, operating expenses, a separate micro-HKD wallet, managed-source protection, and exact reversal/retry without rewriting opening cash. Screenshots after token consumption are local artifacts under `.local/`, excluded from Git. Use a fresh test project for each run: these scenarios intentionally change the synthetic owner's password. Remaining financial forms/workflows and encrypted-offline behavior will be implemented and tested in later stages.

## Data and privacy

- SQLite uses WAL, foreign keys, full synchronization, and versioned Alembic migrations.
- API responses are marked `Cache-Control: no-store`.
- `.env`, credentials, databases, backups, local development notes, and source specification files are excluded from Git.
- Market data licensing is separate from the application license. Missing credentials or unavailable data must never be represented as successful synchronization.
- Public health endpoints return status only. They do not expose account data or configuration.

## License

[MIT](LICENSE). This application records investments; it does not execute trades or provide investment or tax advice.

Deployment, recovery, and full user instructions will be expanded alongside their verified implementations. Do not deploy this development foundation with real financial data.
