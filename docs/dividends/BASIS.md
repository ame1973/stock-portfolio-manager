# Cited split-basis derivation

`spm_api.dividend_basis.normalize_basis` derives comparison-only per-share units
from a SPLIT_ADJUSTED assertion and an explicit cited split-basis declaration. It
is an internal pure calculation, **not an authenticated proof-import API, verified
market history, adopted corporate action or financial posting**. The
[comparison library](EVIDENCE.md) can use its separate derived view without changing
original observations, their source basis or normalized fingerprints.

## Required inputs, not inferred authority

Supply the original observation UUID and typed assertion. Optional
`SplitBasisAssertion` extends the existing typed citation with:

- its own assertion/citation UUID, bounded reference/reason and optional validated
  HTTP(S) URL; private reference fields are hidden from its repr;
- the exact original observation UUID, canonical normalized SHA256, security UUID
  and known ex-date;
- a **separately supplied local `share_basis_date`** and explicit
  `source_basis=AFTER_EFFECTIVE_DATE_ACTIONS`, `target_basis=PRE_EX_DATE_EVENTS`;
- strict explicit `complete=true` and `split_only=true`, not truthy strings/numbers;
- an explicit tuple of at most 100 `SplitReference` snapshots. Each has physical
  and stable-root action UUIDs, security, observed integer version, effective date,
  directional positive INT64 ratios, CONFIRMED status and CURRENT lifecycle.

There is no default basis date, completeness assertion, split list or 1:1 term.
A deliberately complete **empty** chain is an assertion that no applicable split
exists in that exact scope; it is not the fallback for missing data. Duplicate
physical/root revisions, foreign securities, unallocated/unsupported action types
or out-of-scope dates reject rather than being clipped. A provider-only split
observation cannot stand in for an adopted action reference. UNKNOWN raw basis
cannot be relabeled by supplying a split declaration; AS_DECLARED sources need no
split derivation. Null DPS stays null, and currency/source labels never fill or
convert the event currency.

These are **asserted** snapshots and cited declarations. UUIDs, CONFIRMED/CURRENT
strings and a complete flag are not authentication, real DB currentness or proof
that no other market actions exist. Before durable adoption, the future authorized
controller must load/validate original v1 evidence/hash, establish the source's
actual documented share basis, verify/fence the referenced real action rows and
versions, check relevant non-split/unallocated exceptions, retain the completeness
authority and commit actor/audit/decisions atomically. Merely listing recorded
splits does not establish a complete historical chain. No such public controller
or automatic completeness discovery is installed by this library.

## Durable declaration schema

Migration `0014_dividend_basis_assertions` adds one initially empty
`dividend_basis_assertions` table: 52 business tables in total. It does not adopt
old quotations, create a default assertion, change a source's basis or install a
public basis-import controller. The existing pure calculation remains separate.

Each append-only row retains an active owner, server request UUID, canonical UTC
creation time, original observation/security/ex-date/hash pins and explicit source
share-basis date. Its bounded JSON stores the complete cited declaration and exact
physical/root/version/action-date/ratio snapshots together; there is no second
mutable normalized-DPS or independently editable action-reference projection.
The JSON limit is 128 KiB in **bytes**, with at most 100 references, exact key/type
checks, no duplicate physical/root references and explicit true completeness flags.
Private reference URLs are not a document-verification result or a public DTO.

At INSERT, SQLite checks:

- an existing provenance-v1 SPLIT_ADJUSTED source with the exact stored hash,
  security and known ex-date, its original completed dividend run and first sighting;
- each referenced real action's exact physical ID, stable root, version, security,
  effective date, directional positive INT64 ratio and current CONFIRMED state;
- the same `[X,B]` or `(B,X)` interval used below, including the same-day boundary;
- every recorded current non-void quantity action in that interval. Omitted or
  unconfirmed splits and unallocated cash-in-lieu/merger/spin-off actions reject
  the claim. Foreign securities, outside dates, void rows and display-only symbol
  changes do not invent quantity terms or block an otherwise exact declaration.

These guards check **recorded** facts, not the completeness of external market
history. SQL compares the stored source fingerprint and constrains the declaration
hash's format; it does not cryptographically recompute either JSON fingerprint or
replace the typed citation/URL validator. The internal writer below validates
original v1 content/hash and canonical declaration/hash and commits its creation
audit atomically. A future HTTP route must still authorize the current session,
bind server identities and commit its idempotent response in that same short
transaction. Owner/request UUIDs alone are not authentication.

UPDATE, DELETE and INSERT OR REPLACE reject. A later legitimate action revision or
void preserves the historical declaration, rather than blocking the financial
correction or rewriting history; the old reference cannot authorize a new INSERT.
A future canonical writer must recheck source/action scope at each new adoption,
not treat the existence of a previously stored row as current proof or a replay
grant. Only the internal library below currently writes/reads declarations; no
public route, worker or canonical consumer is installed.

Upgrade preserves all previous source and financial rows and appends one schema
audit per existing owner; a repeat upgrade writes nothing. Late DDL/audit failure
rolls back the whole upgrade. Downgrade refuses to delete history: restore a
validated pre-upgrade backup instead.

## Internal audited writer and historical reader

`spm_api.dividend_basis_service` provides a private library over this schema:

- `save(con, owner_id, assertion, at=..., request_id=...)` requires the caller's
  active-owner writer UoW, an explicit aware clock, server-minted assertion/request
  UUIDs and a revalidated typed declaration. It loads the original source, compares
  the entire recorded current action scope and appends one declaration plus one
  `DIVIDEND_BASIS_ASSERTED` owner/request audit. A late audit failure rolls back
  both. No private credential/configuration file or provider HTTP is accessed.
- An exact repeat of that **internal artifact** returns its original record with
  zero writes, even after later action correction, profile disable or alias edits.
  Reusing its assertion UUID with different owner/body/request/time conflicts;
  it cannot rewrite or reattribute the earlier decision. This is not public HTTP
  idempotency or permission to skip current session/CSRF checks on cached responses.
- `get(con, owner_id, id)` returns that owner's original historical record or
  None, never another owner's declaration or an invented empty proof. It verifies
  canonical JSON, all column mirrors/body digest, original v1 source content/hash,
  immutable physical action terms and exactly one matching creation audit. Raw SQL
  rows without that audit are not adopted. Corruption fails with fixed
  BASIS_ASSERTION_UNAVAILABLE and no echo, repair or financial mutation.
- `require_current(con, owner_id, id)` additionally requires the actual writer and
  active owner, then rechecks the source and complete recorded current action
  scope. Retained references to a legitimately revised/voided action remain valid
  **history**, not a new-use grant. Missing/unconfirmed/unallocated/newly known
  quantity actions or changed physical/root/version/ratio/date terms produce
  BASIS_ACTIONS_CHANGED without writes. Call this inside each future canonical
  adoption transaction, never check in a read UoW and reuse it after a race.

`StoredBasis` is a frozen private record, not a public response contract. Citation,
owner and request fields are hidden from repr. Creation audits bind the source and
canonical declaration fingerprints plus bounded public metadata; they do not dump
document URLs/reference contents. The required reason is retained as an explicit
owner explanation. The audit establishes recorded authorship, **not authentication
of an arbitrary internal UUID or verification of the cited document/history**.

The shared `observation_service.original_dividend` reuses exact source/provenance,
first-run/sighting, timestamp and financial-mirror validation. It also checks
canonical original JSON and returns the original typed assertion, including a
query/fragment-bearing citation URL. Its fingerprint is never reconstructed from
a redacted public observation DTO. Public source lists keep withholding those
links and remain SOURCE_ONLY/NOT_ADOPTED; disabling access or correcting a later
alias does not rewrite historical source identity/content.

A basis statement does not fill null source DPS/currency or make nonrepresentable
rational results valid. Even current recorded agreement is only conditional on the
owner's cited completeness declaration, never complete external market evidence,
VERIFIED income or a receipt. This library adds no endpoint, default statement,
worker dispatch, canonical matching/revision/lock, financial replay, entitlement or
cash posting. A protected basis API and canonical consumers remain separate work.

## Date moments follow the ordinary eligibility contract

The ordinary dividend entitlement basis includes effective events **strictly
before the ex-date**. This differs from end-of-day ledger replay, which applies
opening splits that day. The calculation does not change `eligible_quantity` or
make a same-day exception. Special due-bill/distribution eligibility remains an
explicit review, not a rule inferred from a split or quotation.

Let X be the ex-date and B the declared source share-basis date. A dated source
basis already includes its effective quantity actions on B. For each ratio r=new/old:

| Relation | Exact applicable action dates | Conversion to pre-X units |
| --- | --- | --- |
| B >= X | X <= effective date <= B | Multiply by every r |
| B < X | B < effective date < X | Divide by every r |

Therefore, a split effective on X is included when the source basis is on/after X,
but excluded when converting an earlier basis into the strictly-pre-X target. No
previous-day subtraction, NAS clock, retrieval-date guess or ticker inference is
needed. Year 1/year 9999 and inverse conversions retain the same boundaries.

For example, a synthetic historical `0.60000000` amount on post-3:2 share units
becomes `0.90000000` in the earlier eligibility units. A known pre-event amount
`0.90000000` divided by an intervening 3:2 ratio becomes `0.60000000` in later units.
These are mathematical examples under an explicit complete declaration, not
claims about any provider's undocumented adjustment convention or actual income.

## Exact rational calculation and unresolved results

All source DPS and ratio inputs use existing strict scaled-INT64 codecs. The
entire chain runs as `Fraction` over DPS-e8 integers; no float, premature rounding,
integer truncation or financial minimum-money-unit rule is used. Only the final
rational is checked for exact DPS-e8 representation and INT64 capacity. A fraction
that cancels later in the chain is not prematurely rejected or rounded.

The immutable `BasisResult` contains `dps`, comparison `adjustment_basis`,
`issues` and nullable `assertion_id`:

- Existing AS_DECLARED source terms remain unchanged without an assertion.
- UNKNOWN or unproved SPLIT_ADJUSTED remains UNRESOLVED_ADJUSTMENT.
- A valid complete exact derivation yields AS_DECLARED **comparison units** with
  its assertion ID, not a rewritten claim about the original provider's terms.
- Precision/overflow yields NORMALIZED_DPS_PRECISION/OVERFLOW alongside
  UNRESOLVED_ADJUSTMENT, retaining original DPS and SPLIT_ADJUSTED basis. Positive
  sub-tick amounts are not silently turned into zero. Known zero remains exact.
- Invalid identity/hash/basis/scope/frozen-copy inputs fail with fixed
  INVALID_DIVIDEND_BASIS_ASSERTION, without dumping reference contents.

`EvidenceMatch.basis_assertion` passes this declaration to comparison. Each trace
retains `comparison_dps`, `comparison_basis`, `basis_assertion_id` and any issues;
its original source remains separately readable and immutable. Assertion IDs
cannot be reused for different pinned observations. Missing currency, known date
conflicts and all ordinary comparison guards remain. A basis citation adds no
vendor vote, Yahoo wrappers remain one origin, and the result is never VERIFIED,
RECEIVED or automatic adoption. Conditional normalized agreement is not document
verification or an authorization grant.

## Runnable checks and remaining boundaries

`uv run pytest tests/test_dividend_basis.py tests/test_dividend_evidence.py`
checks exact 3:2/inverse/same-day units, pre-ex-date eligibility consistency, complete
empty versus missing chains, rational cancellation, INT64/precision failure,
source/hash/current-snapshot scopes, frozen-copy validation and conditional
comparison/origin traces. Original financial replay rules and source fingerprints
remain unchanged. `uv run pytest tests/test_dividend_basis_migration.py` checks
source/current-row/complete-chain SQL fences, exact date endpoints, legacy rejection,
immutable history, byte bounds and upgrade rollback.
`uv run pytest tests/test_dividend_basis_service.py tests/test_observation_api.py`
checks internal audited storage, owned historical reads, original-content/hash
admission, genuine action correction/void/replay, new-use fences, concurrent retry,
corruption/unaudited-row refusal, source-link privacy and no private file/HTTP I/O.
Protected HTTP basis authorization/idempotency, durable matching decisions,
canonical revisions/manual locks and entitlement/receipt workflows remain separate
work. Paid/live provider and target NAS acceptance remain NOT_TESTED.
