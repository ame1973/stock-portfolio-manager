# Stock Portfolio Manager

A self-hosted portfolio ledger for Hong Kong and US securities, designed for a single owner with multiple brokerage accounts. Monetary values use exact decimal arithmetic and historical exchange rates, not floating-point estimates.

> **Development status:** authentication, master-data management, trade/cash/expense/transfer APIs, corporate-action terms, opening-cash corrections, native-wallet balance reads, source-only job enqueue/read/cancel, provider diagnostics, and exact observation read APIs are implemented. Responsive financial browser workflows include server-paginated desktop tables, versioned side-panel/full-screen editors, and accessible navigation. The source workspace at `/data-center` provides local/recorded diagnostics, exact observations, explicit source-only queueing, job progress/cancellation and owned schedules. Reauthenticated provider policies, private credential import, explicit owner-scoped runtime selection and responsive settings at `/settings/providers` are implemented; imports remain disabled until separately enabled. Protected cited-source creation, responsive quotation forms and owner-scoped split-basis declaration APIs are implemented; canonical review/dividends, valuations, and encrypted offline/backup features remain under development. Unallocated corporate actions remain explicit reviews, not successful valuations. This is not a complete portfolio application or a production release. No live provider coverage or target NAS validation is claimed.

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

Only the web gateway is bound to loopback. API and worker run separately, share the local SQLite volume, and use non-root containers with read-only root filesystems. Base images are pinned by digest. Named `data`, `backups`, `provider_secrets` and `managed_provider_secrets` volumes survive `down`; do not use `down --volumes` for data or credentials you need to retain. The worker provides single-leader supervision, independent heartbeat/scheduling and quota-enforced source-only dispatch for explicit owner jobs/schedules; startup creates no default schedule or provider grant. Local-week-pattern scheduling is not a verified trading calendar.

Operator credentials stay read-only in both services. The separate [managed credential volume](docs/providers/CONFIGURATION.md#separate-managed-credential-storage) is initially empty and owner0700, writable only by API and read-only to worker. The [reauthenticated import API](docs/providers/CREDENTIALS.md) can now prepare private immutable editions and bind disabled owned definitions; **owned runtime selection and responsive settings UI are installed**. Local managed storage requires explicit `MANAGED_PROVIDER_SECRET_DIR` outside operator/data/backup trees; no local directory or key is automatically created. Credential volumes are separate from SQLite backups.

For a different web port, set both `WEB_PORT` and `PUBLIC_ORIGIN`. When using the sample `.env` for Compose, change `PUBLIC_ORIGIN` to `http://localhost:8080` (the local Vite default is port 5173). Production requires `APP_ENV=production` and an HTTPS public origin behind a trusted reverse proxy; target NAS validation and full production deployment/recovery instructions are still pending.

Before a schema change, the migration command checks integrity and retains a mode-0600 local SQLite rollback snapshot in the data directory. Existing ledger values are preserved; invalid ledger dates, ratios, revision lineage, or mismatched cash reversals cause an atomic refusal rather than an invented repair. Trade, cash, expense, security-transfer, and corporate-action facts cannot be rewritten or deleted directly: corrections retain history, and cash reversals link back to and exactly offset their originals. A cash posting must fit both INT64 signs so its reversal remains representable. This snapshot is sensitive and **not** an encrypted disaster-recovery backup. Encrypted portable backups and safe full restore are later implementation stages.

## Owner authentication

The authentication API and browser initialization/login/logout, master-data management, password/session settings, trade/cash/expense/transfer recording, corporate-action terms, native balances and opening-assertion corrections are implemented. Adopting action terms does not imply complete allocation or valuation; provider/valuation/offline/recovery features are still development work.

After migration, explicitly issue a cryptographically random setup token from a trusted local terminal. It is valid for ten minutes, consumed once, and never returned by an HTTP endpoint or automatically written to service/container logs. To create or replace it locally:

```sh
uv run spm-manage setup-token
# Or, for the container deployment:
docker compose exec api spm-manage setup-token
```

`POST /api/v1/setup/initialize` accepts `setup_token` and a unique password/passphrase of 12–200 characters. Setup is permanently disabled once an owner exists. `POST /api/v1/auth/login` accepts the password; it does not return a bearer token. Both requests require an `Origin` header matching `PUBLIC_ORIGIN`.

Authentication uses revocable, HttpOnly, SameSite=Strict session cookies, with Secure required for an HTTPS public origin. Session maximum age is 30 days and idle expiry is seven days. Passwords use Argon2id (64 MiB, three iterations, parallelism four); concurrent hashing is bounded to two operations per API process. Persistent per-client and global attempt limits and increasing failure backoff apply before password verification.

Authenticated mutations require `X-CSRF-Token` (obtained from `/api/v1/auth/csrf`) and an `Idempotency-Key` of 16–200 characters. Session secrets and CSRF secrets are independently random and stored only as hashes. Password-change, reauthentication and credential-import request fingerprints are keyed with the session secret; no fast unkeyed password/key-verification hash is retained. Never put credentials in a URL or browser storage.

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

Account creation can optionally post an initial cash balance. Supply `opening_cash` as a decimal string and an explicit non-future `opening_cash_date`; USD also requires a locked `opening_fx_rate_hkd`. This creates one audited **start-of-day** balance adjustment atomically, **not** an external deposit. Completeness defaults to UNKNOWN and must be asserted separately after reconciliation. Opening cash metadata is never added a second time to ledger balances. Backdated trade, cash, or expense changes before that date append separately audited adjustments using the original opening FX, preserving the declared native opening balance without rewriting any existing fact. Only a nonzero net change to the declared currency before the opening date triggers this rebase; a note-only or FX-only revision does not silently repair an unrelated opening problem. Ordinary trades on the opening date or later still change cash normally; an opening balance in one currency does not fund another. Direct opening assertions are corrected through the dedicated audited API below, not account metadata edits.

All monetary inputs reject floats, scientific notation, excessive fractional digits, and INT64 overflow. SQLite guards reject REAL values in scaled financial columns even on a direct SQL path. No input is silently rounded.

## Accounting behavior

The internal accounting modules use immutable Decimal values and deterministic account/security replay. They implement moving-average cost, exact transfers, rational splits/reverse splits with fractional-residue review, locked historical HKD amounts, and strict ex-date cutoffs. Derived money is rounded HALF_UP only at persistence boundaries; inputs are never rounded into validity. Computed HKD/USD settlement defaults to cents, while explicit broker actuals retain their exact input precision and require an evidence explanation. Buy/sell fees are included once in cost/proceeds, not automatically posted again as operating expenses.

Pure dividend arithmetic keeps one current accrual per stable event/account, preserves actual partial receipts, rejects duplicate counting, and flags mixed-currency reconciliation rather than subtracting unlike amounts. Missing quotes/FX and incomplete cash return unavailable metrics with reasons, not fabricated zeros. The unmodified financial fixtures and boundary tests exercise this library, including revision/receipt income separation.

## Trade ledger API

Trade create/read/list/revise/void operations are available at `/api/v1/trades`, with corresponding browser forms at `/transactions`. They require the authentication/CSRF/idempotency headers described above. This records financial history; it never submits an order to a broker.

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

- A DEPOSIT is positive and external; a WITHDRAW is negative and external. An ADJUSTMENT is signed, nonzero, non-external, and requires `reason`. If supplied, `external_flow` must agree with this classification. Single-leg TRANSFER_IN/TRANSFER_OUT requests are rejected; use the paired `/cash-transfers` workflow instead.
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

These APIs share short atomic posting/reversal/audit/idempotency/invalidation transactions. Schema migration 0005 adds manual sign/flow, same-owner correction and unique-successor guards plus a source lookup index without changing existing financial values. Invalid existing correction graphs stop the upgrade atomically. Responsive cash/expense browser workflows are available at `/cash` and `/cash/expenses`; live valuations remain unfinished.

## Paired cash and securities transfers

Both `/api/v1/cash-transfers` and `/api/v1/security-transfers` support authenticated list/create/read/full-revise/void operations. Use CSRF/idempotency headers, quoted If-Match for changes, and a reason body for void. PATCH returns a new physical ID/revision while preserving `root_id`; unchanged retries do not post again. A stale version returns 412 without overwriting facts. List filters include portfolio/account/root, effective dates, literal note search, bounded descending date/ID cursors and optional history.

### Paired native cash movement

Cash transfers currently operate **within one portfolio**. The source specification does not define cross-portfolio cash-conversion/performance attribution, so that case is explicitly rejected rather than guessing external-flow recognition. Both accounts must be owned and active for new/revised records. Voiding an error remains available after archive. Each fact posts one debit and one credit in the same transaction, both non-external, using source-managed `CASH_TRANSFER_OUT` / `CASH_TRANSFER_IN` journal entries.

A synthetic currency-conversion body is shown below. The amounts/rate are examples, not defaults or provider quotes.

```json
{
  "portfolio_id": "00000000-0000-4000-8000-000000000001",
  "source_account_id": "00000000-0000-4000-8000-000000000002",
  "target_account_id": "00000000-0000-4000-8000-000000000004",
  "transfer_date": "2026-01-03",
  "source_currency": "USD",
  "target_currency": "HKD",
  "source_amount": "100.000000",
  "target_amount": "780.000000",
  "fx_rate_hkd": "7.800000000000",
  "reason": "Synthetic reviewed conversion example"
}
```

- Amounts are positive actual **principal**, excluding separately recorded fees. Same-currency principal must match exactly; record a genuine charge/refund through the expense workflow rather than hiding it in an unbalanced transfer. No fee is silently invented or debited twice.
- Same-account movement is allowed only for an explicit change of currency; transferring a wallet to itself is rejected. Distinct OUT/IN source identities keep both legs unique even on one account.
- `fx_rate_hkd` is the locked USD-to-HKD snapshot, shared when both wallets are USD. HKD remains identity 1. Missing USD FX stays null/MISSING_FX even when both actual native amounts are known; no exchange rate is inferred from their ratio.
- Optional positive `actual_hkd` applies to the USD leg's booking and still requires its historical FX. The operation always requires an evidence `reason` of at least ten trimmed characters. It does not implicitly fund a trade or change the recorded native amounts.
- `source_booked_hkd`, `target_booked_hkd`, and `booking_delta_hkd` are historical leg snapshots, **not current valuation or realized FX P&L**. A nonzero known delta is NEEDS_REVIEW; missing bookings remain MISSING_FX. Native movement is retained honestly while later valuation/reconciliation determines economic attribution.
- Revision reverses both originals exactly and posts both replacements atomically. Void appends both exact reversals. A second-leg, rebase, serialization or constraint failure rolls back the entire fact, cash, audit, idempotency and invalidation change. Relevant pre-opening effects preserve both dated native opening assertions using each account's original FX.

### Cost-preserving securities movement

A security-transfer create body contains `portfolio_id`, `source_account_id`, `target_account_id`, `security_id`, `transfer_date`, positive decimal-string `quantity`, and optional `note`. Both accounts must belong to the same selected portfolio and must differ. Cash/FX conversion is not part of a securities transfer, and it never posts native cash or realizes profit by itself.

Cost is proportional to the source account's independently replayed moving average **immediately before the transfer event**, not the end-of-day position. Unknown execution time is not invented to borrow shares from a later known-time buy. Confirmed opening splits apply first; unallocated held actions or fractional residues require review. A future date is rejected using the security's exchange-local calendar.

- `cost_moved_hkd` with `cost_scope=BOOKED_AT_CREATION` is the immutable six-place creation snapshot. Authoritative replay retains unrounded proportional cost; prior audited trade corrections may restate current source/destination basis without rewriting this historical evidence. It is not a claim about the latest projection's allocation.
- Revisions require the complete create body plus `reason`, retain the root and `created_sequence`, and select only the new current row. Old quantity/cost/date remain intact. The sequence cannot move just because a new revision ID was created.
- Removing/reducing a transfer that would orphan a destination sale or a dependent transfer returns 409 INSUFFICIENT_SHARES and changes nothing. Full-history validation protects both old/new affected portfolios during an owner-authorized correction.
- Native cash, financial facts, exact pair reversals, audit/idempotency, projections' invalidation and opening rebases commit in one short UoW where applicable. Generic cash editing cannot modify transfer legs. Migration 0006 preserves old security-transfer fields/order and adds guarded revision selection plus an immutable paired-cash fact table.

Browser recording/correction/void workflows are available at `/cash/transfers` and `/transfers`. Complete cash-FX performance attribution, unallocated-action workflows and production acceptance remain unfinished.

## Opening-cash assertion API

`GET /api/v1/accounts/{id}/opening-cash` reads the current dated assertion, historical FX/booked HKD, provenance entry/predecessor, completeness and quality. The corresponding browser workflow is `/cash/accounts/{id}`. Its ETag is the **account version**, so an intervening account metadata edit also invalidates a correction's If-Match. Account ownership and active status are required for a correction; archived records remain readable.

`PATCH /api/v1/accounts/{id}/opening-cash` requires CSRF/idempotency, quoted account If-Match and a trimmed reason of at least ten characters. A synthetic replacement body is:

```json
{
  "opening_cash": "1500.000000",
  "opening_cash_date": "2026-01-15",
  "opening_fx_rate_hkd": "7.900000000000",
  "reason": "Synthetic reconciled starting statement"
}
```

- A supplied amount is nonnegative, exact to six places, and needs an explicit non-future date. USD requires explicit historical FX even for known zero; HKD uses identity 1. Neither amounts nor missing FX are inferred from subsequent cash.
- A replacement appends exact original-date/native/HKD/FX reversals of the current assertion and its active derived opening adjustments. It appends the new `OPENING_BALANCE_CORRECTION`, linked to its retired predecessor, then recalculates any required pre-opening adjustment at the **new assertion's FX**. Ordinary trade/cash/expense/transfer facts are not rewritten. Later backdated movements use that current assertion's FX, not a retired opening rate.
- Supply `{"opening_cash":null,"reason":"Withdraw an unsupported starting assertion"}` to clear it. Do not supply a date/rate with null. Clearing reverses the assertion and derived adjustments, retains ordinary recorded movements, and resets cash completeness to UNKNOWN. It does not claim a true zero balance.
- Reintroducing an explicit `"0"` creates a known zero assertion, but does not silently restore COMPLETE. Reconcile and explicitly update the account's completeness before treating recorded cash as the actual balance.
- UNSET, MISSING_FX and NEEDS_REVIEW are distinct from OK. Missing or retired single-chain provenance may be replaced only by this new explicit owner assertion; ambiguous chains are rejected rather than guessed. The generic cash API cannot edit any opening-family entry.
- Metadata, exact reversals, replacement/rebase, audit, idempotency response and invalidation commit together. Conversion, derived-rebase, serialization or CAS failure restores every old value/version. Historical booked HKD remains audit evidence, not current foreign-cash valuation.

## Corporate-action terms API

`/api/v1/corporate-actions` supports authenticated list/create/read/full-revise/void; corresponding browser workflows are at `/corporate-actions`. Actions belong to the installation-wide security catalogue, not a separate multi-tenant data model. POST requires a reason; PATCH takes the full create body with a reason and quoted If-Match; DELETE takes a reason body and quoted If-Match. Root/sequence stay stable across immutable physical revisions. Lists support security/root/type/status/date/history filters, escaped literal note/reason/symbol search, and bounded descending date/ID cursors.

A synthetic three-for-two adoption is:

```json
{
  "security_id": "00000000-0000-4000-8000-000000000003",
  "action_type": "SPLIT",
  "effective_date": "2026-01-03",
  "ratio_numerator": "3",
  "ratio_denominator": "2",
  "status": "CONFIRMED",
  "reason": "Synthetic reviewed rational split terms"
}
```

- Ratio fields accept exact positive INT64 digit strings (recommended) or legacy strict JSON integers; responses always use canonical strings/null. Never construct a large ratio through JavaScript Number: adjacent integers above 2^53 would lose precision. Valid inputs normalize to Python/SQLite integers, retaining the existing integer idempotency fingerprints and historical facts; old numeric response caches can still replay into the exact string output.
- SPLIT needs positive integer terms with numerator greater than denominator; REVERSE_SPLIT requires the opposite. No decimal/float ratios or guessed 1:1 defaults are accepted. Confirmed terms apply at the effective day's opening, preserving total cost. Non-representable fractional shares require review, never truncation or invented cash-in-lieu. Future confirmed quantity terms do not affect today's holdings; each security's exchange-local calendar determines their horizon.
- `status=CONFIRMED` adopts terms, **not a complete valuation**. The response states `allocation_scope=TERMS_NOT_A_VALUATION` and an explicit `effect_scope`. Unallocated later history may remain review-required while a known earlier action is resolved. The computable prefix must still validate; no partial positions are published as a complete current result. Normal ledger writes cannot bypass a held unallocated action.
- CASH_IN_LIEU, MERGER and SPIN_OFF without an implemented allocation workflow must remain NEEDS_REVIEW. Saving their evidence creates a real review item; it does not invent shares, cash, cost allocation or realized profit. Such allocation remains unsupported, not silently successful.
- SYMBOL_CHANGE requires explicit `new_symbol` and no share ratio. Confirmation changes **only the current display ticker**, keeps `security_id` and all financial history, and captures `previous_symbol`. Historical identifiers and provider aliases are never guessed or rewritten; an identity-mapping review remains necessary. Keep future display changes in NEEDS_REVIEW and explicitly confirm after their exchange-local effective date. Undo/correction refuses to overwrite later adopted display changes or an independently changed current ticker. Independent name/status edits remain intact.
- `evidence_url`, when supplied, must be HTTP(S) without credentials or whitespace; it is stored as evidence and **never fetched inside the mutation**. User-entered terms are not provider verification.
- Review replacement/resolution, affected-portfolio replay/invalidation, symbol display changes, facts, audit and idempotency are atomic. A revision/void that would orphan a later sale or transfer is rejected without writes. Only current non-void action revisions participate in replay; old terms remain readable with their lifecycle status.

Migration 0007 adds strict action identities/lineage/terms and unique reversed-parent opening-correction guards without rewriting old financial values. Existing legacy symbol actions do not acquire an invented previous ticker. Duplicate retained current action identities stop the upgrade atomically for explicit review. Downgrade remains restore-from-validated-backup only.

## Using the browser

The interface defaults to Traditional Chinese and follows the operating system's light/dark preference. After issuing a local setup token, open the web address, enter the token, choose a unique passphrase, and confirm it. Later visits use the normal login form. Passwords and setup tokens are never placed in browser storage.

1. Open `/settings/portfolios` and create a portfolio. Reporting currency is fixed to HKD. Leave cash completeness unknown until the entire relevant cash history has actually been reconciled.
2. Open `/settings/brokers` and create a broker. Dividend collection fees are exact decimal strings and affect future estimates only, never past receipts.
3. Open `/settings/accounts`, select the portfolio and an active broker, and choose HKD or USD. Reference lists support bounded name search; changing the search clears the previous selection so an invisible account association cannot be submitted.
4. Leave an unknown opening balance blank; blank is not zero. An explicit balance needs its effective date, and a USD balance also needs the historical HKD conversion rate. There is no default FX rate. Existing starting balances cannot be rewritten through this metadata form; open the account's cash/opening link or `/cash/accounts/{id}` for the dedicated correction workflow. Use the dedicated corporate-action page to adopt terms rather than overwriting security identity metadata.
5. Open `/settings/securities` to add an exchange-specific security. Preserve the actual ticker, including leading zeros and dots. Choose its quote currency and, when known, an explicit first identifier date. Manual verification needs an evidence explanation; it does not establish provider coverage.
6. Use a security's detail link to manage dated identifiers and provider symbols. Period corrections require a reason and the version read by the browser. Yahoo and yfinance belong to the same source family, not independent corroboration.
7. Open `/settings/security` to reauthenticate, change the password, or revoke a listed session. Changing the password revokes all sessions, including the current one, and returns to login.

### Responsive financial workspace

- At widths of 1024px and above, financial facts use a TanStack Table with the server's bounded date/ID page. Counts refer only to that page, not all records. Financial strings are never sorted, rounded or totalled in the browser. Expand **查看原值與操作** to inspect the original detail and its permitted actions.
- Phones and tablets use record cards. Create/edit opens a full-screen Sheet below 1024px and a right-side panel on desktop. The form keeps the observed physical record/version even when background queries change. Native amounts, currencies, historical bookings, missing data and review scopes retain the same meaning in both views.
- Initial editor focus is **關閉編輯**. Tab/Shift+Tab stays within the modal. Cancel or Escape discards that unsaved form, without submitting or persisting a draft; outside clicks do not discard it. During submission, closing is blocked until the response arrives. A lost response does not prove that the server failed to commit: reconcile before changing intent.
- Cancellation returns focus to the observed control when usable, otherwise to stable page content. Confirmed success returns to a stable create/opening/completeness control where available; an in-progress refresh may require the same content fallback. No success is displayed before the server confirms it.
- Date/reference/history filters have individually removable chips. Filters, literal search, server cursor stacks and balance-view dates survive route changes only within the authenticated in-memory workspace. Reloading, logout or session expiry clears them. They are not automatically written to URLs, local/session storage or an offline database. Existing account-scoped journal links still explicitly include their account ID.
- The collapsible desktop sidebar links only to implemented destinations, including the data center. The five-item mobile bar opens trades, native cash, stock transfers, corporate actions and settings; open the data center through its Settings link. The product link opens service status. Canonical-income and valuation destinations are not represented by fake pages.

### Source diagnostics, observations and job controls

Open `/data-center` from the desktop sidebar or the Settings link on phones/tablets:

- **Source settings** reads candidate/local configuration separately from recorded pages. It never probes an upstream, imports a key, enables traffic or promises verified data rights.
- **Security diagnostics** requires an explicit catalogue identity and distinguishes dividends from provider-only splits. Requested dates are not coverage proof; coverage dates and provider billing balances remain unknown.
- **Dividend observations / split factors** retain exact strings, nulls, immutable first identities/hash/times, distinct sightings and explicitly omitted legacy counts. Date filters use event dates; they do not invent dates/currency or adopt income/shares. Source document links are references, not fetched/verified evidence; sensitive query/fragment links remain withheld.
- **Quote source terms/factors** opens a source-only Sheet from either observation list. Explicitly add one security, choose a manual/HKEX/SEC source, supply its known event date and exact dated symbol, then quote nullable DPS/currency or positive INT64 ratio strings and the document reference/reason. No provider, ticker, currency, amount or 1:1 factor is guessed. Optional alias reference failures do not block an unchanged uncertain retry; AS_DECLARED never becomes verification. See the [quotation walkthrough](docs/providers/WORKSPACE.md#quote-one-source-assertion).
- **Fetch jobs** opens a Sheet for explicitly added securities (up to 100), selected network sources and both inclusive dates. Nothing defaults to all securities/sources or a guessed history. Dividends are the default operation; split factors require explicit selection and `force` stays false. Acknowledgment and HTTP 202 mean accepted source-only intent, not verified dividends or financial synchronization.
- Open a job for textual progress, bounded item diagnostics and reasoned status meaning. Active jobs refresh; waits/terminal states require explicit refresh, preserving even a far-future UTC deadline. Requesting cancellation is not a claim that in-flight work has stopped. Saved observations and uncertain upstream spending remain; no ledger, receipt, locked FX or quota is reset.

Lists retain server order and bounded cursor navigation. Source views use the same desktop tables/mobile cards and accessible Sheet lifecycle as financial forms, with memory-only route-specific filters. Source mutations invalidate source reads only. See the [source workspace guide](docs/providers/WORKSPACE.md) for exact navigation, retries, status scopes and current limitations. Owned schedules, source quotation forms and managed settings at `/settings/providers` are available; canonical reconciliation remains unfinished.

### Source authorization and private keys

Open `/settings/providers` from Settings or the data-center provider link. Create an explicit disabled policy, then open its source detail for password confirmation, full reasoned versioned edits and immutable history. A policy always overrides installation-file fallback, including a disabled/cleared/unavailable decision. No provider, rights, request limits or live health is inferred.

EODHD, Alpha Vantage and Massive offer write-only private import/rotation. Paste only the exact key into the password control, never into a reason/reference. Invalid whitespace/newline/overlength paste is rejected and clears any previous key, avoiding native browser trimming. An import always saves a disabled revision; enabling later requires explicit rights, software request limits and acknowledgment. Key/reference presence is not key validity. Clearing appends a disabled policy without deleting files or cancelling jobs. The existing reauthentication form is inside each Sheet; confirming the password does not submit the other form. Server authorization and the frozen observed version remain authoritative, failures retain input, and an uncertain unchanged retry keeps its intent. Passwords/keys never enter query/mutation caches or persistent browser storage. See the [full settings and replay guide](docs/providers/PROFILES.md#browser-workspace).

### Record, correct and void trades

1. Open `/transactions` after creating the required portfolio, account and security. Select the portfolio, an active matching account, and the security from bounded searchable references. Changing a reference search clears the selection; an existing account/security/portfolio outside the first page is fetched explicitly rather than silently replaced. Archived accounts remain available in read filters but cannot receive a new/revised fact.
2. Enter the exchange-local trade date, buy/sell direction, exact quantity/price, fee and tax. Inputs remain decimal strings; browser BigInt validation checks scale/INT64 bounds without rounding or floating-point calculation. Leave unknown execution time blank; if known, provide an explicit aware RFC3339 timestamp. No time or cash funding is invented.
3. The selected security determines quote/settlement currency, not the account default. Enter reviewed historical USD FX yourself; there is no suggested/default rate. Changing the security clears old FX, so HKD identity 1 cannot silently become a USD historical rate. A future trade may keep FX unknown and remains an unposted scheduled draft until explicitly revised later.
4. Optional broker actual-local/HKD settlement fields require an evidence explanation. Otherwise, the server calculates settlement and returns its historical booked result. Trade fees are already included once; do not record them again as expenses. No browser preview is treated as the financial authority.
5. Save and inspect the returned status, native settlement, historical HKD booking and original-value/version details. These are individual financial facts, not current holdings, NAV or live valuation. A null HKD booking remains unknown; a draft's amount is labelled unposted.
6. To correct an active/draft record, open its full revision form and provide a specific reason. If another writer changed it, 412 leaves your input intact: cancel, reload the current version, reconcile and retry deliberately. Unchanged failed submissions retain their idempotency key; changed input creates a new intent. Do not assume a lost response means a transaction was not recorded.
7. To void, enter a specific reason and accept the separate confirmation dialog. Initial focus is Cancel. A dependent later sale/transfer can reject the operation; no success is shown and no original fact is deleted. The server's exact reversal and audit remain authoritative.
8. Filter by portfolio, account, security, date range or literal search, and explicitly include superseded versions when needed. The root-history control isolates one immutable revision chain. Pagination counts describe the current page only; unlike currencies are never totalled here.

### Native cash, operating expenses and starting assertions

- Open `/cash` for the complete journal, or `/cash/expenses` for expense facts. Select the active account, actual native currency and movement date explicitly. The account default does not perform an automatic conversion. Cash deposits are positive, withdrawals negative, and a non-external adjustment needs a reason. Expense charges are positive; negative refunds need evidence. Values retain six-place actual precision, not automatic cent rounding.
- Unknown USD FX is allowed for actual native movements. Leave it blank rather than inventing a rate; historical HKD remains MISSING_FX/unavailable. An optional designated HKD value still needs historical FX and evidence. On revision, the recorded HKD snapshot is carried explicitly so a note-only correction does not recalculate it. Changing native amount, currency or FX clears that carried value for deliberate review; it is not labelled provider-verified or silently inferred from the amounts.
- Full corrections and reasoned confirmed voids retain original facts and exact reversals. The generic journal offers controls only for editable manual items. Trade, expense, receipt, transfer and opening/rebase cash stays source-managed. The journal intentionally includes reversed and reversal rows; expense history can explicitly include superseded facts. Filtered-empty views do not mean the account has no recorded ledger.
- Use a journal item's account link, or its link in `/settings/accounts`, to open `/cash/accounts/{id}`. Each observed/default HKD/USD wallet shows recorded native net, separately confirmable actual balance, quality and historical HKD audit net. Unlike currencies are not added. UNKNOWN/PARTIAL/BEFORE_OPENING/NEEDS_REVIEW returns an unavailable actual balance even when the recorded net is zero. A known zero can coexist with a nonzero historical HKD audit net; that net is not live valuation or FX profit.
- The optional balance date requests the server's current-corrected effective-date view. It is not a reconstruction of what was known at an earlier audit timestamp. The current opening assertion is shown separately with its provenance and shared account version.
- Setting/replacing an opening requires an explicit amount, date, reason and historical USD FX, including for known zero. Blank input cannot save or silently clear it. A separate reasoned, confirmed withdrawal of the assertion reverses its managed effects and resets completeness UNKNOWN while retaining ordinary cash. Reintroducing a known zero does not restore COMPLETE.
- Explicitly set cash completeness only after reconciliation; choosing COMPLETE also requires a separate acknowledgment covering all native currencies, history and opening provenance. Missing FX/quotes can still prevent complete HKD valuation. Opening and completeness editors freeze the observed version even if background reads update, so stale input returns 412 rather than overwriting a concurrent account edit. Cancel, reload and reconcile before retrying.

### Paired cash and stock transfer forms

- Open `/cash/transfers` for paired principal movements, or `/transfers` for stock movement. Select one portfolio and its active owned source/target accounts. Changing portfolio/search clears dependent selections; cross-portfolio transfers are not silently represented as deposits or conversions.
- Enter both positive actual cash principals explicitly. Same-currency amounts must match numerically, including different valid decimal representations; browser validation uses scaled BigInt, not floating point, and preserves the original wire strings. Fees are separate real expense/refund records and are never hidden in principal or automatically charged twice.
- A same-account cash transfer is only an explicit different-currency conversion. USD historical FX may remain unknown; the application does not infer it from the two amounts. An optional positive designated HKD value applies only to the USD leg and still needs historical FX and an explanation. Changing principal/FX/currency clears an old designated override; a note-only revision preserves it. HKD identity never becomes a new USD rate.
- Inspect both historical booking legs and their delta after the server confirms the paired write. Missing delta stays unknown. A known nonzero delta is review-required, not realized FX profit or current cash valuation. Revision/void affects both legs or neither, with exact original-value reversals and immutable root history.
- For stock transfers, enter the security, exchange-local movement date and positive eight-place quantity. No cash/FX/cost override is submitted. The server calculates moving-average basis at the event boundary and validates full source/destination history; a future date is rejected rather than scheduled for automatic activation.
- The displayed stock cost is explicitly **BOOKED_AT_CREATION**. A later correction to an earlier trade may restate current replayed allocation without rewriting that snapshot. Stock movement itself does not create cash or realized profit. Use the root-history control to inspect old/new quantities, costs and sequence identity.
- Corrections require full input and a reason, preserve stable root/sequence, and return a new row ID. Superseded/void records are read-only. Confirmed void requires a reason; an orphaned destination sale/dependent transfer rejects without a success message. Reconcile that dependency rather than altering an original fact in place.

### Corporate-action terms and review

- Open `/corporate-actions`, choose the stable security and exchange-local effective date, and provide a reason. The catalogue is installation-wide. A revision keeps its original security, root and sequence; it cannot silently move an action to another holding.
- Enter rational terms as exact positive INT64 digit strings. The browser validates and compares with BigInt, sends strings and displays every returned digit, including adjacent values above 2^53. Split/reverse-split direction and paired terms are explicit; blank is never an invented 1:1.
- A new form starts NEEDS_REVIEW. Adopting supported terms requires both CONFIRMED and a separate acknowledgment that terms are not a complete valuation or cash/cost allocation. Fractional shares or unresolved subsequent actions can still require review; the server protects dependent sales/transfers atomically.
- CASH_IN_LIEU, MERGER and SPIN_OFF remain review-only evidence: no confirmation option pretends an unsupported allocation succeeded. They do not invent cash, cost or shares. A held unresolved action blocks ordinary ledger writes until appropriately resolved/withdrawn.
- SYMBOL_CHANGE takes an explicit new display ticker and no ratio. It preserves security_id, trades, identifier periods and provider aliases, and keeps an identity-mapping review. A future confirmed display change is rejected with input retained; save it as review and explicitly adopt after the market-local date. Do not undo an earlier adopted display change across a later dependency.
- Evidence URLs are stored references only, not fetched or labelled provider-verified. Changes to action type clear incompatible ratio/ticker fields and reset adoption acknowledgment rather than carrying unrelated terms silently.
- Inspect effect scope, root/history, original reason and review flags. Superseded/void records are read-only. A reasoned, separately confirmed void retains original terms and audit; dependent-share or display-history failures cannot be reported as successful allocation. The UI never computes a new holding/NAV from a partial unresolved prefix.

Brokers and accounts can be archived and explicitly reopened after confirmation. Historical relationships are retained. Lists show the current page, not an all-record total. Failed saves retain input; a 412 means another writer changed the record, so cancel, reload, and reconcile before trying again. Unchanged retries retain their idempotency key. No save is reported as successful before the server confirms it.

A disconnected page may display already loaded in-memory records with a warning; refresh and saves need a connection. This is **not** encrypted offline/PWA support, and no writes are automatically queued. Logout, session expiry, and password change cancel private reads and clear private query/mutation and view state. Offline snapshots/drafts, valuations, canonical dividends/receipts, verified market-session calendars and portable backup/restore remain future work.

## Provider integration status

The internal provider foundation includes a frozen capability catalogue for EODHD, Alpha Vantage, Massive, Yahoo, the optional yfinance wrapper, HKEX/SEC evidence and manual evidence. These are candidate access/market/origin definitions, **not configured availability, verified plan coverage or a license grant**. Every live validation remains NOT_TESTED; full raw-payload storage is disabled by this catalogue. No API key is included in it.

Provider symbols must be explicit dated mappings. The resolver covers the complete inclusive requested window, splitting it at alias changes while retaining the original security ID, alias ID/version and validity periods. Missing heads/tails/interior days and overlapping/colliding identities return no fetch segments; display tickers are never guessed or rewritten. An unverified mapping stays labelled as such. Resolving an alias does not prove that an upstream service has any data for it.

Bounded immutable observation models retain exact eight-place DPS strings, independent event currency, nullable dates and an explicit adjustment basis. Their canonical hashes distinguish changed source terms and regular/special events without overwriting prior evidence. Yahoo and yfinance derive the same YAHOO origin; manual/official references are not an additional vendor vote. Unknown/adjusted DPS remains unresolved rather than silently becoming as-declared income. Missing values and retrieval failures never imply zero dividends, and observations contain no receipt or verified-event status.

The internal HTTPX transport makes **one bounded attempt**, not an automatic sync. It uses fixed HTTPS provider origins/endpoint paths, TLS verification, no environment proxies or redirect following, 5-second connect/pool and 20-second read/write timeouts, plus a separate 20-second total deadline. The default limit is 1 MiB for both wire and decoded bodies; gzip/deflate decompression is bounded. JSON numeric tokens become Decimal/integer values, never financial floats; duplicate keys, nonfinite values, malformed JSON and oversized responses fail explicitly. Receiving JSON, including an empty array or null, does not itself establish dividend coverage or income.

Credentials come only from the trusted server-side access object. Providers are disabled until explicitly enabled with a terms acknowledgment; an API key alone is not permission or proof of coverage. EODHD/Alpha require query authentication, while Massive uses a Bearer header. Incoming pagination URLs cannot change the fixed origin/path or override the server credential. Recorded source pointers omit the complete query; transport outcomes contain sanitized codes and exclude raw payloads/keys from repr. HTTPX/httpcore request/debug logging is suppressed only within these provider operations, since upstream URLs, headers or errors can contain secrets. The client never reads a credential file or publishes an access object to the browser.

429/503 waits are returned as absolute UTC retry deadlines, including conservative parking for unrepresentably large valid waits. No retry, fallback or quota spending is hidden inside the client: durable per-stock quota/retry/circuit/lease policy belongs to the worker. Timeouts/cancellation close responses, and failed/redirected/unsupported requests never manufacture dividend rows or zero income. HKEX/SEC/manual evidence and the optional SDK mode are not turned into automatic HTTP scrapers by this transport.

The internal **EODHD adapter** implements the documented JSON dividends and split-factor routes over that transport. It accepts only complete explicit alias segments, passes valid inclusive from/to dates, and makes one call per operation. It rejects an invented paging cursor or undocumented envelope rather than silently treating a first page as complete. Malformed, duplicate or outside-alias-window records produce an explicit partial/error result while preserving valid source evidence. A documented empty array remains NO_DATA, not confirmed zero income.

EODHD's explicit historical `unadjustedValue` takes priority, including a known zero; `value` alone remains SPLIT_ADJUSTED. The documented historical per-share cash does not independently establish declaration/ex-date terms or an owner's receipt, so historical input retains UNKNOWN adjustment basis with a review warning. Missing payment dates stay null. The original three-letter source currency and recognized frequency are retained separately; unsupported currencies are not replaced with quote currency, and Quarterly/Annual frequency is not guessed to mean REGULAR. Split factors use exact reduced INT64 rational terms and stay PROVIDER_ADJUSTMENT_ONLY, never automatically changing ledger shares/cash/actions. Local adapter health reports configuration only, with unknown quota/last success and NOT_TESTED live validation, not a paid probe.

Use the [live integration report template](docs/providers/LIVE_INTEGRATION_REPORT_TEMPLATE.md) for authorized plan/coverage testing. It starts entirely NOT_TESTED, requires at least 20 explicit samples across HK/US with uncommon identifiers, and separates resolution, dates, DPS, currency, basis, pagination, licensing and independent origins. Keep completed/private reports outside Git; do not paste keys, full private URLs or licensed payloads.

**Alpha Vantage** implements the DIVIDENDS compatibility profile with explicit `symbol`/`data` validation and sanitized HTTP-200 Note/Information/Error Message handling. The official page documents the request but does not supply a complete response example; the public SDK consumer contract is an additional compatibility reference, not a live guarantee. Recognized `amount`/`dividend_per_share` variants must agree if both are present. Currency is never filled from the US market/quote, and adjustment basis remains UNKNOWN. Since this endpoint exposes whole-symbol history rather than a documented date filter, recognized outside-window records are counted and excluded locally without becoming income or a malformed-page failure. Undocumented pagination and unproven split contracts remain explicit errors/unsupported operations, not invented schemas.

**Massive** uses the documented `/stocks/v1/dividends` and `/stocks/v1/splits` fields, not an assumed legacy endpoint. Original `cash_amount` has priority including zero; an adjusted fallback stays SPLIT_ADJUSTED and original cash still does not prove ex-date terms/owner receipts. Source distribution/frequency and event IDs are retained; cumulative historical price-adjustment factors are not individual dividend amounts or split ratios. Individual split terms are exactly `split_to / split_from`; stock-dividend factors stay unadopted with review warnings. US market context and returned ticker must match explicit identity, with no dot/hyphen guessing.

Massive continuation links are validated against the fixed origin and same operation, stripped of credentials and converted to stable relative references before reuse. Explicit ticker/date scope changes, credential echoes, unsafe references and immediate loops produce partial/error results while retaining valid current-page evidence. Each adapter fetch visits exactly one page; the durable worker follows every safe next reference through a new reservation and detects longer cycles/page limits before another request. It does not silently stop after page one or hide quota spending. Page completeness is parsing completeness, never all-history coverage or financial verification.

**Yahoo** implements an opt-in public chart compatibility profile, cross-checked against the yfinance consumer source rather than a guaranteed official Yahoo API contract. It makes one fixed-host request per operation, without cookie/crumb negotiation, browser impersonation, hidden retries or fallback. The resolved exchange catalogue supplies the IANA timezone: inclusive local dates become integer epoch bounds with an exclusive next-day end, including historical daylight-saving changes. Returned symbol/timezone conflicts fail; event map keys must canonically match integer timestamps, and dates stay inside the explicit alias window. These source timestamp keys are not canonical financial event identities.

Yahoo keeps exact event amounts and any explicit event currency; quote metadata never fills missing dividend currency. Adjustment basis remains UNKNOWN, payment/declaration/record dates are not guessed, capital gains/calendar forecasts are not historical dividend evidence, and SDK-style price/dividend repair is not applied. Split numeric terms reduce exactly and remain PROVIDER_ADJUSTMENT_ONLY; a supplied ratio string must agree. HTTP-200 chart errors, invalid shapes and undocumented pagination are explicit failures. A recognized response with absent/empty requested events is only NO_DATA, not confirmed no dividends. Coverage, rights and live validation remain unverified/NOT_TESTED.

**Human document evidence** has typed HKEX, SEC and manual profiles. Supplied assertions require a frozen local citation ID, bounded reference and reason; official attribution also requires an explicit matching HTTPS document host. This checks the supplied reference, **not the document's contents**. No announcement is fetched/scraped, and manual citations may refer to a local document without a URL. Evidence remains present when observations are serialized through the shared base-typed pages; changing terms or the citation creates a distinct assertion/hash, not an overwrite. Existing vendor hashes remain unchanged when no citation is present.

Manual evidence requires explicit enablement/rights acknowledgment and a matching dated identity/market. Exact owner-entered DPS strings, nullable dates/currency and positive INT64 rational split terms are retained; no quote-currency substitution, automatic VERIFIED/RECEIVED status or ledger adoption occurs. These profiles are not extra vendor corroboration votes. Empty input is NO_DATA, never authoritative no dividends; citations are not fabricated HTTP responses/raw hashes. The source-capture library persists these cited assertions with audit and the source observation API reads them; protected actor-bound creation and responsive quotation forms are installed, while canonical review remains unfinished.

**Optional yfinance** is a pinned `1.8.0` action-parser profile (`yahoo-sdk` extra), not permission to use the SDK's downloader. The same bounded HTTPX Yahoo client supplies exact JSON; only the SDK's pure `parse_actions` utility receives whitelisted action fields. Decimal dividend amounts and Fraction split terms are preserved through column-typed tuple extraction, then checked against exact source amounts/currencies/identities. Binary floats, changed/lost/duplicate records, unexpected schemas or SDK warnings cannot become silently accepted financial facts. Ticker/history/download, SDK sessions/cookies, price repair and hidden retry/fallback are not used. Dates/currency/basis retain the Yahoo limitations and both profiles count as one upstream origin, not another vote or quota bypass.

Default runtime images exclude the SDK and report UNCONFIGURED/SDK_NOT_INSTALLED before I/O. The development lock includes it for actual parser tests; all previous dependency versions remain unchanged. Set `ENABLE_YFINANCE=true` in the local `.env` or prefix the Compose build command to install the extra, for example `ENABLE_YFINANCE=true docker compose build api`. Keep the setting for later rebuilds. Only literal `true`/`false` is accepted. **Installation does not enable provider traffic or acknowledge rights**: trusted server-side enablement/terms gates still apply, and incompatible SDK versions fail explicitly. No live coverage is claimed by these package tests.

The **server-only operator-file configuration reader** and real adapter bindings are implemented. `spm-manage providers-check` inspects all eight profiles locally without a database, bootstrap token, provider request or key disclosure. Explicit rights references/reasons, strict request budgets, private ownership/modes, versioned credentials and safe snapshot rechecks are required; missing/unsafe files stay UNCONFIGURED rather than breaking financial readiness. Compose separates a read-only private credential volume from data/backups. See [provider configuration and rotation](docs/providers/CONFIGURATION.md) for the exact format, Docker-secret policy and diagnostic limits. The operator CLI remains separate from the [reauthenticated browser settings](docs/providers/PROFILES.md#browser-workspace).

The **append-only source-capture library** persists completed page attempts, non-secret configuration/dated-alias/parser snapshots, exact dividend/split observations, repeat-retrieval links and audit in one short transaction. The same attempt replays without writes; a later retrieval of unchanged terms adds a run/sighting without rewriting the original observation. Changed terms remain distinct evidence. New captures recheck alias identity/version; callers must also recheck private configuration before and after HTTP. Legacy rows retain unresolved provenance rather than invented metadata. See [evidence storage and integrity](docs/providers/EVIDENCE.md) for the contract and upgrade behavior.

The **protected cited assertion API** at `POST /api/v1/providers/cited-observations` reuses those manual adapters and immutable capture for one explicitly dated owner quotation, with server-minted citation identity, current session/CSRF checks and atomic actor/request-correlated audit/idempotency. Completed replay returns original IDs even after later policy/alias changes; a quotation never fetches a document, spends provider quota or adopts financial facts. Responsive quotation forms use this API from the existing observation lists, preserving input and original intent through uncertain retries without depending on current diagnostic/alias reads. The reference canonical manual dividend event operation remains separate unfinished work. See [cited source assertion boundaries](docs/providers/CITED_ASSERTIONS.md).

The internal [candidate suggestion library](docs/dividends/CANDIDATES.md) retains one seed per source and marks nearby dates, reused provider event IDs and ambiguous same-day amounts for review only. It never merges sources or supplies a canonical event identity. The separate [matched dividend evidence comparison](docs/dividends/EVIDENCE.md) can assess explicitly associated source terms with exact, order-independent tolerance and field-level conflict/incomplete traces. It never infers event identity, fills missing currency/dates/unknown basis, counts Yahoo wrappers as separate vendors or promotes a quotation to VERIFIED. Optional [cited split-basis derivation](docs/dividends/BASIS.md) uses an explicitly complete split-only declaration and exact rational action references for a separate comparison view; raw observations remain unchanged and precision/overflow stays unresolved. Migration `0014_dividend_basis_assertions` adds an initially empty append-only declaration store with source pins and real current-action/version/complete-recorded-chain INSERT fences (52 business tables at that revision). Its [audited writer and protected owner API](docs/dividends/BASIS.md#protected-declaration-api) validate original v1 content/hash, bind server identities, session/CSRF and atomic idempotency, preserve historical replay after action corrections and require current recorded scope for new declarations. No worker or canonical consumer is installed. Migration `0015_dividend_candidates` adds an empty append-only [candidate revision store](docs/dividends/CANDIDATES.md#inert-candidate-revision-storage), bringing the schema to 53 business tables. Its [protected candidate review API](docs/dividends/CANDIDATES.md#protected-candidate-review-api) retains explicit source associations, checks original hashes and current owned basis scope, and preserves historical comparison after later action corrections. It binds server-minted identities, current session/CSRF, version CAS and atomic audit/idempotency; review is not verified market history or a canonical event. The candidate UI and financial adoption remain separate work. Neither a stored declaration nor conditional comparison proves complete market history, document verification or receipt of income.

Migration `0016_dividend_event_controls` adds initially empty [managed event integrity foundations](docs/dividends/EVENTS.md) and brings the schema to 55 business tables. Stable owner-controlled event IDs, immutable decision/evidence history, publication audit fences, manual locks and account-specific receipt-review checks are storage safeguards only. Existing events and receipts are not silently adopted. Managed receipt facts cannot be rewritten, reparented or voided without an exact cash reversal and correlated audit. The migration also blocks replacement writes against retained cash entries, audits, dividend revisions and source observations. The schema does not provide a canonical event/receipt API, calculate entitlements or guarantee that raw SQL writes form an authenticated atomic workflow; projection history for repeated changes within one revision is still required.

The **durable source-job control library** seals owner/intent/request/configuration identity, independently tracks each security/provider/operation, and stores fenced leases plus immutable page checkpoints. It supports expiry recovery, bounded work retries, persistent wait deadlines and cancellation without deleting committed evidence. Source capture, checkpoint and audit share one short transaction; stale or cancelled workers cannot commit a new page. A completed source-only job is not verified dividend income. See [source-job control and recovery](docs/providers/JOBS.md) for progress, retry and legacy-state semantics.

An internal [immutable credential preparation layer](docs/providers/CONFIGURATION.md#inactive-immutable-credential-preparation) creates exclusive, fsynced mode0600 editions in an explicitly provisioned private directory outside data/backups. Preparation itself never enables a profile, changes operator files or writes a database fact; ready metadata contains no credential or plaintext-key hash. The [reauthenticated import API](docs/providers/CREDENTIALS.md) now verifies preparation outside writers and binds a new disabled owned revision with atomic CAS/audit/sensitive idempotency. Sequential committed replay skips all private I/O; a concurrent or interrupted request may leave unselected files, which are retained rather than overwritten, deleted or adopted. Preparation/import alone is not filesystem/database atomicity, provider verification or access enablement. The [browser workspace](docs/providers/PROFILES.md#browser-workspace) provides write-only import; no key-writing CLI is installed. Read-only operator secret mounts and the file-only diagnostic CLI are unchanged.

The initially empty `0012_provider_profiles` schema retains immutable owner/provider policy revisions and opaque credential selections, with disabled first versions, contiguous version fencing and no legacy adoption. The authenticated policy/import APIs, installed owner selector and responsive settings workspace use this history without adopting legacy files. See [managed policy storage boundaries](docs/providers/CONFIGURATION.md#append-only-managed-policy-schema).

The [internal managed-profile controller](docs/providers/CONFIGURATION.md#internal-versioned-controller) now reuses the operator policy validator, applies observed-version CAS and appends each normalized decision with its correlated audit in a short transaction. It accepts checked server-only credential references without reading keys in that writer, returns the latest disabled/cleared decision rather than reviving old access, and rejects corrupt retained history without repair. This controller is now wrapped by the [owned policy-definition API](docs/providers/PROFILES.md): list/read/disabled create/full reasoned CAS update/immutable history under `/api/v1/provider-profiles`. Writes recheck session/CSRF and five-minute reauthentication in the short transaction **before cached replay**; stale versions return 412 and replays retain their original DTO/ETag. Current responses report `OWNED_PROVIDER_POLICY` and `runtime_selection_installed=true`, hide private references and do not claim provider health. Historical pre-install idempotent receipts retain their original scope/false capability; GET reports current code/state. The latest owned disable/clear overrides operator fallback for future pages without cancelling jobs or refunding uncertain spending. `POST /api/v1/provider-profiles/{provider_id}/credential` separately accepts only a private exact credential/reason and observed version for EODHD/Alpha/Massive; it always saves a new disabled definition, never terms acknowledgment or access enablement. The responsive [owned settings workspace](docs/providers/PROFILES.md#browser-workspace) supplies these policy, write-only key and history controls.

The installed owned selector resolves the latest decision for an explicit active owner, blocks fallback for disabled/unavailable selections, rechecks metadata after private reads and retains actual constant-time key comparisons. It uses distinct owner/revision/edition snapshots rather than fabricated operator file versions. Migration `0013_managed_source_contexts` now retains exact managed provenance and fences new reservations/page outcomes against the current owned decision; an owned operator fallback must still have no overriding definition. The internal shared-budget envelope also rechecks every owned peer in those transactions, including unavailable peers becoming enabled and limits changing while the primary stays unchanged. Existing contexts/financial facts are preserved, and completed replay stays zero-write after rotation. HTTP queueing, the actual scheduler/dispatcher and authenticated diagnostics now use this same owner scope and original shared-budget envelope. Old sealed/null pins cannot silently adopt a new key; configuration races become CONFIG_CHANGED with conservative spending. Private loading/rechecks stay outside writers and the event loop. The separate diagnostic CLI remains operator-file-only/no DB/bootstrap/probe. See [owned selection and provenance boundaries](docs/providers/CONFIGURATION.md#managed-provenance-and-current-selection-fences).

The **upstream-attempt library** atomically reserves a bounded request before HTTP, using a shared-origin exact token bucket and conservative rolling-24-hour allowance. Yahoo/yfinance spend the same budget at the minimum enabled profile limits; key/profile rotation does not reset it. An existing grant never permits sending the request again. Pending, cancelled and crash-uncertain reservations remain spent; completed source/checkpoint/outcome/circuit/work changes commit together. Absolute Retry-After, bounded exponential jitter, one half-open circuit probe and mid-pagination waits are durable without consuming another logical work round. See [upstream budgets and attempt safety](docs/providers/BUDGETS.md) for the policy and conservative usage semantics.

The **actual worker dispatcher** now claims managed source-only jobs, checks configuration/SDK/operation/market/dated aliases before I/O, renews short leases, follows all safe pages/alias segments and applies the quota/circuit/outcome contract. Missing manual citation input is not turned into a scraper; unavailable or unsupported items do not spend requests. Mid-page waits park durably, failed symbols remain independent, and restart resumes retained checkpoints. The heartbeat remains independent while awaiting HTTP. See [fenced worker execution](docs/providers/WORKER.md) for cancellation, conservative unknown spending, shutdown and recovery.

### Source-only synchronization and job APIs

`POST /api/v1/dividends/sync` accepts explicit `security_ids`, `provider_ids`, `from_date` and `to_date`, plus optional `operations` (default dividends only) and strict boolean `force` (default false). It requires owner authentication, Origin, CSRF and idempotency headers. **202 Accepted** returns `job_id`, `scope=SOURCE_ONLY`, `canonical_reconciliation=NOT_RUN` and a job `Location`; it is not a promise of provider availability or adopted income. Private configuration is selected server-side before the short writer transaction; no provider HTTP runs in the request handler. No date/security/provider defaults or quota bypass are invented.

- `GET /api/v1/jobs`: owner-scoped managed jobs, newest first, bounded cursors, `status` and literal `q` filters.
- `GET /api/v1/jobs/{id}`: current typed state, committed progress/counters and ETag. Internal plans, raw result JSON, profile hashes, credentials and upstream cursors stay private.
- `GET /api/v1/jobs/{id}/items`: bounded per-security/provider/operation states and safe error codes, with `status`, `provider_id`, `security_id` and literal `q` filters.
- `POST /api/v1/jobs/{id}/cancel`: authenticated/idempotent stop intent, with no body or `{}` and no If-Match requirement. Returns 200 with the current state: CANCEL_REQUESTED is not already stopped. Committed evidence and uncertain upstream spending are retained; cancellation does not reverse financial facts.

An unchanged enqueue/cancel retry returns its original response, even if the worker has progressed; GET provides the latest state. Reusing a key with changed input conflicts. Unconfigured jobs can be accepted and later explicitly skipped/failed without sending requests. Source success, NO_DATA and scanned progress never mean verified dividends, complete market coverage or provider billing. See the [full source-job HTTP and recovery contract](docs/providers/JOBS.md).

### Provider diagnostics without a live probe

Three authenticated no-store read APIs are available:

- `GET /api/v1/providers`: the fixed eight-entry candidate catalogue and local configuration/dependency readiness. Key presence is not enablement, licensing, plan coverage or an extra Yahoo vendor vote.
- `GET /api/v1/providers/{provider_id}/health`: current local STALE/UNCONFIGURED readiness, separately from the latest immutable recorded source page and page-scoped success time. No request is sent; `quota_remaining` remains null rather than guessed from software budgets.
- `GET /api/v1/providers/coverage`: bounded provider/security diagnostics, with `security_id`, `provider_id`, `operation` (default DIVIDENDS), literal `q`, `limit` and keyset `cursor`. It separates candidate market/operation support, configuration, retained unique assertions and last recorded processing. Split data does not imply dividend data. The historical symbol comes from its frozen context, never today's display ticker.

Coverage remains **unknown**: `coverage_start`/`coverage_end` are null with `coverage_basis=NOT_ESTABLISHED`, even after successful page/job processing. NO_DATA does not confirm zero dividends; later failure/empty pages do not delete existing evidence. All live validation remains NOT_TESTED. Legacy runs/status payloads are explicitly omitted/flagged, not silently adopted or repaired by GET. See [provider diagnostics and non-authoritative coverage](docs/providers/DIAGNOSTICS.md) for field scope and pagination.

### Exact source observation reads

`GET /api/v1/dividends/observations` and `GET /api/v1/providers/split-observations` return authenticated, no-store provenance-v1 source assertions. Optional security/provider, inclusive event-date and literal search filters use bounded retrieval-time/ID keyset pagination. Missing dates/DPS/currency stay null; known zero is an exact decimal string, not an estimate. Split numerator/denominator are exact positive INT64 JSON strings and remain PROVIDER_ADJUSTMENT_ONLY, never adopted shares/cash.

Rows retain the original run, fingerprint and retrieval time plus distinct sighting count/latest seen time. Repeated/changed source terms do not become income twice. Current keys/profiles are not needed to read immutable history, and no provider/document request runs. Typed citations are user assertions, not verification; query/fragment document links are explicitly withheld without rewriting their original hash. Invalid v1 provenance fails safely rather than being rounded or silently repaired. Unresolved legacy dividend rows remain stored and explicitly counted as omitted, not dumped or automatically promoted. See [immutable source observation reads](docs/providers/OBSERVATIONS.md) for exact field scope, null-date filters and privacy behavior.

The [managed settings workspace](docs/providers/PROFILES.md#browser-workspace) now provides policy, reauthentication, write-only credential import and immutable history; the [source workspace](docs/providers/WORKSPACE.md) provides explicit queueing/diagnostics, owned schedule editing and actor-bound quotation forms. Canonical cited-evidence review/reconciliation remains unfinished. No default schedule or provider enablement is created at startup. The leader evaluates only explicitly enabled, due definitions. Source execution does not reconcile canonical dividends, create receipts, change ledger facts or claim a complete financial sync. Default unconfigured installations make no upstream requests.

The [source schedule API](docs/providers/SCHEDULES.md) provides owned `GET/POST /api/v1/source-schedules`, `GET/PATCH /api/v1/source-schedules/{id}` with reasoned full-body updates, and immutable occurrence lists, with session/CSRF/idempotency/CAS, no-store and safe typed DTOs. Definitions begin disabled. Explicit enablement permits the actual single-leader worker to coalesce missed slots and atomically queue source-only work with replay/backpressure; `automatic_materialization_installed=true` describes this installed path, **not** worker health, provider success or verified coverage. Private profile preparation stays outside writers and the event loop, independently of dispatch/heartbeat. SIGTERM stops new evaluations; restart retains immutable receipts, initial-backfill consumption and shared quota spending. Metadata reads/writes do not probe providers, spend quota, adopt financial facts or establish verified trading sessions. The responsive owner schedule workspace is available at `/data-center/schedules`, with explicitly disabled creation, reasoned full-version enable/pause/edit and immutable receipt pages linked to actual source jobs. It preserves server order, frozen editor versions/412 inputs and consumed-backfill origin, labels local cadence/UTC progress without claiming an official trading calendar, and never treats queue/skip/empty history as income. Far-future schedules do not continuously poll; reload to confirm current metadata. Private view state is memory-only and schedule writes invalidate only source reads.

## Trusted HTTPS proxy

For production, the NAS proxy must terminate TLS and **overwrite** `X-Forwarded-Proto` with `https` before forwarding to the loopback-only web port. Never publish the API port or attach untrusted containers to the application's private Compose network. Uvicorn trusts only the gateway's reserved internal IP (`WEB_INTERNAL_IP`, default `172.30.93.10`); Nginx overwrites `X-Forwarded-For` rather than accepting a caller-selected rate-limit address. `APP_SUBNET` defaults to `172.30.93.0/24`. Change both network settings together if they overlap a LAN/VPN/Docker network, or when running multiple instances concurrently. When a NAS proxy is present, the default login limit may therefore group devices behind that proxy into one bucket. The application also validates the exact public Origin and rejects insecure authentication requests.

## Checks

```sh
make check
```

Tests use disposable databases. The checked-in reference contract and golden fixtures do not depend on private specification files. The executable OpenAPI and generated TypeScript describe only implemented operations; CI verifies generation drift.

For browser onboarding tests, use a **fresh isolated** Compose project and loopback address; never point tests at a real installation. Bootstrap/password/session/provider values stay out of failure diagnostics through the [private browser policy and controlled failure gate](docs/testing/PRIVATE_BROWSER_DIAGNOSTICS.md): dependency-ordered redaction, no automatic DOM snapshots/traces/video/screenshots, and a reporter that emits only status/location/counts. Browser passwords are generated in worker memory. Raw worker output and failure details are intentionally not printed; failures still retain their real exit status.

```sh
npx playwright install chromium
npm run test:e2e:privacy
npm run build
npm run test:e2e:credentials
# Choose a new project name each run; existing named volumes are not silently erased.
# Export overrides so the suite's Compose restart subprocesses inherit them too.
export WEB_PORT=18083 PUBLIC_ORIGIN=http://localhost:18083
docker compose -p spm-e2e-local-001 up -d --build --wait
SPM_E2E_COMPOSE_PROJECT=spm-e2e-local-001 \
SPM_E2E_BASE_URL=http://localhost:18083 npm run test:e2e
docker compose -p spm-e2e-local-001 down
```

The separate `test:e2e:credentials` command needs no Docker or existing account. It allocates a new private temporary root/loopback port, starts the actual FastAPI/Uvicorn application with the built SPA, and drives real Chromium setup/reauthentication, import, enablement, rotation to disabled, clear and original replay after clear. It checks two exact mode0600 editions in a mode0700 directory, key absence from database rows, zero provider work and unchanged financial/source facts. No worker/live provider or existing installation is used; its generated synthetic tree is removed after server shutdown. This is a source-workspace UI/HTTP/files gate, not a container/NAS/TLS/OS-network-isolation claim. Output and failure artifacts follow the pinned private policy.

The seven-test Compose suite keeps both deployment credential vaults empty. Its managed-settings step reuses the final authenticated session for current password confirmation, disabled policy creation, safe empty key-editor rendering/invalid-paste rejection, a real second-writer412, response-held pending-close protection, explicit keyless enable/disable and physical25+2 policy-history pages in desktop/mobile/dark views. Additional history rows are genuine policy API revisions, not provider runs, income or mocked list responses. Accepted key imports are tested only in the separate temporary fixture, never by seeding deployment secrets.

The serial browser scenarios verify one-time initialization, authenticated rendering at 360/768/1440px, light/dark views, exact opening-cash input with required historical FX, non-overwriting 412 conflicts, archive/reopen without orphaning linked accounts, exchange-specific leading-zero/dotted tickers, dated mappings, disconnected read warnings, reauthentication, session revocation, and password-change revoke-all. Modal keyboard focus, background scroll locking, and the unchanged strict CSP are exercised against the actual Nginx/API/worker deployment. A third scenario uses authenticated **real HTTP requests**, rather than browser forms, to verify trade posting/retry, oversell rejection, stale versions, immutable revision/FX preservation, dependent-sale rollback, exact void/retry, opening preservation, and an unposted future draft. A fourth verifies missing-FX native deposits, immutable cash corrections, operating expenses, a separate micro-HKD wallet, managed-source protection, and exact reversal/retry without rewriting opening cash. A fifth verifies paired cash revision/void, same-account currency conversion, cost-preserving security movement with no cash effect, stable revision sequences, destination-sale dependency rejection, and final native balances. A sixth uses real HTTP to verify exact 3:2 split adoption, stable-sequence correction, dependent-sale rollback, explicit unallocated-action review, display-only symbol adoption without rewriting identifiers, and opening replacement/clear/known-zero reintroduction with exact historical reversals and a separate completeness assertion. It deliberately distinguishes a zero native balance from a nonzero historical HKD booking net. A seventh drives the actual trade browser forms: buy/sell, oversell input preservation, a real second-writer 412, immutable correction/history, failed dependent void, reasoned confirmed void, and an unposted future draft with unknown FX. It checks 360/768/1440px, light/dark rendering, no horizontal overflow, bounded 44–56px input heights, strict CSP, safe confirmation focus and no private browser storage. An additional named cash/expense/opening step within that authenticated browser journey verifies native missing-FX recording/adoption, exact note-only expense preservation, reasoned refunds and voids, an independent micro-HKD wallet, shared-account-version 412 handling, opening replacement/clear/known-zero reintroduction, and a separate completeness acknowledgment. It reuses the existing session rather than weakening production login limits or adding sleeps. A named source-job API step also queues a real source-only intent on the fresh unconfigured deployment, observes the actual worker's skipped/failed outcome with zero upstream spending, follows owner-safe job diagnostics, reads the real provider catalogue/local health/unknown coverage plus explicitly empty source-observation pages, and proves the financial snapshot unchanged. The authenticated journey also includes a transfer-form step: same-currency principal rejection, unknown-FX adoption, stable paired history/void, same-account conversion with a non-profit review delta, stock transfer without cash movement, destination-sale dependency rejection and cost-preserving revision/void. The action-form step verifies explicit 3:2 adoption/acknowledgment, dependent-sale rollback, immutable 2:1 revision, review-only merger/blocked ledger write, display-only ticker change without alias rewrites, future display adoption rejection, and an exact INT64_MAX ratio saved/displayed without Number rounding. The suite currently has seven serial tests with these distinct financial journeys. A further named workspace step reuses the authenticated session to exercise all six financial lists at 360/768/1440px, exact desktop cells and server cursor pages, create/edit Sheet geometry and focus return, safe-area/reduced-height submission access, collapsible/five-item navigation, memory-only filter restoration, and strict CSP. It holds a response only after the real server commits a cash entry, verifies that Escape/Close cannot discard the pending operation, then confirms exactly one journal posting. A real 25-row boundary is compared with server IDs on both pages; no total count is invented. Screenshots after token consumption are local artifacts under `.local/`, excluded from Git. Use a fresh test project for each run: these scenarios intentionally change the synthetic owner's password. Canonical dividend/allocation, valuation and encrypted-offline/recovery workflows will be implemented and tested in later stages. Private failure diagnostics are now gated; continue using only isolated synthetic credentials/data, never a real installation. Reverify the pinned artifact behavior before a Playwright upgrade.

The data-center step also reuses that final authenticated session. It drives actual local diagnostics/empty source pages, explicit default-dividends queueing, response-held pending-close protection, the real unconfigured worker outcome and a deterministic queued cancellation while only the isolated test worker is stopped. A default-image **network-none** helper uses the genuine bounded EODHD adapter and immutable writer with three mocked HTTP requests to retain 27 dividend assertions, two provider-only rational factors and repeated sightings; temporary synthetic profiles/keys never enter the deployment data/secret directories. The browser reads these real persisted pages without API response mocking, compares an actual 25+2 keyset boundary by physical IDs, preserves scaled-INT64_MAX DPS/unknown event currency and both exact INT64_MAX/(INT64_MAX-1) and 1/3 factors, then verifies current profiles remain unconfigured, authoritative coverage unknown, financial snapshots unchanged and private storage empty. No live provider traffic or credentials are required or authorized by this test.

A further named cited-source API step reuses that same authenticated session: explicitly create/enable three manual source policies and dated aliases, append four genuine actor-attributed quotations through the protected POST, verify exact DPS8/nullable currency/source metadata and both rational boundaries, pause all three policies and replay the original response without new evidence. The observation reader renders the retained reference/withheld link in desktop and mobile views.

The named quotation-form step then explicitly reenables those synthetic keyless profiles and drives both real Sheets at 360/768/1440px. It holds an actual committed 201, verifies pending Close/Escape protection, drops only that response, disables the policy, corrects the saved alias and selectively aborts metadata reads. The unchanged UI retry returns the original 201/IDs/key without another run/sighting. Genuine further form writes retain MAX DPS/null currency with independent EUR/QUARTERLY metadata, known zero HKD/SPECIAL/AS_DECLARED, 1:3 and MAX/(MAX-1) factors; unsafe official URLs and overflowing ratios remain field errors without truncation/adoption. All three policies end disabled. These are source API/form writes and a controlled metadata-read failure, not document downloads/verification, live provider responses or canonical financial adoption. Both credential vaults and all job/budget/financial snapshots remain unchanged.

The source-schedule step reuses the same session for disabled creation, reasoned enable/pause, real 412, original-response replay and safe history reads. A separate network-none fixture freezes only explicit old synthetic intent; the deployed daemon uses its unmodified wall clock to coalesce one due occurrence, queue source-only work and report SKIPPED/UNCONFIGURED without HTTP or work rounds. The browser reads that actual immutable receipt/job through protected socket APIs, restarts the real worker and proves no duplicate occurrence/job or financial change. All eight deployment profiles remain unconfigured; no synthetic key is provisioned there. Successful paid/live execution is not claimed.

The schedule browser-form step additionally drives real disabled creation, explicit future enablement, a genuine second-writer 412 with preserved input, reviewed reasoned pause/edit, consumed-backfill locking and 360/768/1440px sheet geometry/focus. A paused, network-none controller fixture retains 27 actual immutable receipts (one queue/26 in-flight skips), and the browser compares the server's physical 25+2 IDs, frozen windows and real job links in desktop/mobile/dark views. This is a synthetic history fixture, not 27 automatic provider runs; the actual unconfigured daemon handles its one queued job with zero rounds/HTTP. The final authenticated journey has a 180-second whole-test budget for its additional steps, while action timeouts, production login limits, TLS/proxy trust and disabled traces/videos remain unchanged.

## Data and privacy

- SQLite uses WAL, foreign keys, full synchronization, and versioned Alembic migrations.
- API responses are marked `Cache-Control: no-store`.
- `.env`, credentials, databases, backups, local development notes, and source specification files are excluded from Git.
- Market data licensing is separate from the application license. Missing credentials or unavailable data must never be represented as successful synchronization.
- Public health endpoints return status only. They do not expose account data or configuration.

## License

[MIT](LICENSE). This application records investments; it does not execute trades or provide investment or tax advice.

Deployment, recovery, and full user instructions will be expanded alongside their verified implementations. Do not deploy this development foundation with real financial data.
