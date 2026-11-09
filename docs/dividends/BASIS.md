# Cited split-basis derivation

`spm_api.dividend_basis.normalize_basis` derives comparison-only per-share units
from a SPLIT_ADJUSTED assertion and an explicit cited split-basis declaration. It
is a pure calculation, **not authentication, verified market history, an adopted
corporate action or a financial posting**. The
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
or automatic completeness discovery is supplied by this calculation. The protected
API described below derives its snapshots from real rows, but its recorded-scope
checks do not verify the completeness or contents of external market documents.

## Durable declaration schema

Migration `0014_dividend_basis_assertions` adds one initially empty
`dividend_basis_assertions` table, bringing that schema to 52 business tables.
The later inert candidate revision store brings the current total to 53. Migration
0014 does not adopt old quotations or create a default assertion. The existing pure
calculation remains separate, and the protected API below records an explicit owner
declaration.

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
audit atomically. The protected API authorizes the current session, binds server
identities and commits its idempotent response in the same short transaction.
Owner/request UUIDs supplied to the internal library alone are not authentication.

UPDATE, DELETE and INSERT OR REPLACE reject. A later legitimate action revision or
void preserves the historical declaration, rather than blocking the financial
correction or rewriting history; the old reference cannot authorize a new INSERT.
A future canonical writer must recheck source/action scope at each new adoption,
not treat the existence of a previously stored row as current proof or a replay
grant. The protected API writes/reads declarations; no worker or canonical consumer
is installed.

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
VERIFIED income or a receipt. The private library itself adds no endpoint, default
statement, worker dispatch, canonical matching/revision/lock, financial replay,
entitlement or cash posting. The protected API below wraps declaration storage;
canonical consumers remain separate work.

## Protected declaration API

`POST /api/v1/dividends/basis-assertions` records one explicit cited declaration.
It requires a current authenticated session, matching Origin, CSRF token and
`Idempotency-Key`; this is ordinary owner-authenticated evidence entry, not the
password reauthentication required for sensitive provider credentials. All
responses are `Cache-Control: no-store`.

Obtain the original observation ID and `normalized_sha256` from the source
observation API, and the current physical action IDs and integer versions from
corporate-action reads. The request must explicitly provide all scope fields:

```json
{
  "observation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "source_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "share_basis_date": "2026-01-10",
  "source_basis": "AFTER_EFFECTIVE_DATE_ACTIONS",
  "target_basis": "PRE_EX_DATE_EVENTS",
  "complete": true,
  "split_only": true,
  "actions": [{"action_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "version": 1}],
  "citation": {
    "citation": "Example document, page 2",
    "reason": "Explicitly state the source share units and complete recorded split scope.",
    "url": "https://example.com/example-document.pdf"
  }
}
```

These identifiers and the document are examples only. An empty `actions` array
must be a deliberate completeness assertion, not a missing-data default. The
server derives the security and ex-date from the validated original observation,
reads each real current action's root/date/status and exact ratio, mints the
assertion/citation and request IDs, and rechecks the whole recorded quantity scope
in its writer. Caller-supplied ownership, ratios, statuses or adoption claims are
rejected. No document is fetched and no provider configuration, credential or
upstream request is needed. A changed source/action pin or an omitted current
quantity action fails rather than creating a partial statement.

A successful response is `201` with a declaration `Location`; the exact action
ratios are JSON decimal **strings**. `scope=BASIS_DECLARATION_ONLY`,
`canonical_adoption=NOT_ADOPTED`, `market_history=OWNER_ASSERTED_NOT_VERIFIED`
and `current_use_scope=REVALIDATION_REQUIRED_FOR_NEW_ADOPTION` are explicit
limits. Citation reference and reason are visible to the authenticated owner;
a URL with a query or fragment is withheld with `url_withheld=true`, without
rewriting the stored statement. Do not put secrets in citation text or URL paths.

`GET /api/v1/dividends/basis-assertions/{id}` reads owned historical evidence;
`GET /api/v1/dividends/basis-assertions` lists it with optional `observation_id`,
`security_id`, literal `q`, `limit` and opaque keyset cursor. IDs from another
owner return the same 404 as missing IDs. List order is creation time and ID,
descending; it does not imply that a historical action snapshot is still current.
There is no update, delete or public adoption operation for a declaration.

An unchanged retry with the same idempotency key returns the original 201 and
`Location` after rechecking the current session/CSRF, even if an action was revised
or voided. Within that writer, the cached DTO is also compared with its audited
historical declaration and the normalized original request, not with current action
terms. A changed body using that key conflicts; action-pin order alone does not
change the intent. A **new** declaration must recheck the current recorded scope.
Malformed, mismatched or unaudited cached history fails safely without repair or
echo; audit, assertion and idempotent response commit or roll back together. These
checks are not cryptographic protection against a privileged database writer
replacing multiple mutually consistent records. These operations never create
canonical dividends, entitlements, receipts, cash or action changes.

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
`uv run pytest tests/test_basis_api.py tests/test_contracts.py` checks the protected
HTTP boundary, writer-time session/CSRF rechecks, concurrent and historical replay,
exact ratio strings, scope pins, rollback, owned keyset reads and private-safe
failures. Durable matching decisions, canonical revisions/manual locks and
entitlement/receipt workflows remain separate work. Paid/live provider and target
NAS acceptance remain NOT_TESTED.
