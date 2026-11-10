# Dividend event integrity foundation

Migration `0016_dividend_event_controls` added database safeguards for a future
owner-controlled dividend workflow. It added two initially empty provenance tables
and brought that schema revision to 55 business tables. It neither adopts a source observation
nor installs an event, entitlement, receipt or reconciliation API. An ordinary
source sync still cannot create canonical income or mark a dividend received.

## Identity, decisions and evidence

A managed event uses an explicit stable UUID and an owner control record. Two
separate distributions can share the same security and ex-date; neither a ticker
nor a date identifies an event. The original event revision and each subsequent
revision retain their exact dates, currency, scaled DPS and decision. A managed
revision is published only when its numbered successor, explicit decision,
selected evidence links and one correlated revision audit are present. Concurrent
writers cannot publish two revisions at the same version.

A `CANDIDATE` decision references one current, owned, open candidate revision for
the same security and exact proposed terms. Every explicitly selected observation
must have its own v1 source link; a candidate already associated with another event
cannot silently create a second event. A candidate cannot replace a locked or
withdrawn current revision. `MANUAL`, `VERIFY` and `WITHDRAW` decisions require an
explicit reference. Verification retains identical terms and requires a manual
lock and `AS_DECLARED` basis; withdrawal retains the previous terms and lock.
These are storage guards, not document verification or independent vendor votes.
The migration also closes SQLite `INSERT OR REPLACE` paths that could otherwise
replace existing cash entries, audit records, dividend revisions or source
observations even when their update/delete triggers reject ordinary changes.
Normal source deduplication keeps the original observation and adds a sighting;
it does not replace the observation.

The migration leaves existing events, receipts and historical FX unchanged and
marks existing events as legacy provenance; it never silently binds them to an
owner or managed source decision. Immutable managed event controls, decisions,
revisions, evidence and entitlement records cannot be deleted or replaced. A
receipt remains attached to its original entitlement; its actual amount, tax,
fees, currency, date and historical FX cannot be rewritten or reparented. Its
only managed update is a versioned `ACTIVE` to `VOID` transition with the matching
exact same-effective-date cash reversal and correlated owner audit already present. Unknown USD FX
remains null; signed or cross-currency cash is not silently converted or treated as
ordinary same-currency income.

If an event with active historical receipts changes terms or is withdrawn, the
publication guard requires an open reconciliation review for every affected
account, with the correct portfolio and previous/new physical revision IDs.
Receipts from all earlier revisions are included. A verification that does not
change terms, or a receipt already voided, does not itself create a difference.
This check does **not** calculate a reconciliation or rebuild any entitlement.

## Audited historical reader

The internal `dividend_event_service.get` reads a managed event only for the
supplied owner and does not authenticate that owner. For a retained version it
validates the whole revision chain, including later revisions, before returning
the requested historical record. Missing, inconsistent or tampered decisions,
links, audit fingerprints, candidate histories or original source/basis records
fail closed with a fixed error; the reader never repairs them. Versions must be
contiguous integers, timestamps canonical UTC and chronological, and each
physical revision must have its own correlated audit.

The revision audit's canonical before/after values contain the stable event and
physical revision IDs, revision number, exact terms, review status, manual lock,
decision kind, candidate revision ID, sorted evidence field links and creation
time. The private citation and source URL contribute a separate canonical hash
rather than their plaintext to that fingerprint. Owner, request and reason also
must match the audit row. A candidate decision is checked against its original
audited candidate and assessment, including each source's supporting fields.
Manual decisions cannot manufacture corroboration or verification; unresolved
adjustment terms remain incomplete, and verification is a separate explicit
locked decision. Verification and withdrawal preserve the prior terms, while
withdrawal also preserves the prior lock. Only an explicit manual revision can
follow a withdrawal.

This is a historical integrity check, not a current-use grant: a later candidate
dismissal or action correction leaves a valid historical record readable, but
new adoption must independently revalidate the current candidate and recorded
basis/action scope. Internal records and citations are not public DTOs; future
routes must authenticate the caller and withhold private URL details. The reader
performs no writes, provider requests, credential-file access, entitlement
calculation or receipt posting. No canonical event HTTP endpoint is installed.

## Audited internal decision writer

The private `dividend_event_writer.save` appends an explicitly identified manual,
candidate, verification or withdrawal decision in an existing writer transaction.
It checks the active owner, retained event history and expected next version;
it does **not** authenticate an HTTP caller. A new candidate decision resolves
one current open owned candidate, rechecks its original sources and current
recorded basis/action chain, derives its exact terms, assessment and per-source
supporting fields, and cannot reuse that candidate for a different event. A
manual decision requires explicit terms and a cited reference. Unknown or
split-adjusted manual terms stay incomplete; only an explicit separate
`AS_DECLARED` verification locks unchanged terms. Withdrawal preserves the
prior terms and lock, and reopening it requires a new explicit manual revision.
These are owner decisions, not independent verification of an external document.

The writer inserts the event revision, source links, correlated audit, affected
owner-portfolio invalidations and any conflict/incomplete evidence reviews in
one transaction. Reviews are attached to affected portfolios, or to the event
when none exists. An explicit later decision resolves outstanding reviews only
after rechecking their original event revision, canonical detail, owner and
creation audit; corrupt review history aborts the whole transaction. Historical
replay of the same internal revision identity, request and timestamp is zero-write
even after a later source/action change or receipt. A new candidate use is not
such a replay and must pass current-scope checks again.

**Projection safety fence:** if *any* prior revision of an event has an
entitlement, this internal writer rejects a new revision—even a verification with
unchanged terms—until an atomic projection rebuild is installed. Repointing the
current event without replacing the applicable projection could otherwise hide
old entitlements and receipts. Initial decisions only invalidate affected
portfolios; they do not calculate entitlements or create actual receipts. A
protected API must not be exposed until it can atomically maintain those
projections and reconciliation, and the receipt workflow remains separate.

Source-observation responses currently describe their own source-only payload;
their `NOT_ADOPTED` marker is not a query for later event evidence links. Before
exposing event adoption, make the public source and candidate views unambiguous
about any current or historical event linkage. No internal decision invokes a
provider, reads credential files or verifies a cited document.

## Recorded account-share calculation

The internal `dividend_eligibility.recorded_eligibility` calculates an
account-by-account view from **one portfolio's supplied recorded facts**. The
caller must authorize the owner and portfolio, verify every trade and both sides
of each transfer belong to that portfolio, and select current adopted actions;
the pure function neither reads the database nor authenticates those inputs.
Provider-only split observations are not adopted corporate actions.

For the explicitly selected ordinary rule, an ex-date already reached uses only
facts strictly before that date, including recorded transfers and previously
confirmed rational splits; all ex-date events are excluded. Before the ex-date,
the function instead reports recorded holdings through the supplied exchange-local
as-of date, not a forecast or established entitlement. The caller must explicitly
select review rather than the ordinary rule for special distribution, due-bill or
other unknown eligibility conventions. It must also supply the local calendar date;
the function never guesses it from the server clock.

Results preserve the requested account order and exact eight-place share strings.
A recorded zero means no shares were found in the supplied facts, **not** proof of
complete ownership history. Every result is marked `RECORDED_SHARES_ONLY` and
`UNVERIFIED`; no entitlement, tax, FX, gross/net payment or receipt is calculated.
An unresolved action affecting held shares, or a split producing an unrepresentable
fraction, returns `NEEDS_REVIEW` without a quantity. Transfer-connected accounts
are conservatively reviewed together, including connections through accounts not
requested in the result; independent account histories can still be reported.
Connections are intentionally undirected across the requested historical interval,
so review can extend to an account even if a transfer preceded the action. Invalid
inputs and computable oversold histories fail closed; an unresolved component never
publishes a guessed quantity.

The pure calculation does not itself read a database, authenticate a caller,
calculate an entitlement or post a receipt.

## Owned recorded-history reader

The separate internal `dividend_eligibility_service.get` loads one explicitly
identified owner's portfolio and security within the caller's existing database
transaction. It requires an active owner, returns no portfolio for a foreign or
missing identity, includes active and archived accounts, and validates account,
broker and portfolio ownership. Results are ordered by account identity and
carry the requested ex-date, exchange-local as-of date, explicit rule and the
portfolio's observed `data_revision`. The latter is a snapshot value, **not** a
current-use grant or a substitute for rechecking all facts. Authentication and
CSRF still belong to a future protected caller.

For relevant trade and security-transfer roots, the reader checks retained
revision and predecessor continuity even when a later revision moved to another
portfolio or security owned by the same owner. It validates both sides of each
transfer, action security identity, current/void lifecycle, canonical dates,
rational directions and the real ledger sequence's kind and stable root. A
missing current successor cannot silently turn shares into zero. Actual active
facts, current adopted splits and unresolved current actions are then read by
the existing ledger replay; source-only factors are never adopted.

The reader requires one actor/request-correlated `CREATE` or `REVISE` audit per
retained fact. It checks the canonical after fingerprint of every retained
security transfer and corporate action, their reconstructible revision/void
before fingerprints, each current non-void trade's canonical after fingerprint,
and each terminal void's status/version audit. Action review flags may have
changed independently since the original action; both possible historical flag
values are checked without changing the fact. Facts lacking the required audits
fail closed rather than being silently adopted as verified history. These checks
are against the retained database records; hashes are not signatures or proof
of an externally authenticated actor or complete market history.

**Retired-trade evidence:** retiring a trade overwrites its original `updated_at`.
When an immutable original-status/timestamp origin is retained, the reader checks
its CREATE/REVISE after fingerprint and compares its reconstructed raw-row before
fingerprint with the actual successor REVISE or terminal VOID audit. It checks
origins for current rows too, and rejects an inconsistent or unsupported claim
instead of repairing it. If every relevant retired trade has a matching origin
and retirement hash, the scope is `RETIRED_TRADE_HASHES_MATCHED`; if none are
retired, it is `NO_RETIRED_TRADES`.

When any relevant retired trade has no origin, the reader reports
`RETIRED_TRADE_HISTORY_UNVERIFIED`. It still checks all other available origins
and reconstructible hashes, so missing legacy evidence cannot hide a mismatch in
another chain. Without an origin, a valid-shaped alteration of the old trade's
original after or retirement before hash may remain undetected. The reader never
guesses a timestamp or treats an expiring idempotency cache as durable proof.
Matched hashes describe consistency of retained records only: they are not a
signature, authenticated external evidence, verified market ownership, complete
history or a financial-use grant. All account results still carry
`history_completeness=UNVERIFIED`.

The reader performs no writes, provider requests or credential-file reads. It
is not an entitlement projection, event/receipt API or UI. Before any economic
use, a writer must revalidate current owner, facts, action scope and the audit
limitation in the same transaction as the financial changes; an earlier read or
`data_revision` alone does not authorize adoption. Root-history and audit scans
may need indexed batching if measured portfolio histories grow.

## Inert trade-audit origin storage

Migration `0017_trade_audit_origins` adds one initially empty append-only table
without changing existing trade rows, cash, receipts or audit events. It brought
that schema revision to 56 business tables. A separate table matters because historical
trade retirement audits hash the entire old trade row; adding a trade column
would change the set of values needed to reconstruct those existing fingerprints.
No origin is automatically backfilled or recovered by this migration. The trade
writer captures new physical rows and eligible current legacy rows. The
recorded-history reader checks retained origins when present and preserves the
explicit unverified scope for older rows with missing evidence.

An origin can retain a physical trade's claimed original status and `updated_at`,
its owner and exact CREATE/REVISE audit identity. Current-row claims must match
the currently stored status, timestamp and version. A pre-retirement capture or
retained-response recovery additionally requires a separately correlated audit;
recoveries are only permitted for a retired trade. SQL checks ownership, account
and broker scope, actual ledger sequence kind/root, one matching creation audit,
canonical metadata shapes, capture mode and immutable uniqueness. It rejects
updates, deletes and replacement writes, but **cannot verify the canonical
content of an audit hash or an old response**, authenticate an actor, or prove
when a claim was captured or that its prerequisites shared a transaction. In
particular, a well-shaped recovered timestamp can still be false. The mode named
`AT_CREATION` is a stored claim, not database proof of capture timing.

The trade writer verifies the original audited DTO before recording it, and
captures the original and financial changes atomically before retiring a
still-current trade. An older current trade with no original creation/revision
audit returns 409 `NEEDS_REVIEW`; an inconsistent audit or stored origin returns
503 `TRADE_AUDIT_UNAVAILABLE`. These failures do not change the fact, cash,
origin, audit, idempotency or invalidation. An explicit review workflow for a
current trade with a missing creation audit remains unfinished, so the operator
cannot yet resolve that blocked correction in the app. Recovery of an
already-retired trade needs retained original response evidence, immutable field
mirrors and an exact match to the old audit digest, with a separately audited
authorized workflow. An expired or missing response is not evidence: leave the
history unverified and require explicit review rather than inventing a timestamp
or rewriting the trade/audit. A stored origin is not by itself financial adoption
or an eligibility authorization.

## Internal trade-audit origin verifier

The separate `trade_audit` helper can read one explicitly owned origin or retain
one in a caller-supplied writer transaction. `remember_created` matches a current
trade's original DTO and request to its CREATE/REVISE audit before inserting an
`AT_CREATION` claim. `capture_before_retire` verifies the current physical trade
and its original audit first, then appends a canonical capture audit and origin
in the caller's transaction. An existing valid origin is reused without writes.
The caller is responsible for placing these operations in the same transaction
as the original creation or retirement; invoking the helper later cannot prove
when a claim was first captured.

A read checks the owner, account/broker and ledger-sequence scope, immutable trade
values, exact original DTO fingerprint, capture metadata and any capture audit.
It reconstructs the original raw-row `before_sha256` needed to compare a later
revision or void audit. Recovered origins also recheck their immediate successor
or void audit; other origins and the whole trade chain still require the caller's
separate checks. None of these checks authenticate an HTTP session, establish
complete ownership history or authorize financial adoption. Hashes are not
signatures. A malformed or inconsistent origin fails with a fixed private error;
there is no provider request or private-file read.

The protected trade writer invokes this helper in the same transaction as
trade creation, revision or void, alongside settlement, audit, idempotency and
invalidation. Existing verified current rows without origins receive a correlated
pre-retirement capture; completed historical HTTP replays remain zero-write.
The dividend eligibility reader checks each available origin, compares retired
ones with their actual retirement audit, and labels absent evidence as unverified.
Neither component enables an economic projection.

## Internal retained-response recovery

`recover_from_response` accepts an explicitly supplied original `TradeRecord` only
for a retired owned physical trade. It compares every public response field with
the immutable stored fields reconstructed at the claimed original status and
timestamp, checks the old CREATE/REVISE after fingerprint, and verifies the raw
predecessor fingerprint against the actual successor REVISE or terminal VOID
audit. Missing, unrelated or conflicting evidence is rejected without inventing a
timestamp or modifying any old trade, cash entry or audit. An owner, transaction,
reason and request identity are required; one new recovery audit and immutable
origin are appended in the caller's writer transaction. The audit binds both the
original response fingerprint and a digest of the supplied reason. Repeating the
same internal request returns its original result without writes; a different
request cannot replace an existing origin.

Recovery changes the recorded audit scope even though the economic facts are
unchanged. The same transaction increments the data revision and appends an
invalidation for every owned portfolio in the trade root, from the earliest
recorded trade date in that root. This matters when a later correction moved a
trade to a different portfolio or security. Any failure must roll back the audit,
origin and all invalidations together. A valid-shaped SQL claim or audit cannot
prove that an authenticated user submitted a response; retained hashes are not
signatures or external statements. Missing old creation audits remain unresolved.

The helper alone is an **internal primitive** and does not authenticate a caller.
The protected API below supplies that boundary; neither it nor the helper
recovers an old origin automatically or creates an entitlement, receipt or verified
ownership. Missing evidence stays unverified.

## Protected trade-audit recovery API

`GET /api/v1/trades/{id}/audit-origin` returns an owner-scoped physical trade's
version and status, origin mode (`MISSING`, `AT_CREATION`, `BEFORE_RETIRE` or
`RETAINED_RESPONSE`) and capture time if one exists. The response scope is
`DATABASE_AUDIT_ORIGIN_ONLY` and history completeness is always `UNVERIFIED`.
`MISSING` does not establish that a creation audit or recoverable response exists.
A malformed retained origin fails closed with 503; foreign trades return 404.

`POST /api/v1/trades/{id}/audit-origin/recover` requires the complete originally
returned `TradeRecord` in `original_response`, a reason, current session and CSRF,
a recent password confirmation, an idempotency key, and `If-Match` containing the
quoted **physical trade version** from the read or trade endpoint. Only a retired
physical trade is eligible; a response or audit that cannot be matched returns
409 `TRADE_ORIGIN_REVIEW_REQUIRED`. An already retained origin cannot be replaced.
The resulting response contains origin metadata only, not the supplied financial
response. It is not a financial adoption, complete chain verification or proof of
external ownership. Neither operation makes provider requests or reads private
provider files.

The writer rechecks session, CSRF and recent password confirmation inside the
transaction before both new writes and cached replay. Recovery audit, origin,
portfolio invalidations and the idempotency receipt commit together; response
serialization failure rolls them all back. A completed retry with the same key,
version and normalized body returns the original response with no new writes,
even after a later successor correction. Replay also revalidates its cached
metadata and original intent against the retained audited origin. Both operations
are private and `no-store`. An absent original creation audit needs the separate
explicit unverified review API below; that review does not yet unblock correction.

## Browser recovery workspace

In **Transactions** (`/transactions`), include historical revisions, expand the
relevant physical trade and open **Trade audit origin**. A missing origin is not
proof that the original response or audit exists. For a missing origin on a
superseded or void trade, the recovery sheet accepts the complete original JSON
response, a reason and an explicit acknowledgment. It asks for a recent password
confirmation separately; confirming the password alone does not recover anything.
The backend compares the supplied response with the recorded creation and
retirement fingerprints. No old trade, cash entry or audit is rewritten.

Paste the original response as received, not a reconstructed current trade or a
JSON object reserialized by a program using binary floating-point numbers. A
`created_sequence` can exceed JavaScript's safe integer range. The form validates
JSON and identity, but sends the original JSON text unchanged so its integer digits
survive. Do not paste passwords or provider keys. The input and retry key remain in
form memory, not browser storage or the query cache; closing or reloading discards
them. If the response is uncertain, keep the sheet open and retry the unchanged
input after password confirmation if needed. The observed physical version and
operation key stay fixed; editing the response or reason creates a new intent.
A stale version requires closing, reloading and reviewing the current state. An
already retained origin cannot establish that a different uncertain request caused
it. A physical version outside the browser's safe integer range cannot be submitted
from this form; inspect the exact server response and use the protected API rather
than guessing a version.

The result reports consistency of retained database records only. It is not an
external ownership statement, complete holdings history, entitlement or receipt.
If the original response or creation audit is missing, do not invent one; the
separate explicit review API below remains unverified and has no browser workflow yet.

## Inert unverified trade-history review storage

Migration `0018_trade_history_reviews` adds an initially empty append-only review
decision table, bringing the current schema to 57 business tables. It does not
backfill a review, change existing financial facts or modify old audits. A
physical trade can retain consecutive `ACKNOWLEDGE_UNVERIFIED` and `WITHDRAW`
decisions, with an explicit missing-creation-audit or missing-retired-origin gap,
the status/version/update time observed at the decision, a claimed snapshot hash
and a correlated owner/request audit. A later decision must refer to the preceding
revision; a withdrawal preserves that preceding gap. Later matched-origin recovery
can coexist with the history and an old acknowledgment can then be withdrawn.

Database guards check active ownership, account/broker/portfolio and ledger-sequence
scope, the observed current physical state, decision alternation, gap conditions,
audit metadata and bounded identity/time/hash shapes. Updates, deletions and
replacement writes are refused. They do **not** authenticate a session, validate
the canonical snapshot or audit fingerprint, establish a complete trade chain, or
prove that a user saw or accepted a gap. Even a valid-shaped false snapshot hash
can pass SQL. A bare SQL row does not unblock a trade correction or cause an
eligibility projection. Missing or corrupt evidence remains unresolved, and a
review is never a recreated creation audit or proof of ownership.

## Internal unverified trade-history review controller

The private `trade_history_review` controller can append, read and withdraw an
explicit review in a caller-owned transaction. It validates the active owner,
physical trade and ledger scope, the complete review decision sequence, and each
correlated audit's canonical before/after fingerprint and reason. It checks the
actual observed trade snapshot rather than trusting a valid-shaped stored hash. If
a trade reviewed while current was later retired, it reconstructs only the
observed status, version and timestamp and checks the actual successor or void
before fingerprint. A later creation audit must match that reconstructed original
response; a review made after retirement cannot invent the original timestamp.
Matching fingerprints describe database consistency, not authenticated external
evidence or proof of when an audit was created.

`save` records the decision and its audit and invalidates every owned portfolio in
the trade root from its earliest trade date in one writer transaction. A savepoint
rolls back partial changes even when the caller catches a failure. A completed
internal request replays its original decision without writes after later valid
changes. `require_current` rechecks the current physical version and the latest
acknowledged gap; if a missing creation audit or origin later appears, the old
acknowledgment is no longer a current-use review, even when the later record
matches. It does not treat a later audit as proof it existed at creation.

These helpers do not authenticate an HTTP session or validate the entire trade
root, ownership history or economic scope. The protected API and browser workspace
below authenticate review decisions; no trade-writer grant or eligibility
integration consumes them yet. A future financial writer must consult the review
history even when an audit later appears, and must revalidate the complete relevant
scope in its own transaction. No provider or private-file access is performed.

## Protected unverified trade-history review API

`GET /api/v1/trades/{id}/history-review` returns the owned physical trade's
version, status, exact review revision as a decimal **string**, current evidence
gap, latest decision and review state. `NOT_REVIEWED` means a gap is present;
`NOT_REQUIRED` means this particular check found no reviewable gap, not that
ownership or the complete chain is verified. `ACKNOWLEDGED_UNVERIFIED` identifies
the latest acknowledgment of the same still-current gap; `WITHDRAWN` identifies a
withdrawn decision. `EVIDENCE_CHANGED` means a later audit or origin changed the
gap after an acknowledgment, even if its fingerprints match. All responses retain
`history_completeness=UNVERIFIED` and `scope=UNVERIFIED_TRADE_HISTORY_REVIEW_ONLY`.
The paginated `GET /trades/{id}/history-review/revisions` returns the actual
audited decisions in descending revision order; its optional search is restricted
to decision IDs, not private reasons.

`POST /trades/{id}/history-review` requires a current session, CSRF token,
idempotency key, recent password confirmation and `If-Match` with the quoted
physical trade version. The body names an explicit decision, the last observed
review revision as a canonical decimal string, the exact observed gap, and a
reason. The reason is retained in the audit and returned to the authenticated
owner; do not include passwords or provider keys. For example, after reading a
first missing-creation-audit gap on physical version 1:

```json
{
  "decision": "ACKNOWLEDGE_UNVERIFIED",
  "expected_revision": "0",
  "expected_gap": "MISSING_CREATION_AUDIT",
  "reason": "Synthetic example: the original audit is unavailable"
}
```

For withdrawal, use `WITHDRAW`, the current review revision and the last decision's
gap, even if current evidence has since changed. Review revisions are strings so
their INT64 digits survive JavaScript; do not parse them through a floating-point
number. A physical/review version mismatch returns 412. A changed evidence gap
returns 409 `TRADE_REVIEW_CHANGED`, rather than recording an acknowledgment of a
different gap. An absent reviewable gap returns 409; invalid or contradictory
retained audits and forged cached decisions fail closed with 503. Foreign trades
return 404. Responses are private and `no-store`.

The current session, CSRF and reauthentication are rechecked inside the writer
before a new decision or idempotent replay. Review, audit, portfolio invalidations
and the serialized idempotency response commit together. The same key and unchanged
intent replay the original audited decision without writes, even after a valid later
withdrawal, retirement or matching-origin recovery. Replay rechecks the cached DTO
and original version, decision, gap and reason against the full retained review
history. The response's `observed_trade_version` is the **original observed physical
version**; reread the status for current state. Review responses do not issue an
ETag for the combined trade/review state; use the status's `trade_version` or the
trade endpoint's ETag for the next physical `If-Match`. The response omits owner,
audit/request IDs, raw trade snapshot and snapshot hash. Neither this API nor a
bare SQL row unblocks trade PATCH/DELETE, establishes verified ownership, creates
an entitlement or posts a receipt. The separate writer/eligibility integration
remains unfinished. **Known limitation:** if a missing creation audit is later
inserted with matching contents, the current trade writer can accept a correction
or void without proving when that audit was created, including after withdrawal.
Do not rely on this path until the writer's review-history fence is installed.

## Trade-history review in the browser

On `/transactions`, expand a physical trade's **unverified trade-history review**
section to load its current status and, separately, its paginated decision history.
A reasoned acknowledgment or withdrawal requires recent password confirmation and
an explicit scope acknowledgment. The editor freezes the observed physical version,
exact review-revision string and gap. Withdrawal retains the last decision's gap
even when later evidence has changed. The browser does not guess unsafe versions or
treat a failed status read as verified history.

If a response is uncertain, keep the editor open and retry the same normalized
reason: its original idempotency key and observed intent remain in memory, including
after password confirmation. Changing only leading or trailing whitespace does not
change the submitted reason or key. Closing the editor discards the unsaved input
and key; if it was closed, inspect the decision history before making another
choice. Passwords, reasons and request bodies are not saved in browser storage. A
review is not ownership, complete holdings, income or permission to revise or void
a trade, and it does not post cash or dividends.

## Required future protected writer and projection work

These constraints are defense in depth, not authentication, authorization,
cryptographic validation or an application workflow. SQL checks stored source
pins and audit hash *shapes*, not their canonical content. The future protected
writer must recheck the active session and CSRF, original source/candidate audits,
canonical hashes, current basis/action scope, explicit field-level provenance and
manual authority inside its own short transaction. It must commit its decision,
financial projections, audit, idempotency response and invalidations atomically;
no provider HTTP or private-file reads belong in that transaction. A row or an
existing audit alone does not grant adoption. The database checks presence at
publication but cannot prove that every prerequisite was created in the same
transaction or that an entitlement amount was calculated correctly.

The reference schema has `UNIQUE(revision_id, account_id)` for entitlements.
Managed entitlement snapshots are protected from in-place rewrites, so this
schema alone cannot retain multiple projection versions for the same event
revision and account. A subsequent projection-history migration and rebuild
controller are required before supporting corrections to eligibility within one
revision. They must preserve old receipt links, aggregate active receipts by
stable event/account/currency across revisions, and treat cross-currency or missing
FX as review rather than inventing a rate. For example, an expected 10 USD with
6 USD received, later accepted as 11 USD, leaves 6 received and 5 outstanding;
the old and new expectations must not be added together.

Receipt creation still requires a protected writer to insert the actual receipt,
its cash posting, audit, idempotency record and invalidation together. Likewise,
a void writer must insert the exact reversal and matching audit before changing
the receipt state, all within one transaction. The SQL transition guard cannot
prevent a direct writer from committing an isolated receipt or reversal without
using that workflow. No receipt posting or void HTTP endpoint is installed yet.

## Verification

`uv run pytest tests/test_trade_audit_origin_migration.py tests/test_trade_audit_service.py tests/test_trade_origin_writer.py tests/test_trade_audit_legacy_guard.py`
additionally exercises upgrade preservation, atomic rollback, scoped immutable
origin storage, canonical origin verification, actual trade-write transactions,
concurrency and the distinction between valid-shaped stored metadata and a verified hash.
The test does not turn SQL metadata into an authorized recovery workflow.

`uv run pytest tests/test_trade_audit_recovery.py` exercises retained-response
matching, changed or missing audits, multiple retired revisions, cross-portfolio
invalidation, competing recovery requests and rollback of the inner savepoint and
outer transaction. It uses isolated synthetic databases and does not establish an
HTTP authorization or an external source of ownership evidence.

`uv run pytest tests/test_trade_audit_recovery_api.py` covers owned metadata reads,
reauthentication, CSRF, physical version bounds, idempotent replay, competing
requests, response-cache validation and atomic failure. These tests use synthetic
temporary databases and do not prove external market ownership.

`npm run build && npm run test:e2e:trade-origin` uses an exclusive temporary
loopback application and real Chromium. It creates synthetic legacy drafts and
new audit rows without changing existing audit rows or guards. It exercises the
responsive form, password confirmation, committed-but-lost response and original
zero-write retry, and checks the actual wire body for `2^53+1` and `INT64_MAX`
sequence digits. Synthetic financial facts, historical FX and existing audits
remain unchanged. This is not a container, NAS, TLS, live-provider or external
ownership test; the private fixture suppresses diagnostics and removes its own
temporary tree after shutdown.

`uv run pytest tests/test_trade_history_review_migration.py` checks synthetic
upgrade preservation, append-only acknowledgment/withdrawal history, owner and
physical-version fences, missing-evidence classification, replacement attempts,
concurrent decisions, downgrade refusal and inert SQL rows. The test also shows
that SQL can retain a false but well-shaped snapshot hash, and that such a row
cannot unblock the current trade writer. It is not authentication or a review API.

`uv run pytest tests/test_trade_history_review_service.py` exercises audited
acknowledgment and withdrawal, exact snapshots, later conflicting or matching
audits, actual retirement links, origin recovery, replay, competing decisions,
owner/version fences and savepoint rollback. It uses newly created synthetic
records in verified temporary databases and does not remove audit guards or alter
existing audit events. It does not exercise a protected review endpoint or grant.

`uv run pytest tests/test_trade_history_review_api.py` exercises protected status,
explicit gap fencing, two versions, authentication/CSRF/reauthentication,
concurrency, exact revision strings, pagination, corrupt audits, cached-intent
validation and atomic rollback. It also checks that an acknowledged audit gap does
not yet unblock either trade correction or void. These are isolated synthetic
HTTP/SQLite tests, not proof of external ownership or eligibility.

`npm run build && npm run test:e2e:trade-review` runs the actual application and
Chromium against an exclusive temporary database. It checks reasoned review and
withdrawal, recent password confirmation, a committed-but-lost response and its
unchanged-key zero-write replay, and withdrawal after retained-origin recovery
changes the evidence. It adds only new synthetic legacy facts and checks that
existing audits, financial facts and historical FX remain unchanged. It does not
exercise a worker, provider, deployment volume, container, NAS, TLS or external
ownership; private diagnostics follow the pinned browser policy.

`uv run pytest tests/test_eligibility_trade_origins.py tests/test_dividend_eligibility_service.py`
checks origin-to-retirement audit matching, mixed legacy histories, inconsistent
retained claims and concurrent WAL read snapshots. These are internal recorded
share checks, not authenticated event, entitlement or receipt endpoints.

`uv run pytest tests/test_dividend_event_migration.py tests/test_dividend_event_service.py tests/test_dividend_event_writer.py`
checks migration preservation and rollback, source and candidate scope, publication
audits, manual locks, immutable history, whole-chain and review corruption
detection, exact scaled values, audited receipt reversal, competing writers,
projection invalidation and the pre-rebuild entitlement fence. These are synthetic
database and internal-library checks, not HTTP authorization, provider coverage,
verified documents, receipt projection calculation, live-provider or NAS
acceptance. Downgrade refuses to delete history; restore a validated pre-upgrade
backup instead.
