# Dividend evidence comparison

The internal `spm_api.dividend_evidence` library compares proposed terms with
explicitly associated source assertions. It does **not** discover event identity,
merge observations, persist a canonical event/revision, verify a document, rebuild
entitlements or create actual receipts. The canonical dividend HTTP/UI workflow
remains unfinished. Existing [source observations](../providers/OBSERVATIONS.md)
and [quotation forms](../providers/CITED_ASSERTIONS.md) retain their source-only
contract. [Candidate hints](CANDIDATES.md) can identify pairs for review, but do
not create the explicit association required by this comparison.

## An association is an input, not a date heuristic

`assess(candidate_id, terms, matches)` takes a UUID, frozen `DividendTerms` and at
most 100 frozen `EvidenceMatch` inputs. Every match needs the same explicit
candidate UUID, a unique observation UUID, the original typed assertion and a
bounded reason. An optional [split-basis assertion](BASIS.md) must pin that exact
original identity/hash/security/day; its ID cannot be reused for another source.
The security identity must equal the proposed security. Duplicate
observation IDs, cross-security/candidate scope and invalid internal copies fail
with `INVALID_DIVIDEND_COMPARISON`, without dumping source contents.

These pure inputs are not authorization or evidence of an actual stored association.
The [internal audited candidate controller](CANDIDATES.md#internal-audited-decisions)
loads original immutable v1 assertions and their hashes, retains the explicit match
choice, fences current basis/action versions and commits the decision and audit
atomically. A future protected route must still authenticate the current session,
check CSRF and commit its idempotency response in the same writer. Do not hash an
enriched/redacted public DTO as the original assertion or treat a supplied UUID or
reason as HTTP authorization. The pure comparison itself accesses no database,
private configuration, document or upstream; it uses static provider origin
metadata from the registry.

Different distributions may share an ex-date. Nearby dates, equal DPS, a current
ticker or source period alone do not prove the same distribution. This library
only assesses associations supplied by its caller; a known date/type difference
remains a conflict, not permission to silently merge or discard an event.

## Exact known terms and conservative tolerance

DPS remains a nonnegative eight-place decimal string backed by INT64. No float,
scientific notation, input rounding, quote-currency substitution or implicit FX is
accepted. Known zero is distinct from a missing DPS. Dates use the existing strict
calendar codec. Missing optional announcement/record/payment dates and UNKNOWN
event type are not automatically filled or treated as contradictory facts.
Independent source period/currency/frequency labels do not infer event terms.

DPS is compared only within the same explicit currency and common declared/ex-date
comparison basis. UNKNOWN raw basis remains unresolved. A SPLIT_ADJUSTED source
needs an explicit, complete cited [split-only basis assertion](BASIS.md), not a
provider-only factor or a guessed horizon. Its exact result is a separate
comparison view with an assertion ID: original source DPS/basis/hash stay intact.
The library does not authenticate that declaration, prove its market completeness
or silently fill a missing currency/date. Without the assertion, behavior remains
unresolved; nonrepresentable precision/overflow remains an explicit issue.

The required comparison tolerance is an absolute `0.000001` or relative `0.0001`,
whichever is larger. To avoid an input-order-dependent result, apply the formula
in **both directions**, equivalently against the smaller nonnegative amount. In
scaled DPS-e8 integers, with minimum `low` and maximum `high`:

```text
(high - low) * 10000 <= max(1000000, low)
```

This is an exact predicate, not a rounding/conversion rule. Testing the whole
minimum/maximum range also prevents non-transitive chains: two observations may
each agree with a middle proposal while disagreeing with each other. That group
must remain a conflict. Proposed and original amounts are never changed to make
them agree. Known announcement/record/payment dates, ex-date, currency or event
type contradictions are retained even on an otherwise incomplete source.

## Assessment and field-level trace

The immutable result retains:

- `status`: only PROVISIONAL, CORROBORATED, CONFLICT or INCOMPLETE;
- `independent_origins`: eligible supporting vendor origins, not HTTP/page counts;
- `eligible_observation_ids`: complete assertions aligned with the proposed terms;
- `conflicting_fields`: known contradictions across the proposal and source group;
- `incomplete_reasons`: unresolved proposed/source facts and missing support;
- `checks`: stable observation-ID-ordered supporting/conflicting fields and
  incomplete reasons for each assertion, plus `comparison_dps`, `comparison_basis`
  and nullable `basis_assertion_id`. Derived AS_DECLARED comparison units are
  conditional on that cited declaration, not a relabeled original source.

A known contradiction takes precedence over incomplete/provisional/corroborated
classification. Incomplete proposed essential terms or no supporting complete
assertion yields INCOMPLETE. Two independent eligible vendor origins can yield
CORROBORATED; one vendor, or human quotations without a vendor vote, is at most
PROVISIONAL. Yahoo/yfinance share YAHOO. HKEX/SEC/manual quotations are not another
vendor vote, even when their text says "verified" or their basis is AS_DECLARED.
Nothing here yields VERIFIED, WITHDRAWN or RECEIVED, and an elapsed pay date has
no receipt effect.

One incomplete assertion does not erase two other eligible agreeing sources;
its incomplete reasons remain visible. Conversely, its known contradictory date
is not ignored. Group conflicts can exist even when each row's DPS individually
supports the proposal, because tolerance is not transitive. Results and trace
order are invariant under input reordering; no first-provider-wins policy exists.

## Verification boundary

Run `uv run pytest tests/test_dividend_evidence.py tests/test_dividend_basis.py` for
exact successful, contradictory, incomplete, scope, immutable-copy, rational-chain,
INT64/tolerance and order cases.
The authoritative sparse source fixtures are reused only for their intended
properties; any omitted currency/basis needed for a complete synthetic comparison
is explicitly supplied by the test, never inferred in production. The [audited basis writer and protected owner API](BASIS.md#protected-declaration-api)
check original source content/hash and real current action scope, retaining
historical declarations with their authorship audits. They do not trust a stored
row as a new adoption grant. Canonical matching, revisions/manual locks,
entitlements, receipts/reconciliation
and their public workflows still require separate implementation and acceptance.
Paid/live provider and NAS validation remain NOT_TESTED.
