# Provider diagnostics and non-authoritative coverage

These installed **read-only** HTTP APIs distinguish candidate capabilities,
current local configuration and immutable recorded source pages. They do not
probe upstream services, reserve requests, import keys, enable providers, repair
legacy rows or adopt financial facts. Every route requires an owner session and
returns `Cache-Control: no-store`.

All paths below have the `/api/v1` prefix. Authentication follows the
[owner guide](../../readme.md#owner-authentication). No credential or file path
belongs in an application URL or request body.

## Candidate catalogue

`GET /providers` returns the fixed eight-entry catalogue, ordered by provider ID,
with `items` and `next_cursor=null`. Each entry includes its candidate markets,
operations/capabilities, access mode, origin group, documentation pointer and
licensing limitations, plus current local `configuration` and `configured`.

- Local readiness is checked through the real private-file/adapter boundary,
  including the optional SDK's installed/parser version. A present key alone does
  not enable traffic. Missing/disabled/unsafe/unacknowledged files remain explicit
  UNCONFIGURED diagnostics; the rest of the application stays usable.
- Candidate `data_rights` remains UNVERIFIED or MANUAL_EVIDENCE_ONLY. An operator's
  terms acknowledgment is not verified licensing or plan coverage.
- Yahoo and yfinance retain the same YAHOO origin, not two independent vendors.
- Manual/HKEX/SEC readiness means cited input can be supplied, not that a document
  was downloaded, verified or counted as another vendor vote.
- `live_validation` remains NOT_TESTED and raw-payload storage remains false.

## Local health and recorded history

`GET /providers/{provider_id}/health` returns:

- `configured`, `state` and `reason`: **local** file/dependency readiness. Ready but
  unprobed profiles are STALE with a bounded reason; unavailable profiles are
  UNCONFIGURED. A historical successful page never promotes this to live health.
- `last_result`: the latest provenance-v1 completed source page, ordered by
  `finished_at,id` descending across securities/operations, or null. It explicitly
  identifies the security, operation and historical provider symbol. It exposes
  bounded timing, requested window, parse/continuation flags, counts, HTTP status
  where actually attempted, safe error code and a query-free source pointer.
- `last_success_at`: the most recent fully parsed OK_WITH_DATA/NO_DATA **page**.
  This can include a first page with a continuation. It is not whole-history
  coverage, canonical dividend adoption or an upstream healthcheck timestamp.
- `last_error_code`: the latest recorded page's bounded error code, if any; it is
  separate from the current local `reason`.
- `quota_remaining=null`: provider billing/reset boundaries are unverified. The
  [software reservation ledger](BUDGETS.md) must not masquerade as an upstream
  account balance or remaining licensed quota.

Local configuration loss/rotation does not erase or relabel historical source
results. These views do not determine whether old credentials still work.
`has_more` is a boolean; the actual upstream cursor, private profile/context JSON,
credential references, rights text, hashes identifying configuration and owner/job
payloads are never returned. No raw provider body is returned.

## Recorded processing is not market coverage

`GET /providers/coverage` lists bounded provider/security pairs. Optional filters
are `security_id`, `provider_id`, `operation` (default DIVIDENDS), and literal `q`
search over ticker/provider ID. `limit` is 1–100. Follow the returned `next_cursor`;
server order is ascending provider ID, then stable security UUID. Unknown explicit
security IDs return 404; invalid providers, operations or cursors return 422.

Each row separates:

- Current local `configuration`, candidate `supported_market` (exchange-derived)
  and `supported_operation`. These do not resolve or prove symbol coverage.
- `status`: the latest provenance-v1 page state for that exact provider/security/
  operation, or NOT_TESTED if none exists. An unconfigured skipped job without a
  completed source page does not invent a provider response or NO_DATA record.
- `provider_symbol`: only the last recorded frozen alias, otherwise null. It is
  not guessed from a display ticker, selected from today's mapping or usable as
  a new fetch authorization. The worker must resolve each complete explicit window.
- `observations_count`: distinct retained source assertions linked by immutable
  sightings, not summed page row counts. Repeated retrievals do not double-count;
  later empty/failed pages do not erase earlier assertions.
- `availability`: retained assertions remain OBSERVATIONS_AVAILABLE after later
  failure/NO_DATA. Without assertions, NO_DATA is DATA_NOT_CONFIRMED, not confirmed
  zero dividends. Unsupported and unavailable remain distinct outcomes.
- `last_result` and `last_success_at`: page-scoped history for the selected
  operation. Split processing never implies dividend processing.
- **`coverage_start=null`, `coverage_end=null`,
  `coverage_basis=NOT_ESTABLISHED`**. Requested windows, parsed pages, completed
  checkpoints and successful source jobs cannot establish authoritative coverage.
  A null end date is unknown, not an empty market-history assertion.

The security/exchange catalogue and source history are installation-wide in this
single-owner product; these are not multi-tenant market-data isolation endpoints.
Brokerage entities and job control remain owner-scoped. No account, receipt,
position, cash, historical FX, portfolio revision or financial total is changed.

## Retained legacy state

Only provenance-v1 immutable pages participate in recorded diagnostics. The
`legacy_runs_omitted` count explicitly reports retained unresolved runs; their
unknown operation cannot be assigned to a dividend/split history. Coverage also
reports `legacy_status_ignored` when an unvalidated retained status row exists.
Arbitrary old status/error/source metadata is neither dumped nor used to fill
coverage/success. GET never backfills, deletes or adopts it.

Full observation listings, cited evidence/review workflows, source UI, automatic
schedules and managed key import/settings are subsequent work. These diagnostics
do not complete canonical reconciliation or the full portfolio application.
Paid/live provider and target-NAS acceptance remains NOT_TESTED.
