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

## Checks and remaining work

`uv run pytest tests/test_dividend_candidates.py tests/test_dividend_evidence.py`
checks same-day multiple distributions, nearby-date review, possible provider-ID
revisions, exact amount/currency preservation, UNKNOWN basis, Yahoo lineage,
permutations, frozen-copy validation and the 100-source bound. An actual captured
v1 EODHD mock response is also reloaded from SQLite and passed through the pure
function while the complete database remains unchanged and no provider HTTP client
is constructed by suggestion. This is not a live provider request.

Persisted candidate identities and explicit match decisions, authenticated review
APIs/UI, canonical immutable event revisions/manual locks, entitlement rebuilding
and actual receipts remain unfinished. A current observation or a hint is never a
claim of confirmed income or a current-use grant for a split-basis declaration.
