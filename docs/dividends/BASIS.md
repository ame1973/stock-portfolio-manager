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
remain unchanged. Durable basis declarations, real-row writer fences, canonical
revisions/manual locks, entitlement/receipt workflows and their protected APIs/UI
remain separate work. Paid/live provider and target NAS acceptance remain
NOT_TESTED.
