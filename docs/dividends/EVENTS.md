# Dividend event integrity foundation

Migration `0016_dividend_event_controls` adds database safeguards for a future
owner-controlled dividend workflow. It adds two initially empty provenance tables
and brings the schema to 55 business tables. It neither adopts a source observation
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

**Retired-trade limitation:** retiring a trade overwrites its original
`updated_at`, so its original creation/revision after fingerprint and the
retirement before fingerprint cannot always be reconstructed from the retained
trade row. The reader checks the retained audit's presence, actor, metadata and
fingerprint shape, but cannot detect every valid-shaped alteration of those old
hashes. `trade_history_audit_scope=RETIRED_TRADE_HISTORY_UNVERIFIED` makes this
limitation explicit whenever the relevant history contains a superseded or
voided trade; otherwise it reports `NO_RETIRED_TRADES`. Do not treat the former
as a fully verified historical audit chain. Restoring or independently verifying
that lineage requires additional retained evidence or an explicit review path;
the reader does not guess the lost timestamp or treat an expiring idempotency
cache as durable proof.

The reader performs no writes, provider requests or credential-file reads. It
is not an entitlement projection, event/receipt API or UI. Before any economic
use, a writer must revalidate current owner, facts, action scope and the audit
limitation in the same transaction as the financial changes; an earlier read or
`data_revision` alone does not authorize adoption. Root-history and audit scans
may need indexed batching if measured portfolio histories grow.

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

`uv run pytest tests/test_dividend_event_migration.py tests/test_dividend_event_service.py tests/test_dividend_event_writer.py`
checks migration preservation and rollback, source and candidate scope, publication
audits, manual locks, immutable history, whole-chain and review corruption
detection, exact scaled values, audited receipt reversal, competing writers,
projection invalidation and the pre-rebuild entitlement fence. These are synthetic
database and internal-library checks, not HTTP authorization, provider coverage,
verified documents, receipt projection calculation, live-provider or NAS
acceptance. Downgrade refuses to delete history; restore a validated pre-upgrade
backup instead.
