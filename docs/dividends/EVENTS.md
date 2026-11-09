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

## Required future writer and projection work

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

`uv run pytest tests/test_dividend_event_migration.py` checks migration preservation
and rollback, source and candidate scope, publication audits, manual locks,
immutable history, exact scaled values, audited receipt reversal, competing
writers and per-account review across revisions. These are synthetic native-SQL
checks, not HTTP authorization, provider coverage, verified documents, receipt
projection calculation, live-provider or NAS acceptance. Downgrade refuses to
delete history; restore a validated pre-upgrade backup instead.
