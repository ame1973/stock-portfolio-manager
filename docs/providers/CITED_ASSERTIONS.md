# Cited source assertion core

The internal `spm_api.providers.cited` controller records a human quotation of
source terms through the existing manual adapters and immutable evidence store.
It is **not an HTTP creation API or browser workflow yet**. An internal actor UUID
is not proof of a current session, CSRF authorization or idempotent HTTP intent.
The caller must perform those checks before exposing this primitive to users.

A cited assertion is not the canonical manual dividend event operation. Canonical
event creation, matching, verification, entitlements and receipts remain separate
work. Neither an official host nor the owner's AS_DECLARED assertion establishes
authentic document contents, VERIFIED income or another independent vendor vote.

## Explicit input and access

- Only `hkex_evidence`, `sec_evidence` and `manual` accept these inputs. A quotation
  cannot impersonate a network vendor or supply a trusted origin/hash/HTTP result.
- The operation is explicit: DIVIDENDS or SPLITS. Dividend ex-date and split
  effective date are known ISO calendar dates, not inferred from retrieval time.
- A security and exact source symbol must match one explicit dated alias over the
  event's single calendar day. Leading zeros, dots and exchange identity remain
  intact; quote currency is never used as event currency or market identity.
- The existing owned selector requires an active owner and explicit current
  source access/rights. A current disabled or unavailable decision never revives
  an older enabled policy or operator fallback. No key, request budget or provider
  request is needed for a manual source; no access is enabled automatically.
- DPS uses the existing exact nonnegative eight-place decimal-string codec,
  including known zero and the scaled INT64 maximum. Unknown DPS/currency and
  optional dates remain null; UNKNOWN basis/type stays unresolved. A frequency
  label does not imply REGULAR. Split ratios retain positive INT64 terms and
  always remain PROVIDER_ADJUSTMENT_ONLY.
- The caller supplies a bounded reference, reason and optional validated HTTP(S)
  URL. Official attribution requires that profile's exact HTTPS host/port. Manual
  citations can be local references without a URL. No document is fetched.
  The server mints citation/run identities; clients cannot supply actor, citation
  ID, provider event ID, raw hash, response status or financial adoption claims.

## Preparation, writer fences and replay

`prepare` loads/rechecks owned configuration outside writers, resolves the dated
identity and runs the real manual adapter with exactly one quoted assertion. Its
page retains USER_SUPPLIED_ASSERTION, DOCUMENT_NOT_FETCHED and
NOT_AN_INDEPENDENT_VENDOR_VOTE warnings, with no HTTP status/pointer/raw hash.

`save` requires the caller's writer unit of work and active matching actor. It
rechecks the prepared terms and exact one-day scope, then reuses the current
selection/alias fences and `capture_page`. The context, completed source run,
observation, sighting, capture audit and actor-attributed
USER_CITED_SOURCE_ASSERTED audit commit or roll back together. The authorship
audit carries the caller's server request UUID, not a client-authored identity.
There is no private file/HTTP operation in the writer.

Repeating the same committed prepared run returns its original IDs without any
new source or audit writes, including after later policy/alias changes. A new
preparation instead mints a distinct citation/assertion; identical quotations are
not extra financial events or vendor corroboration. HTTP replay coordination is
not implemented by this internal primitive.

No jobs, upstream reservations/outcomes, provider-status projection, canonical
dividends/receipts/actions, portfolio revisions, cash, shares or FX are changed.
Retained assertions use the existing [exact observation lists](OBSERVATIONS.md),
including query/fragment link withholding without changing the original hash.
There is no new observation-by-ID route or invented Location target.

The schema remains `0013_managed_source_contexts` / 51 business tables. Operator
`providers-check` remains file-only, with no DB/bootstrap/upstream probe. Paid/live
provider coverage and target NAS acceptance remain NOT_TESTED.
