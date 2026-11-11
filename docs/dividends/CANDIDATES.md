# Review-only dividend candidate suggestions

`spm_api.dividend_candidates.suggest` prepares **one separate candidate seed per
supplied immutable source observation** and bounded pairwise hints for human review.
It does not merge sources, mint stable event IDs, persist matches, verify documents,
adopt dividend terms, create income or write any ledger facts. It is an internal
calculation, not an authenticated HTTP endpoint or a completed dividend workflow.

## Explicit input and separate seeds

Each `CandidateObservation` contains an observation UUID and the original typed
`DividendObservation`. A production caller must load the original validated v1
source content and fingerprint, authorize its actor and recheck any relevant
persisted decision before writing. A caller-provided UUID, citation or normalized
hash is not that authorization or proof that the source row exists. Never rebuild
an original hash from a redacted public DTO.

The function validates the typed source and frozen internal copies, including the
manual/official attribution rules already used by source capture. It rejects
duplicate observation IDs and duplicate `(provider, security, source fingerprint)`
identities, matching the existing source-store uniqueness boundary. No source row,
DPS, event currency, date or basis is changed or filled. Each frozen seed retains
its exact observation ID, computed original fingerprint, provider/origin metadata
and frozen `DividendTerms`. These terms are proposed **source terms**, not a
canonical event revision. Yahoo/yfinance still share one YAHOO origin; manual and
official labels are not extra vendor votes or independently verified documents.

At most 100 observations are accepted, bounding the worst-case output to 100 seeds
and 4,950 hints. Seeds and hint endpoints are sorted by original UUID; input order
does not determine identity, priority or the eventual financial decision. Empty
input produces no seeds or hints, not confirmed zero distributions. Invalid input
fails with `INVALID_DIVIDEND_CANDIDATE_INPUT` without dumping source content.

## Hints are not matches

Hints only relate two observations for the **same security UUID**. The function
never guesses a provider alias, casts a ticker, uses quote currency, infers a
missing date or compares securities based on display symbols. It emits a hint if:

- explicit ex-dates are identical (`SAME_EX_DATE`), or differ by at most three
  calendar days (`POSSIBLE_DATE_MISMATCH`); or
- the same provider reports the same explicit provider event ID for both sources
  (`POSSIBLE_SOURCE_REVISION`), even when dates are unknown or farther apart.
  ID reuse is a reason to review, not proof of one underlying distribution.

Known differing event types and event currencies are surfaced as
`DIFFERENT_EVENT_TYPES` and `DIFFERENT_CURRENCIES`. `DIFFERENT_REPORTED_DPS` means
only that the two original non-null decimal strings differ exactly. It is not an
amount conflict or comparability conclusion: currencies and adjustment bases may
be different, and this suggestion step does not apply the comparison tolerance.
On the same ex-date, a raw DPS difference with an UNKNOWN event type also emits
`AMBIGUOUS_MULTIPLE`. Regular and special distributions on the same date remain
separate seeds; two unknown-type amounts are never summed into one event.

Calendar arithmetic uses the explicit local dates, including year 1/year 9999 and
leap-day boundaries. No retrieval date, NAS clock or payment-date guess is used.
Source-period labels, source currency and incomplete fields remain distinct from
canonical terms. A hint is neither an `EvidenceMatch` nor a request to book income.
Only an explicit, authorized association may later be evaluated by
[the exact evidence comparison](EVIDENCE.md), including its adjustment-basis,
currency and conservative tolerance rules.

## Inert candidate revision storage

Migration `0015_dividend_candidates` adds one initially empty
`dividend_candidate_revisions` table, for 53 business tables in total. It preserves
all prior facts and does not create candidates from old observations or install a
public candidate workflow. Each append-only revision carries a stable candidate ID,
owner/security scope, contiguous revision number, `OPEN` or `DISMISSED` state,
nullable proposed dates/DPS/currency, original adjustment basis, an explicit
bounded list of observation/hash/basis/reason associations, a change reason,
request UUID and canonical UTC creation time. These states are not canonical
`VERIFIED` or `WITHDRAWN` statuses.

The SQLite INSERT fence requires an active owner, unchanged owner/security across
a candidate's revisions, contiguous version and nondecreasing creation time. It
rejects reusing an ID or replacing an existing version; same-security same-date
candidates with distinct IDs are allowed. Dates and scaled INT64 values are
constrained without rounding. An association list may be explicitly empty or have
at most 100 entries, bounded to 128 KiB in **bytes**. Duplicate IDs, malformed or
extra/missing JSON keys, mismatched security/hash, legacy provenance, missing first
sightings and foreign basis references reject. An optional basis reference must
belong to the same owner, security and original observation/hash. Corrections to
later actions do not rewrite earlier candidate or basis history.

This SQL schema is **not** actor/session authorization, a recorded creation audit,
cryptographic recomputation of the source or association hash, or proof that a
basis declaration still matches current actions. Bare SQL rows without a validated
matching audit must not be treated as accepted decisions. The protected API below
uses the controller explicitly; no worker automatically writes or consumes the
candidate table.

Upgrade retains prior rows and adds one schema audit per existing owner; a repeated
upgrade is a no-op. Late DDL/audit failures roll back the upgrade. Downgrade refuses
to delete candidate history: restore a validated backup instead.

## Internal audited decisions

`spm_api.dividend_candidate_service` provides an internal `save`, historical `get`
and writer-only `require_current`. A caller explicitly supplies the candidate and
revision UUIDs, observed next version, proposed terms, each source/hash and reason,
optional owned basis ID, owner UUID, request UUID and aware creation time. The
controller revalidates these inputs, sorts and bounds the distinct associations,
loads the original v1 source content and hash, and requires the exact current owned
basis/action scope for every basis used in a new revision. It appends the revision
and a request-correlated audit with before/after decision fingerprints in one short
writer transaction. It does not infer any association from a hint or ex-date.

The internal owner UUID is **not HTTP authentication**. The protected route below
mints the IDs, rechecks the current session and CSRF inside its writer, fences the
observed version, and commits the serialized idempotency response with the audit.
The internal controller itself is not an API or an idempotency-key receipt. A
replay of the exact same already-audited internal revision returns its original
historical comparison without writes after later action changes; different content,
owner, request or creation time cannot reuse its revision identity.

Reads validate every retained revision's canonical stored fields and exact audit
chain, and validate the selected revision's original source and retained basis.
The candidate's revisions must have one owner/security and a contiguous version
sequence; a corrupt earlier audit cannot be hidden by a later revision, nor can a
corrupt later audit revive an earlier version. New current use and idempotent
historical replay also check the whole audit chain. A corrupt or unaudited row
fails closed without repair or disclosure. This currently scans all retained
revisions for each candidate read, so a history page can repeat that work; use
verified checkpoints or batched validation if measured history sizes warrant it,
not a shortcut that skips audits. The returned
comparison is **historical**, not a claim that the basis is still current or that a
payment exists. `require_current` checks the latest explicit version, OPEN state,
source and full current basis/action scope inside a writer; it does not by itself
verify or adopt terms. A new DISMISSED revision also checks any basis references it
contains. If an old basis has become stale, a caller may explicitly omit that
reference from the dismissal while retaining the original historical revision.
No source sync, financial ledger, event, entitlement or receipt is changed.

## Protected candidate review API

All operations require an owner session and return `Cache-Control: no-store`:

- `POST /api/v1/dividends/candidates` creates one review candidate and returns 201,
  a stable candidate `Location` and version `ETag`.
- `GET /api/v1/dividends/candidates` lists the latest version per owned candidate,
  with optional `security_id`, `state`, literal search and bounded keyset pagination.
- `GET /api/v1/dividends/candidates/{id}` reads the current owned revision;
  `GET /api/v1/dividends/candidates/{id}/revisions` lists retained versions newest
  first with bounded version keyset pagination.
- `PATCH /api/v1/dividends/candidates/{id}` appends a full explicit revision. It
  requires the observed quoted `If-Match`; stale versions return 412. A candidate
  cannot move to another security. There is no delete or automatic adoption on the
  candidate resource; the separate protected event endpoints require an explicit
  distinct-event or existing-event choice.

Create and revise bodies require `state`, explicit `terms`, `matches` and
`change_reason`. Each match supplies the existing observation UUID, its original
`source_sha256`, an optional owned `basis_assertion_id` and an association reason.
An explicitly empty list is allowed. Caller-supplied owner, candidate/revision ID,
version, confidence, verification or receipt fields are rejected. Amounts are
nonnegative exact decimal strings with up to eight places; null currency/date/DPS
remain unknown, and known zero remains zero. The API never fills dividend currency
from a security quote. Same-day distributions require separate explicit candidates.

Writes require `X-CSRF-Token` and `Idempotency-Key`; session and CSRF are rechecked
inside the writer before both a new decision and cached replay. Revision fingerprints
include `If-Match`; unchanged source associations are sorted before fingerprinting.
A completed retry returns its original body, candidate ID and ETag even after a
later action correction, provided the original audited history remains intact.
Before replay, the server checks that the cached DTO and normalized request still
match that owned historical record; it does not repair tampered receipts. A new
revision referencing a stale basis instead returns `CANDIDATE_BASIS_CHANGED` (409).
To dismiss without reusing a stale basis, explicitly omit that match in the new
reasoned revision; old revisions and evidence remain available.

Responses distinguish the stable candidate `id` from physical `revision_id`, and
include exact proposed terms, explicit matches, review reasons and a field-level
historical `assessment`. `CANDIDATE_REVIEW_ONLY`, `NOT_ADOPTED` and
`REVALIDATION_REQUIRED` are explicit: an assessment is not a current action-scope
check, official verification, canonical event, entitlement or payment. The additional
`canonical_adoption_scope=RESPONSE_ONLY_NOT_LINKAGE` means that `NOT_ADOPTED`
describes the review response, not a lookup of current or historical event use. An
older cached response receives the fixed scope label on replay without changing its
original decision or retained idempotency record. Source
payloads, private URLs, owner/request IDs, provider credentials and raw association
JSON are not returned. A malformed or unaudited retained candidate fails safely
with 503 rather than silently falling back or repairing history.

## Checks and remaining work

`uv run pytest tests/test_dividend_candidates.py tests/test_dividend_evidence.py`
checks same-day multiple distributions, nearby-date review, possible provider-ID
revisions, exact amount/currency preservation, UNKNOWN basis, Yahoo lineage,
permutations, frozen-copy validation and the 100-source bound. An actual captured
v1 EODHD mock response is also reloaded from SQLite and passed through the pure
function while the complete database remains unchanged and no provider HTTP client
is constructed by suggestion. This is not a live provider request.

`uv run pytest tests/test_dividend_candidate_migration.py` checks empty upgrade,
preservation/rollback, version fencing, immutable history, source/basis scope,
nullable/exact financial values, malformed JSON and the byte/entry limits.
`uv run pytest tests/test_dividend_candidate_service.py` additionally exercises the
internal audited writer, historical replay, stale action fences, cross-owner and
corrupt-history rejection, concurrent revisions, exact comparison, 100 explicit
matches and an actual mock-HTTP source capture. It does not claim a live request.
`uv run pytest tests/test_candidate_api.py` checks owner/session/CSRF protection,
server-minted identities, exact terms, CAS, concurrent idempotency, historical
replay, tamper rejection, scope corruption, limits and physical keyset pages.

The [protected candidate-adoption endpoints](EVENTS.md#protected-candidate-adoption)
let the owner create a distinct event or explicitly associate the current candidate
with one selected existing event. Both candidate identity and physical revision are
pinned and revalidated; the review response's `NOT_ADOPTED` label is still not a
linkage query. Adoption records an owner decision, not verified market history,
eligible shares, confirmed income or a receipt. The candidate review UI, atomic
entitlement rebuilding, verification/withdrawal and actual receipt workflow remain
unfinished.
