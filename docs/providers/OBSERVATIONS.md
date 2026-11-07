# Immutable source observation reads

These installed authenticated, no-store GET APIs expose normalized provenance-v1
source assertions. They never fetch a provider/document, read current private
configuration, repair history, reconcile a canonical event, create a receipt or
change cash, shares, historical FX or portfolio revisions. History remains
readable after a key/profile is removed; viewing it does not grant new traffic.

All paths below have the `/api/v1` prefix and require an owner session under the
[authentication contract](../../readme.md#owner-authentication).

## Endpoints and bounded filters

- `GET /dividends/observations`: exact nullable dividend source terms, dates,
  currency/basis, independent source-currency/frequency metadata and explicit
  incomplete reasons. A missing DPS is null, not a zero or a quote-currency guess.
- `GET /providers/split-observations`: exact rational source factors, always
  `effect_scope=PROVIDER_ADJUSTMENT_ONLY`, never ledger action adoption. Numerator
  and denominator are positive INT64 **JSON strings**, including every digit of
  `9223372036854775807`; do not parse them through JavaScript Number.

Both support `security_id`, `provider_id`, inclusive `from_date`/`to_date`, literal
`q`, `limit` (1–100) and keyset `cursor`. Date filters apply to ex-date or effective
date, **not retrieval time**. Null ex-dates remain visible without a date filter;
when an explicit date bound is present, they cannot be assigned into that range.
Unknown explicit securities return 404. Invalid scopes/date ranges/cursors return
422 without echoing request inputs.

Server order is descending **first retrieval time, then stable observation UUID**.
Follow `next_cursor`; it uses canonical UTC microsecond timestamps and must not be
replaced with an event-date, provider-coverage or another resource's cursor.
Appending a later sighting does not move an old assertion to another page.
Search treats `%`, `_` and backslash literally and covers provider, frozen alias,
provider event ID and the catalogue display ticker. It does not search private
context/profile/metadata payloads.

## Exact terms and immutable provenance

Each item includes its stable ID, original `provider_run_id`, provider/origin,
security and frozen source symbol/event identity, canonical normalized fingerprint,
query-free source pointer and original `retrieved_at`. `sightings_count` counts
immutable retrieval links for that assertion; `last_seen_at` is their latest
completed time. Repeated source terms preserve the first row/run/hash/time, while
changed terms or citations remain distinct rows. They are not added together as
financial income.

`dps` uses exact eight-place decimal strings. Unsupported/missing event currency
remains null even when the security trades in USD/HKD; `source_currency` retains
an explicit recognized three-letter source code separately. Source period or
frequency is not automatically REGULAR. Null dates and UNKNOWN/SPLIT_ADJUSTED
basis remain unresolved. Yahoo/yfinance still share YAHOO and manual/official
citations are not another independent vendor vote.

Every item declares `provenance_version=1`, `scope=SOURCE_ONLY` and
`canonical_adoption=NOT_ADOPTED`. These are source assertions, not VERIFIED income,
owner receipts or a complete-market-coverage report. See
[provider diagnostics](DIAGNOSTICS.md) for separate local/recorded availability
and [source storage](EVIDENCE.md) for immutable capture/replay behavior.

The reader validates the bounded normalized assertion, canonical hash, original
run identity/operation and exact mirrored financial/source fields before returning
it. Inconsistent or oversized v1 evidence fails closed with 503
`SOURCE_EVIDENCE_UNAVAILABLE`; it is not rounded, silently skipped or repaired.
Raw provider bodies/hashes, arbitrary metadata JSON, configuration/profile/alias
JSON, credential references, private owner/job payloads and upstream cursors are
not returned. Reads have no source/financial/audit/idempotency writes.

## Cited documents and withheld links

A typed citation retains its local ID, reference and user explanation. A plain
HTTP(S) document URL can be returned as an unverified reference, never downloaded
or validated by this API. A URL containing a query or fragment is withheld in the
public presentation: `url=null`, `url_withheld=true`. Use the citation identity and
reference to review the original document; the API does not rewrite a link into
an apparently equivalent document or expose private query tokens.

The original assertion and its fingerprint are **unchanged**. The fingerprint
identifies the stored original, not a newly hashed/redacted response DTO. Official
host attribution and an AS_DECLARED user assertion are not document verification,
canonical VERIFIED status or proof of receipt.

## Unresolved legacy history

Page responses explicitly declare `scope=IMMUTABLE_SOURCE_V1` and include
`legacy_records_omitted`. Retained provenance-0 dividend rows matching the filters
remain in SQLite but are not serialized as trusted new assertions. Their old IDs,
financial encodings, private URLs and arbitrary metadata are not guessed, dumped,
backfilled or deleted. This omission count is not an all-record total or a claim
that no other dividends existed. Source-only split history starts with v1 storage.

This is installation-wide security/source history for the single-owner product,
not a multi-tenant financial authorization model. Owner-scoped brokerage/job
boundaries are unchanged. The [source workspace](WORKSPACE.md) reads these exact
assertions without adoption. The [protected cited assertion API](CITED_ASSERTIONS.md)
can append current-owner quotations with atomic audit/idempotency and original
replay; citation-write forms and canonical reconciliation/review remain unfinished. Explicit schedules and
managed policy/key settings are installed separately; none adopts financial
terms. Paid/live provider and target-NAS validation remains NOT_TESTED.
