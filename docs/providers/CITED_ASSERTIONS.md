# Cited source assertions

`POST /api/v1/providers/cited-observations` records one owner-attributed quotation
through the existing manual adapters and immutable evidence store. It is an
installed source-only creation API with responsive quotation forms on the existing
observation pages, **not canonical dividend event creation**.

The reference manual dividend event operation belongs to canonical matching,
verification, entitlements and receipts, which remain separate work. Neither an
official host nor the owner's AS_DECLARED assertion establishes authentic document
contents, VERIFIED income or another independent vendor vote.

## Protected creation contract

Use the current owner session, matching Origin, X-CSRF-Token and an
Idempotency-Key under the [ordinary mutation contract](../../readme.md#owner-authentication).
Fresh password confirmation is not required for a quotation; sensitive source
policy/key changes retain their own reauthentication rules. An internal actor UUID
is never a substitute for HTTP authorization. The writer rechecks the current
session/CSRF before returning a cached result or retaining new evidence.

The body is a required discriminated union with explicit `operation=DIVIDENDS` or
`operation=SPLITS`. A synthetic dividend example follows; replace security, dated
source identity, dates and terms with deliberately reviewed inputs:

```json
{
  "operation": "DIVIDENDS",
  "provider_id": "manual",
  "security_id": "00000000-0000-4000-8000-000000000001",
  "provider_symbol": "0005.CITED",
  "ex_date": "2026-01-08",
  "dps": "0.90000000",
  "currency": "USD",
  "event_type": "UNKNOWN",
  "adjustment_basis": "UNKNOWN",
  "citation": {
    "citation": "Owner-supplied local document, page 3",
    "reason": "Explicit quotation of source terms, not independent document verification.",
    "url": null
  }
}
```

A split body instead supplies `effective_date`, `ratio_numerator` and
`ratio_denominator`, for example exact strings `"1"` and `"3"`. It does not accept
DPS, cash, FX, ledger action status or financial adoption. Positive INT64 ratio
strings preserve every digit; existing strict integer input compatibility remains,
but browser callers must not parse large terms through JavaScript Number.

A successful HTTP201 returns `provider_id`, `security_id`, `operation`,
`observation_id`, `provider_run_id`, `citation_id`, `scope=SOURCE_ONLY`,
`canonical_adoption=NOT_ADOPTED` and `document_verification=NOT_VERIFIED`. IDs are
server-minted, not client-authorized ownership or verification. No Location/ETag
is invented: read the existing [bounded observation lists](OBSERVATIONS.md) by
security/provider and follow their real cursors. No new observation-by-ID route
or canonical event is created.

## Explicit input and access

- Only `hkex_evidence`, `sec_evidence` and `manual` accept these inputs. A quotation
  cannot impersonate a network vendor or supply a trusted origin/hash/HTTP result.
- Dividend ex-date and split effective date are known ISO calendar dates, not
  inferred from retrieval time. Optional dividend dates can remain null.
- A security and exact source symbol must match one explicit dated alias over the
  event's single calendar day. Leading zeros, dots and exchange identity remain
  intact; quote currency is never used as event currency or market identity.
- The existing owned selector requires an active owner and explicit current
  source access/rights. A current disabled or unavailable decision never revives
  an older enabled policy or operator fallback. No key, request budget or provider
  request is needed for a manual source; no access is enabled automatically.
- DPS uses the exact nonnegative eight-place decimal-string codec, including known
  zero and the scaled INT64 maximum. Floats, scientific notation, overprecision
  and overflow reject without rounding. Unknown DPS/currency remain null; UNKNOWN
  basis/type stays unresolved. A frequency label does not imply REGULAR.
- `source_currency` retains a three-letter source label independently of nullable
  event currency and security quote currency. It is not a conversion/FX grant.
- Split ratios always remain PROVIDER_ADJUSTMENT_ONLY, never adopted share terms.
- The citation contains a bounded reference, reason and optional validated HTTP(S)
  URL. Official attribution requires that profile's exact HTTPS host/port. Manual
  citations can be local references without a URL. No document is fetched.
  Clients cannot supply actor, citation ID, provider event ID, raw hash, response
  status, private configuration or financial adoption claims.

## Preparation, writer fences and replay

The route uses `spm_api.providers.cited.prepare` to load/recheck owned
configuration outside writers, resolve the dated identity and run the real manual
adapter with exactly one quotation. Its page retains USER_SUPPLIED_ASSERTION,
DOCUMENT_NOT_FETCHED and NOT_AN_INDEPENDENT_VENDOR_VOTE warnings, with no HTTP
status/pointer/raw hash.

`save` rechecks the prepared terms and exact one-day scope, then reuses current
selection/alias fences and `capture_page` inside the authorized mutation. Context,
completed source run, observation, sighting, capture audit, actor-attributed
USER_CITED_SOURCE_ASSERTED audit and serialized idempotency response commit or
roll back together. The authorship audit carries the minted server request UUID,
not a client-supplied request identity. No private file/HTTP runs in the writer.

Reusing the same committed key and normalized body returns the original HTTP201
and IDs with **zero new source/audit/idempotency writes**, including after later
policy disablement, alias corrections or unavailable private files. Current auth
and CSRF still apply. A different body with that key returns409 before private
preparation. Concurrent equal intents commit one assertion and one receipt.
Advisory replay checks only skip preparation; the writer remains authoritative.
If that receipt disappears in between, return409 CITED_ASSERTION_RETRY_REQUIRED,
never prepare inside a writer or silently adopt retained source facts.

A separately submitted new intent mints a distinct citation/assertion even for an
equal quotation; it is not extra financial income or vendor corroboration. The
underlying internal primitive also supports identical committed prepared-run
replay, but does not itself authorize an HTTP session.

## Responsive quotation forms

The existing dividend/split observation pages host source-only EditorSheet forms.
They require one explicitly added security, source, known event day and exact
source symbol. Optional saved-alias pages use the existing bounded raw catalogue
records; they never guess a symbol or become a prerequisite for committed replay.
No quote currency, date, provider, amount or 1:1 factor is inferred. Shared exact
string codecs validate DPS/ratios without financial Number conversions. The
independent bounded source-frequency label is integer metadata, not financial math.

Input remains local to the open component, not a draft/mutation/persistent cache.
An uncertain unchanged intent survives candidate searches, acknowledgment, the
same alias choice and failed metadata reloads. Actual quoted-field changes create
another intent. Close/Escape is blocked while pending; a confirmed original replay
is not advertised as a newly created/current financial fact. Only source queries
invalidate. See the [workspace walkthrough](WORKSPACE.md#quote-one-source-assertion)
for explicit selection, access setup, retained input and retry instructions.

## Errors and non-effects

Errors are fixed, no-store responses with the server request ID, never echoed
private input/rows/paths:

- 401/403: current session, transport/origin or CSRF rejected.
- 422 VALIDATION_ERROR: missing operation, untrusted fields or inexact/malformed input.
- 422 INVALID_CITED_ASSERTION or CITED_SOURCE_IDENTITY_UNRESOLVED: attribution or
  exact dated source identity cannot support this quotation.
- 409 IDEMPOTENCY_CONFLICT, CITED_SOURCE_CHANGED or CITED_ASSERTION_RETRY_REQUIRED:
  conflicting input, changed access/alias or an interrupted replay boundary.
- 503 CITED_SOURCE_UNCONFIGURED: current source access/rights is unavailable.
- 503 CITED_SOURCE_UNAVAILABLE: evidence/replay cannot be retained/read safely;
  no automatic repair or private diagnostic dump.

No jobs, upstream reservations/outcomes, provider-status projection, canonical
dividends/receipts/actions, portfolio revisions, cash, shares or FX are changed.
Query/fragment citation links remain withheld in public observation views without
changing the original stored assertion/hash. Migration
`0014_dividend_basis_assertions` separately adds an inert declaration store
(52 business tables); this quotation API does not write/select it. Operator
`providers-check` remains file-only with no DB/bootstrap/upstream probe. Canonical
reconciliation and paid/live provider/target NAS acceptance remain unfinished or
NOT_TESTED.
