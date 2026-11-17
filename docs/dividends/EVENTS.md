# Dividend event integrity foundation

Migration `0016_dividend_event_controls` added database safeguards for an
owner-controlled dividend workflow. It added two initially empty provenance tables
and brought that schema revision to 55 business tables. It neither adopts a source
observation nor installs an entitlement, receipt or reconciliation API. The protected
manual event writes described below retain decisions but do not create income. An
ordinary source sync still cannot create canonical income or mark a dividend received.

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
basis/action scope. The protected DTOs described below authenticate the caller and
withhold private URL details. The internal reader performs no writes, provider
requests, credential-file access, entitlement calculation or receipt posting.

## Event workspace

The signed-in `/dividends` page lists owned event decisions by ex-date using the
protected, server-paginated event API. Owners can filter by security, recorded event
status and date, inspect the exact DPS and original decision/evidence, and open
separately paginated revision and rejection histories. Search, filters and cursors
remain in the authenticated in-memory workspace; they are not written to the browser
address bar, local storage or an offline cache. The five-item mobile navigation links
to this page; corporate actions remain reachable from the transaction page.

Opening an event reads its rejection state separately. A rejection can change while
its event DTO and ETag stay unchanged; the page therefore provides an explicit
rejection refresh, labels the last successful read, and refuses to associate responses
whose stable event, physical revision or version do not match the displayed event.
The two requests are not an atomic snapshot, even when their pins match. A failed
refresh is marked stale rather than treated as an absence of rejection. Historical
resolution links are shown as retained decisions, not verified documents. Private
citation URLs with a query or fragment are withheld, and opening an allowed external
link is an explicit user action.

`VERIFIED` is displayed as an owner acceptance, not document verification; a missing
event or an unrejected decision does not establish ownership, eligibility, an accrued
amount or a receipt. List and history reads do not calculate income, mutate financial
facts, contact providers, or relax the event-revision fence. Protected projections and
receipt reconciliation remain separate unfinished work.

### Read account tax and fee estimates

Expand an event and choose **View account tax and fee estimates**. The owner
explicitly selects an account from bounded pages and enters a calendar day; neither
is inferred from the security, event date or current date. Viewing these estimates is
read-only and not evidence that the account holds the security or qualifies for a distribution.
Account selection and estimate/history query scopes stay in mounted component memory,
not a query cache, browser address bar or browser storage. Failed, offline or stale account reads cannot
be used for a new selection.

The separate tax and fee reads must each match the frozen stable event, physical
revision, version, selected account, portfolio, broker and explicit day. Setting
heads, account and broker versions stay canonical decimal strings and are never
converted through JavaScript `Number`. Selected records must match their head,
effective day and account-scope labels. Malformed, foreign, duplicate or out-of-order
history fails closed. A later account or broker change is shown as a stale setting
scope, not silently treated as a current estimate. Histories are bounded independent
reads and may contain later decisions than the displayed selection.

`MISSING` is shown as unconfigured rather than zero; `UNKNOWN` stays unknown, including
an unknown fee with a separately known currency; an explicitly estimated zero remains
zero. The fee selection includes its own rejection-state read. The tax selection and
independently fetched event rejection are separate requests, not an atomic snapshot;
no clear state is inferred for tax. None of these owner estimates confirms withholding,
fees, documents, ownership, income, entitlement, a receipt or FX.

`npm run test:e2e:dividend-estimates` uses a new synthetic application and real browser.
It creates a zero tax estimate and unknown USD fee through the protected APIs, then
checks the read-only workspace at 360/768/1440, keyboard, no-store, history, an earlier
missing selection with a nonempty head, separate rejection refresh with unchanged
event ETag, and stale account scope after a real account edit. Financial/FX tables
remain empty; no provider, worker, deployment data or screenshot is used.

### Record an account estimate in the browser

After selecting the account and lookup day, choose **Check rejection state to record an
estimate**. The independent fresh rejection read must match the displayed event and
physical revision and show no recorded rejection; the fee read must also show no
rejection. Failed, pending, offline, malformed or inconsistent reads cannot open a
new editor. These reads are separate snapshots, not proof that the event is accepted;
the server rechecks source, rejection, setting head and account scope atomically.

The password-confirmed tax and fee sheets freeze the displayed event, account,
portfolio, broker, exact-string account/broker versions and setting-head pins. Enter
an explicit effective day and reason; the lookup day is not silently reused. Choose
an explicit estimate or unknown: tax rates are exact decimal fractions from zero to
one, while an unknown fee can have an explicitly chosen currency or an explicitly
unknown currency. An estimated fee requires a known currency. No value is rounded or
inferred as zero, and the acknowledgment states that the decision is not a confirmed
deduction, ownership, income or receipt.

The form, password and unchanged uncertain retry key stay in component memory only.
Closing discards that local retry context; inspect history before starting another
operation. A response may be a historical zero-write replay even after later settings
or rejection. The sheet validates returned scope, predecessor, exact ordinal, values,
reason and no-grant labels, and blocks dismissal while a request is pending. A 409 or
412 retains the inputs for review rather than implying an unobserved operation failed.
No estimated setting creates a projection, receipt, cash entry or exchange rate.

`npm run test:e2e:estimate-writes` starts a separate synthetic application and real
browser. It checks password confirmation, 360/768/1440 layouts, no-store reads, a
held/lost zero-tax response, same-key historical replay after a later unknown tax,
an unknown fee with known currency, an account-version conflict, and a competing
rejection that blocks a new fee without writes. Financial/FX tables remain empty; no provider, worker, deployment data
or screenshot is used.

### Manual event creation in the browser

The event workspace offers a separate manual creation sheet with password
reauthentication; the server requires a recent password authentication. The
owner explicitly selects the stable security, ex-date, DPS and dividend currency; the
form never derives the dividend currency from a security quote or account. A blank
amount cannot become zero, while explicitly entered zero remains zero. Other dates
and the URL may remain unknown; event type and adjustment basis begin as `UNKNOWN`.
The owner supplies a citation, a decision reason and a separate acknowledgment that
this is not verified documentation, ownership or income. A new creation is a distinct
stable event, even when another event has the same security and ex-date.

Inputs remain in the open form rather than persistent storage or mutation caches. A
lost or invalid response retains the same body and idempotency key for an unchanged
retry, including after password reauthentication; a changed submitted intent gets a
new key. Closing the form discards that local retry context, so the owner must inspect
history before another creation. The response is checked against the submitted scope,
exact scaled DPS, status, citation, redaction and explicit no-grant labels. A matching
response may be a historical replay, not proof that it is the latest event state. The
sheet blocks closing while a password or decision request is pending and refreshes
server reads after closing or saving. Revision/rejection decisions and financial
reconciliation are not performed by this creation form.

### Manual event revision in the browser

An owner can open a separate manual revision only after the independently loaded
rejection state matches the event's stable ID, physical revision and version, and a
rejection's cited decision has a valid, safely redacted reference and reason. A failed,
paused, pending, mismatched or incomplete state does not enable revision. The editor freezes those
observed pins when opened; its PATCH sends the original physical revision and event
`If-Match`, together with the exact rejection ID when a rejection was observed. The
server rechecks all of them in its writer transaction; the browser reads are not a
single atomic snapshot or a financial grant.

Existing terms are shown without converting the DPS to a JavaScript number. The
security identity cannot be changed. Each applicable unlock, reopening and rejection
resolution requires its own explicit confirmation. A new citation must be entered:
previous URLs, including withheld private URLs, are never silently copied, and the
owner must acknowledge that the new version uses only its newly entered reference.
The same memory-only retry, reauthentication, pending-close and response-checking rules
as creation apply. A stale response preserves the input and directs the owner to
recheck history; a response may be a historical idempotent replay. Existing entitlement
or projection history still blocks revision until atomic rebuilding and receipt
reconciliation are implemented. Existing receipts, cash and historical FX are not
rewritten by this workflow.

### Manual override in the browser

The event workspace offers a distinct, reauthenticated manual-override editor only
when a separately loaded rejection state matches the displayed event and physical
revision. It fixes the security and observed pins while the owner explicitly supplies
replacement terms, citation and reason. A separate confirmation records the intent
to create an override, in addition to each applicable unlock, reopening, rejection
resolution and new-reference confirmation. The new cited URL is never copied from
an older version, and missing amounts or currencies are not filled in automatically.

The protected server appends a new `MANUAL` revision marked `manual_override=true`;
older decisions remain unchanged. The browser checks the new identity, exact terms,
reference redaction, explicit override marker and no-grant labels. An uncertain
unchanged request retains its memory-only body/key across reauthentication, and a
successful response can be a historical replay rather than the current version.
Existing entitlement or projection history still blocks the new decision until atomic
reconciliation exists; no receipts, cash or locked FX are rewritten. An override does
not verify a document, establish ownership or create income.

`npm run build && npm run test:e2e:dividend-override` uses a separate exclusive
synthetic application and real browser to exercise a locked, withdrawn and rejected
version, all explicit confirmations, a held/lost response and historical zero-write
replay after a later revision. A competing rejection blocks an unpinned override without
writes or loss of form input. No worker, provider, deployment data or screenshot is
used.

### Owner acceptance in the browser

A separate acceptance editor requires a successfully loaded rejection state matching
the displayed event and physical revision. It is available for valid, non-withdrawn
events with an explicitly `AS_DECLARED` amount basis; unknown or split-adjusted basis
is not silently converted. The owner sees the exact terms, reauthenticates, enters a
new citation and reason, and confirms that the acceptance is an owner decision, not
document verification or income. If the observed version is rejected, a separate
confirmation binds its exact rejection ID; the rejected decision and its reference
remain in history. A later rejection after a clear read is not silently resolved.

The protected server rechecks the version, physical revision and current source or
candidate, then appends a new locked `VERIFY` revision on identical terms. The browser
validates the returned physical identity, exact decimal terms, status, reference
redaction and no-grant labels. It never substitutes prior private URLs or treats
conflicting evidence as independently verified. Historical entitlement or projection
history still blocks the decision until atomic reconciliation exists. An uncertain
unchanged retry retains its memory-only body/key even through reauthentication; the
returned revision may be an old idempotent replay rather than the current version.
The operation does not post or reverse a receipt or rewrite cash or historical FX.

### Rejection decision in the browser

A separate rejection editor is offered only after a successful, unpaused and matching
independent read reports that the observed physical revision is not rejected. A failed,
pending, mismatched or inconsistent read cannot enable it, and a recorded rejection
cannot be rejected again through the form. The owner must reauthenticate, supply a
citation and reason, and explicitly confirm the rejection's limited scope. The sheet
freezes the stable event ID, event version and physical revision ID; the server rechecks
them inside its writer transaction, so a rejection or revision created after the read is
not silently replaced. Existing entitlement or projection history also blocks a new
rejection until atomic reconciliation exists.

A successful rejection returns the unchanged event representation, not the rejection
record. The browser checks that representation against the complete frozen event, then
refreshes event and rejection reads independently. A same-key response may be a
historical replay even after a later resolution; it does not establish the current
rejection state. A dropped or inconsistent response retains the unchanged submitted
body and idempotency key in the open form; changing a submitted intent rotates the key.
Closing discards that local retry context, so inspect rejection history before making
another decision. Password and decision requests prevent closing while pending. Neither
this decision nor its citation proves a document, creates eligibility, withdraws an
event or reverses a receipt. No cash or historical FX is rewritten.

### Event withdrawal in the browser

A separate withdrawal editor requires a fresh rejection-state read matching the
stable event, physical revision and version. It accepts a valid non-withdrawn event
without converting an unknown or split-adjusted amount basis. The owner sees the
frozen terms and any cited rejection, reauthenticates, supplies a new citation and
reason, and explicitly confirms the withdrawal. A rejection observed at opening
requires a separate confirmation and exact rejection ID; a rejection added later is
not silently resolved. A previously withdrawn event must instead be reopened through
the manual revision workflow with the required confirmations.

The protected server rechecks the pins and appends a `WITHDRAW` revision with the
same terms and prior manual-lock state. The browser validates that successor, including
the exact DPS, citation redaction and no-grant labels. Existing entitlement or
projection history blocks withdrawal until atomic reconciliation exists. Withdrawal
is not a receipt reversal and does not rewrite cash, receipts or historical FX. A
lost or inconsistent response keeps the unchanged memory-only request and key for
retry; a replay may be historical even after a later explicit reopening.

`npm run build && npm run test:e2e:dividend-withdrawal` uses a separate exclusive
synthetic application root and real browser. It checks explicit rejection resolution,
retained lock and terms, pending-close protection, a dropped committed response and
zero-write historical replay after a later unlock/reopen, and a competing rejection
that blocks an unpinned withdrawal. It does not use an existing installation, worker,
provider or screenshot and does not establish document verification or financial
eligibility.

`npm run build && npm run test:e2e:dividend-events` runs the real API and built SPA
with synthetic events in an exclusive temporary root. It checks responsive/keyboard
behavior, no-store responses, a rejection without an ETag change, private URL
withholding and an unchanged complete database after the read-only views. It also
reauthenticates, holds and drops committed manual-creation and revision responses,
checks the busy close guard, and retries each unchanged body/key with no additional
database writes. Before replaying the revision, the test appends a later successor and
verifies that the replay remains historical and does not replace the latest version.
The revision exercise also checks the explicit observed rejection pin,
retained resolution and unchanged financial facts. A separate competing rejection
created after an unrejected state was read leaves the ETag unchanged; an unpinned
revision is rejected without writes and the form retains its input. It also submits a
browser rejection, drops the committed response, resolves that rejection with a later
revision and verifies the unchanged-key replay is historical and zero-write. A competing
rejection made after a clear read blocks a second browser decision without overwriting
it or discarding the form. The browser also accepts an explicitly pinned rejected
revision, tests the separate resolution confirmation and drops its committed response;
replaying after a later unlock and revision writes nothing and does not replace the
current state. A rejection added after a clear acceptance read blocks the unpinned
acceptance without writes.

## Protected event-decision read API

Authenticated owners can read retained managed decisions through three private,
`Cache-Control: no-store` endpoints:

- `GET /api/v1/dividends/events` lists current owned events with optional security,
  workflow-status and inclusive ex-date filters, literal search and bounded keyset
  pagination. Distinct events with the same ex-date remain distinct.
- `GET /api/v1/dividends/events/{event_id}` returns the current physical revision
  and an event-version ETag.
- `GET /api/v1/dividends/events/{event_id}/revisions` lists the complete retained
  decisions by descending revision, with bounded pagination and literal search.

Each returned event is checked against its entire audited history, including later
revisions, candidate lineage and original evidence. An inconsistent owned chain
returns a fixed 503 error rather than a partial or repaired result. An unrelated or
unmanaged legacy event is not silently assigned to the owner. Historical candidate
assessments remain historical after later basis or action changes and do not establish
current adoption. The response explicitly carries `OWNER_EVENT_DECISION_ONLY`,
`REVALIDATION_REQUIRED`, `document_verification=NOT_ESTABLISHED` and
`eligibility_scope=NOT_ESTABLISHED`. A manual decision has no independent-origin
count rather than an invented zero; a `VERIFIED` status is an owner's retained
workflow decision, not proof that a document was fetched or authenticated.

Citation URLs containing a query or fragment are withheld rather than returned with
private parameters; the reference and a withholding marker remain visible to the
owner. These reads never contact a provider, create an entitlement or change receipts,
cash or historical FX. Protected candidate adoption, owner acceptance, explicit manual
override, withdrawal and rejection are described below; the browser candidate-adoption
form remains unfinished. The existing event revision fence is unchanged.

## Protected manual event writes

`POST /api/v1/dividends/events` creates a new stable event and initial physical
revision from explicit owner-supplied terms, citation and reason. For example:

```json
{
  "security_id": "00000000-0000-4000-8000-000000000003",
  "event_type": "REGULAR",
  "ex_date": "2026-01-08",
  "announce_date": "2025-12-01",
  "record_date": "2026-01-09",
  "pay_date": "2026-02-01",
  "dps": "1.23456789",
  "currency": "USD",
  "adjustment_basis": "AS_DECLARED",
  "citation": "Synthetic distribution reference",
  "source_url": null,
  "reason": "Synthetic owner-entered distribution decision"
}
```

Dates other than ex-date and the URL may be omitted when unknown. DPS must be an
exact nonnegative decimal string with at most eight fractional digits; an explicit
zero remains distinct from missing data. Currency is explicitly HKD or USD, never
inferred from the security. `AS_DECLARED` creates a provisional owner decision;
`UNKNOWN` and `SPLIT_ADJUSTED` remain incomplete and open an evidence review. A
citation can describe a local reference, but neither a citation nor an HTTPS URL
proves the document was fetched or authenticated. URLs with queries or fragments
are withheld from responses; the original remains in the private decision record.
Never include credentials in the citation, URL or reason.

`PATCH /api/v1/dividends/events/{event_id}` requires the complete manual payload,
the observed `current_revision_id` in `revision_id`, and `If-Match` containing
its quoted event version. The security cannot be changed on the stable event.
Replacing a manually locked decision requires `confirm_unlock: true`; reopening a
withdrawn event requires `confirm_reopen: true`. Both flags are required when both
conditions apply. These explicit owner actions create a new provisional or incomplete
physical revision, not a verified document. The earlier revisions and decisions stay
unchanged. A stale physical ID or version returns 412; an exhausted event version
cannot be advanced. Two distributions on the same security and ex-date can be distinct
stable events and are never merged by date.

Both writes require an active owner session, matching Origin, CSRF token, idempotency
key and recent password confirmation. The serialized writer rechecks session, CSRF,
reauthentication and the observed event, and commits the decision, correlated audits,
evidence-review changes, affected portfolio invalidations and idempotency response
together. An additional append-only audit fingerprints the normalized HTTP intent,
route, idempotency-key digest and exact event/revision identities, so a cached response
cannot be swapped with another otherwise identical event. These database fingerprints
are consistency checks, not signatures or independent document evidence.

A completed unchanged retry reauthenticates and verifies the entire retained event
history, original unredacted reference, request binding and cached response. It returns
the original result without writing, even after a later revision; its ETag describes
that original result, not the current event. Read the event again for its current ETag.
Reusing a key with different input returns 409. An inconsistent history or cached
result fails closed with 503, without attempting repair. Responses are private and
`no-store`.

The existing publication fence rejects **every** new revision when any prior revision
has an entitlement or the event has projection history, until a future atomic rebuild
and reconciliation workflow can preserve all receipt associations. Neither manual
write calculates holdings, posts receipts or changes cash or locked FX. Protected
acceptance, rejection, manual override and withdrawal are described below; projection
rebuilding, receipt posting and the browser dividend workspace remain separate work.

## Protected candidate adoption

These private endpoints record an explicit owner decision; they do not automatically
merge source observations or establish income:

- `POST /api/v1/dividends/events/from-candidate` creates a **distinct** stable event.
  Its body requires `confirmation=CREATE_DISTINCT_EVENT`.
- `POST /api/v1/dividends/events/{event_id}/from-candidate` appends a candidate
  decision to the **selected existing** stable event. Its body requires
  `confirmation=MATCH_EXISTING_EVENT` and the observed event `current_revision_id`
  as `revision_id`; `If-Match` carries the quoted observed event version.

Both bodies require the selected stable `candidate_id`, physical
`candidate_revision_id`, integer `candidate_version` and a reason. For example:

```json
{
  "candidate_id": "00000000-0000-4000-8000-000000000001",
  "candidate_revision_id": "00000000-0000-4000-8000-000000000002",
  "candidate_version": 1,
  "confirmation": "CREATE_DISTINCT_EVENT",
  "reason": "Synthetic explicit candidate association"
}
```

The server rechecks the active owner, current open candidate and exact physical
revision, its audited source associations and current recorded basis/actions inside
the writer. Missing ex-date, DPS or currency is not filled; a complete candidate
with conflicting or absent evidence retains `CONFLICT` or `INCOMPLETE` and opens
an evidence review. A candidate already linked to a stable event cannot be linked
to a different event. Distinct candidates on the same security and ex-date may be
explicitly recorded as distinct events. A candidate cannot replace a locked or
withdrawn event, change an existing event's security, or bypass the publication
fence when entitlements or projection history already exist.

Both operations require the session, matching Origin, CSRF, idempotency key and
recent password confirmation. The event decision, links, audits, relevant review
and portfolio invalidation, and serialized idempotency response commit together.
An unchanged completed retry checks the current session and the complete retained
event/candidate history and original intent, then returns its original result without
writing. A later candidate dismissal, basis change or event revision does not turn
that historical response into a new current-use grant; read the event again for its
current ETag. Changed intent under the same key conflicts, stale pins return 412,
and inconsistent retained history fails closed with 503. Responses are `no-store`
and explicitly retain `OWNER_EVENT_DECISION_ONLY`, `REVALIDATION_REQUIRED`,
`document_verification=NOT_ESTABLISHED` and `eligibility_scope=NOT_ESTABLISHED`.
No provider is contacted and no entitlement, receipt, cash or locked FX is changed.

## Protected owner acceptance

`POST /api/v1/dividends/{event_id}/verify` accepts an explicit
`ACCEPT_REVISION` decision. It requires the observed physical `revision_id`, the
quoted event version in `If-Match`, an owner citation and a reason. An optional
`evidence_url` is a reference supplied by the owner, not a document fetched or
independently authenticated by the server. For example:

```json
{
  "revision_id": "00000000-0000-4000-8000-000000000002",
  "decision": "ACCEPT_REVISION",
  "citation": "Synthetic distribution notice reviewed by owner",
  "evidence_url": null,
  "reason": "Synthetic owner acceptance of this revision"
}
```

The operation requires an active owner session, matching Origin, CSRF token,
idempotency key and recent password confirmation. Inside the serialized writer it
rechecks the event and exact observed physical revision; for a candidate decision it
also rechecks the current candidate, source and recorded basis/action scope. It appends
an audited `VERIFY` revision with the same terms, `workflow_status=VERIFIED` and
`manual_locked=true`, together with relevant review resolution, portfolio invalidation
and the idempotency response. The adjustment basis must be `AS_DECLARED`; unknown or
split-adjusted basis cannot be accepted. A withdrawn event cannot be accepted without
an explicit manual reopening. An owner can explicitly accept unchanged terms even when
source comparison previously needed review; closing that workflow review does not
establish that the evidence or document is independently verified.

A completed unchanged retry reauthenticates and validates the complete retained
history, original unredacted reference, request binding and cached response, then
returns the original result without writing. A later candidate dismissal or event
revision does not make that historical response a new grant. Its ETag describes the
original accepted revision; read the event again for the current version. Changed
intent under the same key conflicts, stale event or candidate pins return 412,
unresolved current scope returns 409, and inconsistent retained history fails closed
with 503. URLs with a query or fragment are withheld in responses. Responses are
private and `no-store` and explicitly retain `OWNER_EVENT_DECISION_ONLY`,
`REVALIDATION_REQUIRED`, `document_verification=NOT_ESTABLISHED` and
`eligibility_scope=NOT_ESTABLISHED`.

The existing publication fence rejects acceptance when any prior revision has an
entitlement or the event has projection history. This decision does not calculate
holdings, post receipts or alter cash or locked FX; tax estimates for an earlier
physical revision are not inherited. A separate protected `REJECT_REVISION` decision
is described below; rejection is not withdrawal, zero income or replacement terms.

## Protected manual override

The same verification endpoint accepts `MANUAL_OVERRIDE` only with complete explicit
replacement terms. It requires the observed physical `revision_id`, quoted event
version in `If-Match`, unchanged `security_id`, event type, ex-date, exact DPS string,
currency, adjustment basis, citation and reason. Dates other than ex-date and
`evidence_url` may be omitted when unknown. For example:

```json
{
  "revision_id": "00000000-0000-4000-8000-000000000002",
  "decision": "MANUAL_OVERRIDE",
  "security_id": "00000000-0000-4000-8000-000000000003",
  "event_type": "REGULAR",
  "ex_date": "2026-01-08",
  "dps": "1.23456789",
  "currency": "USD",
  "adjustment_basis": "AS_DECLARED",
  "citation": "Synthetic owner reference for replacement terms",
  "evidence_url": null,
  "confirm_unlock": true,
  "reason": "Synthetic explicit owner override"
}
```

The decision is a new owner-entered `MANUAL` physical revision with
`manual_override=true`, not an automatically verified document. Replacing a locked
revision requires `confirm_unlock: true`; reopening a withdrawn event requires
`confirm_reopen: true`; both are required when both conditions apply. A new
`AS_DECLARED` override remains `PROVISIONAL`. `UNKNOWN` or `SPLIT_ADJUSTED` remains
`INCOMPLETE` with an evidence review, and explicit zero remains distinct from missing
DPS. Later acceptance is a separate decision. A previous candidate is retained in
history but is not silently adopted as the source of the new manual terms.

The active session, Origin, CSRF, idempotency key, recent password confirmation and
physical/version pins are rechecked inside the writer. The revision audit's canonical
fingerprint binds the override label, and a second correlated audit binds its observed
predecessor and new revision. The historical reader validates both for the complete
chain: a late marker cannot turn an ordinary manual decision into an override, and a
missing or inconsistent marker cannot turn an override into a plain manual decision.
Older manual and other decisions retain their original audit fingerprints and report
`manual_override=false`; older cached responses without the additive label replay with
that false label without rewriting storage. Matching local audit fingerprints are not
signatures, document authentication or proof of complete ownership.

An unchanged completed retry reauthenticates and validates the original unredacted
reference, request intent, cached result and complete history, then returns the original
result without writes, even after a later revision. Its ETag is historical. Changed
intent under the same key conflicts, stale pins return 412 and inconsistent retained
history fails closed with 503. URL queries/fragments are withheld from responses; no
provider is contacted. Existing entitlement/projection history still fences publication,
and receipts, cash and locked FX are not rewritten. A tax setting on the previous
physical revision is not inherited.

## Protected event withdrawal

`POST /api/v1/dividends/events/{event_id}/withdraw` records a specific owner's
explicit decision to withdraw one observed event revision. It requires the current
physical revision ID, the quoted current version in `If-Match`, an owner reference,
a reason and the exact `WITHDRAW_EVENT` confirmation. For example:

```json
{
  "revision_id": "00000000-0000-4000-8000-000000000002",
  "confirmation": "WITHDRAW_EVENT",
  "citation": "Synthetic withdrawal notice reviewed by owner",
  "source_url": null,
  "reason": "Synthetic owner-confirmed withdrawal decision"
}
```

An optional HTTP(S) `source_url` is retained as an owner-provided reference, never
fetched or independently authenticated by this operation. URLs with queries or
fragments are withheld in responses; credentials are not permitted in a URL and
should never be placed in the citation or reason. A missing provider response never
withdraws an event automatically.

The endpoint requires the active owner session, matching Origin, CSRF, an idempotency
key and recent password confirmation. The short serialized writer checks the exact
observed event and retained history, then appends the withdrawal decision and its
audits together with applicable portfolio invalidations and the idempotency response.
It preserves the earlier terms and manual lock. A withdrawn event can only be reopened
by a separate explicit manual revision, with the applicable reopen and unlock
confirmations. A new key attempting to withdraw an already withdrawn event returns
409; stale event pins return 412.

An unchanged completed retry rechecks authentication, the full retained history,
the original unredacted reference and request binding, and returns its original
result without writing, even if the event was subsequently reopened. The returned
ETag describes that historical result; read the event again for its current ETag.
Changed intent under the same key conflicts and inconsistent history fails closed.
Responses are private and `no-store`; the owner decision is not document verification
or an eligibility grant.

The existing publication fence still rejects withdrawal when any prior revision has
an entitlement or the event has projection history. This endpoint does not reverse or
hide receipts, cash or locked FX and does not replace the future atomic cancellation,
reconciliation and receipt workflow.

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

The writer inserts the event revision, source links, correlated audits, affected
owner-portfolio invalidations and any conflict/incomplete evidence reviews in
one transaction. Its savepoint rolls back the entire event operation if a caller
catches a body failure; unrelated outer work remains intact. If savepoint creation,
rollback or release has an uncertain outcome, the enclosing transaction is aborted.
Reviews are attached to affected portfolios, or to the event when none exists. An
explicit later decision resolves outstanding reviews only after rechecking their
original event revision, canonical detail, owner and creation audit; corrupt review
history aborts the event operation. Historical replay of the same internal revision
identity, request and timestamp is zero-write even after a later source/action change
or receipt. A new candidate use is not such a replay and must pass current-scope checks
again.

**Projection safety fence:** if *any* prior revision of an event has an
entitlement, or the event has a stored projection version, this internal writer
rejects a new revision—even a verification with unchanged terms—until an atomic
projection rebuild is installed. The projection-history migration also blocks
publication through direct SQL while any projection version exists. Repointing the
current event without replacing the applicable projection could otherwise hide
old entitlements and receipts. Initial decisions only invalidate affected
portfolios; they do not calculate entitlements or create actual receipts. The protected
manual revision API retains this fence until it can atomically maintain those projections
and reconciliation; the receipt workflow remains separate.

Source, basis and candidate responses use `canonical_adoption_scope=RESPONSE_ONLY_NOT_LINKAGE`
to make clear that their static `NOT_ADOPTED` marker describes the response itself,
not a lookup for current or historical event evidence links. An event may retain a
link even though the source/review payload is not itself an event. No internal decision
invokes a provider, reads credential files or verifies a cited document.

## Current event-source fence

The internal `dividend_event_writer.require_current_source` rechecks one active
owner's complete audited event history and exact physical revision/version in
an existing writer transaction. For a candidate decision it independently
requires the same current, open candidate, original source and recorded
basis/action scope, and matches its identity, terms and assessment to the event.
Dismissed or superseded candidates and changed basis are rejected for new use;
they do not erase valid historical reads. Explicit manual verification or
withdrawal remains its own decision rather than silently reverting to an old
candidate. The caller must use the application engine's writer unit of work;
changing a connection option after a read transaction starts cannot promote its
potentially stale WAL snapshot to a serialized writer.

This function does not authenticate a session, prove eligible shares or complete
ownership, create income or authorize receipts. `CONFLICT`, `INCOMPLETE` and
`WITHDRAWN` statuses remain visible and are not economic approvals. Its result
is valid only at the instant of the check: a caller that changes the event,
candidate or related action scope in the same transaction must recheck before
using it, and must never carry it into a different transaction. It performs no
provider requests or credential-file access. The internal recorded-projection writer
below consumes this fence but does not authenticate a caller or grant income.

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

Independently, the reader validates the complete recorded review history for every
relevant physical trade, including all retained revisions of a root that moved to
another security or portfolio. It reports `CREATION_GAP_REVIEWED_UNVERIFIED` when
any audited review recorded a missing creation audit, even after withdrawal, an
unverified correction or a subsequently appearing matching audit. A later match
does not establish when the evidence originally existed. If no such review is
recorded, `NO_RECORDED_CREATION_GAP_REVIEW` describes only the retained review
history, not proof that creation evidence or ownership was complete. A currently
missing creation audit still makes this reader fail closed; inconsistent review
history also fails closed even if another gap was already found. The creation-gap
scope and retired-origin scope are separate, so neither hides the other.

The reader performs no writes, provider requests or credential-file reads. It
is not an entitlement projection, event/receipt API or UI. Before any economic
use, a writer must revalidate current owner, facts, action scope and the audit
limitation in the same transaction as the financial changes; an earlier read or
`data_revision` alone does not authorize adoption. Root-history and audit scans
may need indexed batching if measured portfolio histories grow.

## Pure projection-version selection

The internal `dividend_projection.select_current` accepts an explicitly supplied
current event revision and account for each requested scope, plus all supplied
immutable projection versions for those scopes. It requires one contiguous
per-event/account projection chain starting at 1, matching predecessor IDs,
nondecreasing event revision numbers and globally consistent physical revision
identities. It selects exactly one latest projection per scope, including repeated
rebuilds of the same event revision. Duplicate or foreign identities, broken chains,
missing scopes and stale selected revisions fail rather than being silently summed,
omitted or treated as zero. It validates every supplied chain before reporting a
missing or stale scope, so either cannot hide corruption in a different account.

`EXPECTED` carries a shape-checked `Accrual` with internally consistent rounded
components and explicit quality; `NO_POSITION`, `NEEDS_REVIEW` and `CANCELLED`
carry no invented amount. Missing HKD is not zero, and a negative expected net must
remain under review. Selection retains the original rounded amount, rounding delta
and HKD value; it does not rederive historical FX or independently verify the
calculation. The existing `reconcile` function can then use a separately authorized
current expected accrual and aggregate active receipts by stable event/account,
including receipts attached to older revisions. A receipt without a current matching
accrual or a cross-currency difference requires review, not a guessed conversion.

This is a pure selection primitive over caller-supplied records, not a database
reader, historical audit, authenticated grant, persisted projection or receipt
workflow. The caller must establish a complete account scope, verify the entire
stored event/ledger and current eligibility in the same writer transaction, and
apply ex-date recognition rules: an upcoming projection is not accrued income.
No current source or event writer calls this selector yet; the existing event
revision fence remains in place until atomic rebuilding and reconciliation exist.

## Inert projection-version history

Migration `0020_dividend_projection_history` adds an initially empty, append-only
`dividend_projection_versions` table, bringing this schema revision to 59 business
tables. It does not modify, reparent or backfill old entitlements, receipts, cash
postings, historical FX or audit records. Existing receipts stay linked to their
original entitlement; the new table has no receipt linkage. The separate internal
history reader and recorded-projection writer are described below.

A version records an explicitly identified managed event, its current physical
revision and account, a consecutive per-event/account predecessor chain, the
original portfolio and broker identities, observed account and broker versions,
portfolio data revision, as-of date, eligibility rule and unverified trade-history
scopes. These associations remain historical even if an account later moves; a
consumer must recheck the actual current account, broker and fee settings rather
than treating a recorded version or portfolio revision alone as a grant. Expected
amounts retain scaled integer shares, gross, tax, fee, net,
rounding delta, explicit tax and original-fee snapshots, and the selected FX and
source. A nonzero fee in a different currency requires an explicit fee conversion
rate and source. An explicitly zero cross-currency fee can retain its original
currency with a null conversion rate and source; this does not infer an FX rate.
Unknown USD/HKD conversion remains independently null with an explicit quality;
HKD uses an explicit identity rate. `NO_POSITION`, `NEEDS_REVIEW` and `CANCELLED` store no
invented monetary amounts. These values describe a supplied snapshot, not an
assertion that a complete account scope or dividend entitlement was established.

SQL checks owner, exact account/portfolio/broker associations and account/broker
versions, current event revision, portfolio data revision, predecessor, correlated
audit metadata and canonical timestamp ordering. A snapshot
cannot predate its event revision or the event's correlated audit. SQL rejects
mutation, deletion, replacement, stale event revisions and malformed value shapes.
After any version is stored, direct event publication and the internal event writer
are fenced until
an atomic rebuild is implemented; historical decision replay remains read-only.
These checks cannot authenticate the caller, verify canonical audit fingerprints,
prove eligibility or validate the economic calculation. A valid-shaped false audit
or amount can still pass SQL. The table itself must not be consumed as a financial
grant, an income record or a receipt. Future protected consumers must recheck the
complete current scope and arrange event publication, all affected projections,
receipt reconciliation, audit and idempotency in one transaction; that work must
replace the temporary publication fence deliberately. Downgrade refuses to remove
history: restore a validated pre-upgrade backup instead.

## Inert tax-setting provenance storage

Migration `0021_dividend_tax_settings` adds one initially empty, append-only table
and brings the schema to 60 business tables. It does not backfill or change old
projections, entitlements, receipts, cash, FX or audit records. A row records one
explicit event revision/account scope, original portfolio and broker identities,
observed account and broker versions, an effective-from date, a consecutive
predecessor, a correlated request/audit and either an `ESTIMATED` fraction in
scaled parts per million or an explicit `UNKNOWN` with no rate. Zero is valid
only when explicitly supplied; neither a missing row nor `UNKNOWN` means zero.
A setting for an earlier physical event revision is not automatically inherited
by a later revision.

SQL checks the current event and account scope at insertion, audit metadata,
revision sequence and canonical dates; it rejects updates, deletions, replacement
and conflicting version/request identities. It cannot authenticate a session,
verify an audit fingerprint, select an effective setting, prove eligibility or
confirm actual withholding. SQL-shaped rows are not an income record or grant.
The separate internal controller below validates audit content and selection; the
protected API described below adds authentication but does not confirm withholding.
The projection provenance retains an optional selected setting, observed history
head and effective day without rewriting existing projection fingerprints. The internal
projection writer below revalidates them in its own writer transaction and keeps
`ESTIMATED` separate from computational quality. Downgrade refuses to remove history;
restore a validated pre-upgrade backup instead.

## Audited internal tax-setting controller

`dividend_tax_service.history` reads one owner's explicit physical event revision
and account in a single transaction. It validates the complete retained setting
chain and event history, owner and original portfolio/broker scope, chronological
predecessors and canonical audit fingerprints. Missing or foreign scope returns
`None`; an owned scope without decisions returns an empty history, not a zero
rate. Inconsistent history fails closed with a fixed error, and private identifiers,
rates and reasons are omitted from object representations. Matching fingerprints
show consistency of retained records, not session authentication, actual withholding,
complete ownership or an entitlement.

`select` uses a caller-supplied tax-effective calendar day. Of the decisions no
later than that day, the one with the latest effective date wins; a later decision
wins a same-day tie. An explicit `UNKNOWN` masks older estimates, and no applicable
decision is `MISSING`. Selection retains the observed complete history head even
when the selected decision is older or none applies. A supplied historical prefix
can reproduce an earlier view; selection alone does not prove that a caller supplied
the complete database history. Later backdated decisions do not rewrite a retained
snapshot. No date is guessed from ex-date, exchange, receipt date or `as_of`.

`save` rechecks the active owner, current event source, independent rejection history
and exact account/portfolio/broker versions inside a serialized writer. It appends a
reasoned `ESTIMATED` or `UNKNOWN` decision, canonical audit and portfolio invalidation
together. An explicit
zero estimate is valid. A completed identical request replays its original decision
without writes even after later changes; a new request must pass current checks.
A caught failure rolls back the decision, audit and invalidation; an uncertain
savepoint outcome aborts the enclosing transaction. `require_current` independently
rechecks current event/source, independent rejection history, the full setting history,
its exact observed head and the selected account/broker scope. A rejected revision fails
current use even if an earlier setting exists or no setting applies. Callers must repeat
that check after a relevant change even within the same transaction. Both return `OWNER_ESTIMATE_ONLY`, never
confirmed tax. Reasons are retained in the local audit; do not include passwords or
provider credentials in them.

These are internal primitives, not themselves an HTTP authorization or idempotency
boundary. The protected API below rechecks session and CSRF and commits its idempotency
receipt in the same transaction. No current projection, event revision, receipt, cash
or FX is created or changed here, and no provider request or credential-file read is
made. An economic writer still needs independent current recorded eligibility,
event-state, fee/FX provenance, authentication and atomic reconciliation checks.

## Protected tax-estimate API

The owner-scoped base path is
`/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings`.
All responses are private and `Cache-Control: no-store`.

- `GET` requires an explicit `effective_on` date. It returns the current physical
  event revision, account/portfolio/broker scope, the complete setting-history head,
  and the selected `MISSING`, `UNKNOWN` or `ESTIMATED` decision. It reports whether
  a selected setting still matches the current account and broker versions; a changed
  scope is not silently treated as a current estimate.
- `GET /history` requires `revision_id` and `revision_no`; it returns bounded,
  keyset-paginated decisions for that physical revision, including older revisions.
  History is validated before pagination; a corrupt retained chain fails closed.
- `POST` appends one estimate or explicit unknown. It requires the physical event
  revision ID, portfolio and broker IDs, exact account/broker version strings,
  observed setting head number and ID, `effective_from`, `rate`, and a reason.
  Send `null` for an explicit unknown rate; send a decimal fraction such as
  `"0.300000"` for an estimated 30% rate. Zero must be explicitly entered as
  `"0.000000"`. Missing `rate` is invalid, not an implicit unknown.

Account, broker and setting versions are canonical decimal strings up to signed
INT64 limits; clients must preserve their digits without JavaScript `Number` conversion.
`If-Match` pins the bounded event revision number, not the tax-setting head. The
first head is `"0"` with a null ID; later writes must supply both exact observed head
values. Writes require current session, trusted Origin, CSRF, an idempotency key and
recent password reauthentication. The short writer transaction rechecks the session,
reauthentication, event/source, independent rejection state, account/broker versions and
setting head, then commits the decision, audit, idempotency receipt and portfolio
invalidation together. A new write with otherwise current pins to a rejected revision
returns `409 DIVIDEND_REVISION_REJECTED`; an unavailable rejection history fails closed.
The SQL rejection fence remains in place.

An unchanged completed retry returns its original decision after current session and
reauthentication checks, even if later decisions or event revisions exist, provided
the owner/account scope and retained history remain readable. A changed
intent under the same key conflicts; stale observed versions return 412. Results are
`OWNER_ESTIMATE_ONLY` and `REVALIDATION_REQUIRED`, not confirmed tax, ownership,
eligibility or income. Tax selection and history reads remain available for a rejected
revision but do not imply its rejection state or current-use authority. No endpoint
contacts a provider or modifies prior projections, receipts, cash or locked FX. The
browser provides explicit estimate forms; projection rebuilding and receipt workflows
remain unfinished.

## Projection tax provenance

Migration `0022_dividend_projection_tax` adds six nullable columns directly to each
immutable projection version, without adding a business table or changing prior
entitlements, receipts, cash, FX or audit fingerprints. Existing rows retain null
provenance: a legacy tax amount is not proof of an effective setting or confirmed
withholding. Provenance cannot be attached to an old immutable row after the fact.

A version 1 snapshot retains the explicitly supplied tax-effective day, observed
setting-history head number and identity, selected setting identity and separate
`MISSING`, `UNKNOWN` or `ESTIMATED` status. An empty head has number zero and no
identity; an inapplicable future setting can give a nonempty head with no selected
setting. A known zero estimate stays distinct from a missing or unknown rate.
An `EXPECTED` amount requires an `ESTIMATED` selection with an exactly matching tax
fraction; non-amount states carry no invented monetary amount. SQL checks the latest scoped
head, effective-day selection, account/broker versions and timestamp ordering when
inserting a version, but does not authenticate a caller or prove an audit's contents.

The internal projection-history reader verifies the complete retained setting and
projection chains, then reproduces the selection from the original observed prefix.
A later backdated setting does not relabel the older projection; corrupt later
history still fails the complete read closed. Versioned tax fields are included in
that projection's canonical audit fingerprint, while legacy fingerprints stay
unchanged. The explicit day is not inferred from ex-date, receipt date or the server
clock. These checks establish only consistency of retained owner assertions, not
actual withholding, eligible shares or an economic grant. The internal writer below
does not authenticate or reconcile receipts. Event publication remains fenced until
an authenticated atomic rebuild and receipt reconciliation are implemented.

## Inert revision-rejection storage

Migration `0023_dividend_rejections` adds one initially empty immutable table and
brings the schema to 61 business tables. It does not backfill rejections or change
existing event decisions, entitlements, receipts, cash, projections, tax settings,
locked FX or prior audits. A row identifies one owner-controlled current physical
revision, a cited reason, a request and one correlated audit. The schema rejects
wrong or stale event/owner/revision scope, malformed metadata, missing or ambiguous
audit shapes, duplicate rejections, updates, deletes and replacement writes.
Matching database hashes are not signatures, an authenticated owner decision or
proof of external evidence.

The original migration prevented publishing any successor after a retained rejection
and fenced new entitlement, projection or tax-setting inserts for that rejected
revision. Migration `0024` permits only the explicitly audited immediate-successor
resolution described below; the financial publication fence remains in place. A
rejection does not withdraw an event, turn an expected amount into zero, adopt another
revision, reverse a receipt or alter existing cash and historical FX. The original
event representation and its strong version ETag remain unchanged; historical
projection reads do not become current-use approvals. Existing receipts and projections
remain retained and require explicit later reconciliation, not silent deletion.

The SQL guards do not authenticate a request or verify canonical audit content. The
protected rejection decision below adds those HTTP checks, but there is still no
authenticated economic or receipt writer. Do not insert rejection rows or invoke internal writers
on deployment data: where financial history exists, a resolution remains fenced
until atomic projection/receipt reconciliation is implemented.

## Internal rejection-history reader and fence

`dividend_rejection_service.history` reads the supplied owner's event and complete
rejection history in one database snapshot. It validates the entire audited event chain,
exact physical revisions, canonical timestamps, each unique correlated rejection audit,
and each resolved rejection's immediate-successor linkage. Its fingerprint binds the
event and rejection identities, explicit decision, creation time, reason hash and a
hash of the unredacted citation and URL. The source fingerprint also retains any manual
override and earlier rejection-resolution labels. An empty owned history means no
rejection is recorded, not that the dividend is verified or eligible. A missing or
foreign event returns `None`; malformed or inconsistent history fails closed with a
fixed error. The reader performs no writes, provider calls or session authentication.

`require_clear` runs only in an active-owner writer transaction and rechecks the exact
observed event revision and rejection history. It rejects a current unresolved rejection
rather than interpreting it as zero or withdrawal; a properly resolved earlier rejection
remains in history but does not reject a later physical revision. This check covers
rejection state **only**: it does not revalidate a dismissed candidate, basis/actions,
ownership completeness, eligibility, fees, tax or FX, and it is not a financial grant.
Callers must perform all other current-source and authorization checks separately and
repeat this fence after any relevant same-transaction change. A historical WAL read
cannot be promoted into a current writer. The internal recorded-projection writer below
consumes this fence; it is not an authenticated economic or receipt endpoint.

## Internal rejection-decision writer

`dividend_rejection_writer.save` accepts an explicitly supplied rejection identity,
owner, event and observed physical revision, cited reference, reason and request ID in
an active-owner writer transaction. It verifies the complete retained event and
rejection history, exact physical pins and source-audit chronology; a reused identity,
conflicting retained decision, orphan audit or inconsistent history fails closed. An
explicit rejection may refer to the retained event even after a candidate is dismissed;
this does not restore that candidate or assert its source is current.

The writer appends the rejection and its canonical source/reference audit and
invalidates affected portfolios from the event's ex-date in one savepoint. A caught
body failure rolls back the whole rejection operation; an uncertain savepoint outcome
aborts the enclosing transaction. Existing entitlements, projections, receipts, cash,
locked FX and prior decisions are not rewritten. An exact internal retry with the same
rejection identity, request and unredacted intent returns the audited historical result
without another write, even if the candidate is later dismissed. A changed intent or
another decision for the same revision does not silently replace the original.

This internal writer does **not** authenticate a session, perform CSRF or password
checks, establish complete holdings, verify cited documents, cancel income or itself
resolve a rejection. The protected HTTP decision below calls it only after those request
security checks and the additional financial-history fence. Do not invoke this internal
primitive directly on deployment data; necessary atomic projection/receipt reconciliation
remains unfinished.

## Explicit internal rejection resolution

Migration `0024_dividend_resolutions` adds one initially empty immutable resolution table
and brings this schema revision to 62 business tables. It preserves previous events,
rejections, projections, entitlements, receipts, cash, tax settings, locked FX and
existing audit records. The event writer now requires the exact observed rejection ID
before it can publish a rejected revision's immediate physical successor. An incorrect
or absent pin does not silently clear a rejection. The successor revision's audit binds
both the rejection and resolution identities; a separate correlated audit binds the
original rejection fingerprint and the exact successor fingerprint. Earlier ordinary
and manual-override audit fingerprints remain unchanged. Adding a marker later cannot
reclassify an ordinary revision as a resolution. The historical event and rejection
readers verify the complete chain, including multiple rejection/resolution pairs, before
returning a retained result. Matching hashes establish local consistency, not an
authenticated request, verified document or economic grant.

The successor, resolution, both audits, review updates and affected portfolio
invalidations are committed in one savepoint within the caller's writer transaction.
A caught failure rolls back the entire operation; an uncertain savepoint outcome aborts
the outer transaction. An identical internal replay validates the full retained history
and original rejection pin without writing. A later rejection may cite a previously
resolved successor without erasing either earlier decision. Rejecting or resolving does
not alter the event representation at the moment of rejection; publishing the successor
advances its normal event version.

Protected manual revisions, existing-event candidate adoption, acceptance, manual
override and withdrawal accept an optional `resolve_rejection_id` naming the exact
observed rejection. They still require the event's physical revision and `If-Match`
version, plus their existing confirmation, current-source and authorization checks.
An absent pin for a rejected current revision returns 409; a mismatched pin returns
412. A pin without a current rejection is also rejected. The pin is part of the
idempotent intent when supplied; absent and explicit null preserve the earlier
request fingerprint, so old completed requests remain replayable without writes.
The event ETag changes only when the successor is published, not when a rejection
is recorded. The separate protected rejection reads described below expose retained
pins; do not insert rejection rows directly into deployment data to obtain one.

Both the application and SQL publication fences still prevent a successor whenever any
prior revision has an entitlement or the event has projection history. They never detach,
rewrite or reverse earlier receipts, cash or locked FX. The internal primitive does not
authenticate an HTTP session, confirm ownership, validate eligibility or post income;
atomic projection/receipt reconciliation remains unfinished.

## Protected rejection-state and history reads

An authenticated owner can read `GET /api/v1/dividends/events/{event_id}/rejection`
for the current physical event revision and its separate `NOT_REJECTED` or `REJECTED`
state. A rejected response includes its immutable decision ID, cited reference and
reason. `NOT_REJECTED` means only that this revision has no retained rejection; it
is not proof of verified documents, complete holdings, eligibility or current economic
use. The response explicitly labels those boundaries. It does not replace the writer's
same-transaction source, eligibility or rejection checks.

`GET /api/v1/dividends/events/{event_id}/rejections` returns bounded, descending
revision-keyset pages of retained decisions. A resolved item identifies its audited
immediate successor and resolution; an unresolved current decision has no resolution.
Search covers only rejection IDs and revision numbers, not private citations or reasons.
Both reads validate the complete retained event, rejection and resolution history in one
SQLite snapshot, including records outside a requested page. Missing or foreign events
return 404 and inconsistent histories fail closed with 503. URL query strings and
fragments are withheld from responses; private decisions use `Cache-Control: no-store`
and have no event ETag. Recording a rejection leaves the ordinary event response and its
strong ETag unchanged. The reads do not change dividend facts or contact providers;
ordinary session activity may refresh the session's last-seen time.

These read resources expose retained decisions but do not themselves create or resolve
one. Atomic projection/receipt reconciliation and the dividend UI remain unfinished.

## Protected rejection decision

`POST /api/v1/dividends/{event_id}/verify` accepts `decision=REJECT_REVISION` with the
observed physical `revision_id`, quoted event version in `If-Match`, an owner citation,
reason and optional `evidence_url`. It requires the active owner session, matching
Origin, CSRF, an idempotency key and recent password confirmation, including on replay.
The serialized writer rechecks those credentials and the exact current event revision.
It can reject a retained decision even if its candidate was later dismissed, without
asserting that candidate or its documents are current.

The rejection and its canonical source/reference audit, a separate HTTP-intent audit,
affected portfolio invalidations and the serialized idempotency response commit together.
The intent audit binds the exact rejection and event identities, observed revision,
request route, key digest and original unredacted intent digest. A completed unchanged
retry verifies the complete event/rejection/resolution history and original intent,
reauthenticates and returns its historical result without writing. A changed intent with
the same key conflicts; stale event pins return 412; corrupt retained history fails
closed. An uncertain savepoint or response failure leaves no partial decision or key.

For source-contract compatibility the response is the **unchanged event representation**
with its original event ETag, not a mutable rejection flag. Read the separate rejection
state resource for its exact rejection ID before an explicit successor resolution. A
rejection is not withdrawal, replacement terms, zero income, verified documents or an
eligibility grant. It does not post or reverse receipts, cash or locked FX.

Until atomic projection/receipt reconciliation is installed, a **new** rejection is
refused with 409 `DIVIDEND_EVENT_PROJECTION_REQUIRED` when any historical event revision
has an entitlement or the event has any projection history. It is also refused when the
event version cannot advance. These fences avoid creating a rejected state that cannot
currently be resolved safely. They do not prevent an unchanged historical replay after
a later valid successor and subsequent receipt. A later workflow must implement the full
atomic rebuild and reconciliation rather than silently dropping these safeguards.

## Inert fee-setting provenance storage

Migration `0025_dividend_fee_settings` adds one initially empty immutable table, bringing
this schema revision to 63 business tables. It does not backfill the broker's current
fee, alter earlier projection fingerprints, or change event decisions, tax settings,
entitlements, receipts, cash, locked FX or old audit records. Each assertion belongs
to one owner, physical event revision and account, and retains the observed portfolio,
broker and their account/broker versions, an explicit effective-from day, a unique
setting number, predecessor, request and correlated audit shape.

`ESTIMATED` requires an explicit nonnegative six-place fee and HKD or USD currency;
explicit zero is not the same as missing. `UNKNOWN` has no amount but can retain an
explicitly known supported currency, or no currency when that too is unknown. A source
currency outside the supported set must not be silently replaced or converted. The
current broker fee may be an omitted default zero and does not establish a historical
fee estimate. Settings are not automatically inherited by a new physical event revision
or another account.

The SQL guards require the current owned event revision, consistent active owner and
account/broker scope, an unrejected revision, a contiguous setting chain, valid explicit
amount/currency and a correlated audit shape; updates, deletes and replacement writes are
blocked. Database constraints and shaped hashes do not authenticate a caller, verify an
audit's canonical contents, select an effective setting, prove actual broker charges or
grant eligibility. Do not insert fee rows directly into deployment data; use the
protected owner API below. New versioned projection snapshots can retain explicit fee
provenance as described below.
Before any economic projection, independently revalidate the exact event, source,
rejection state, recorded eligibility, selected effective-day fee and observed history
head, account/broker versions and any explicit fee-FX provenance. Later backdated
assertions must not relabel retained snapshots.

## Pure fee-estimate selection

`dividend_fee_service.select` operates only on caller-supplied `FeeSetting` values
and an explicit effective calendar day. It validates the complete supplied revision/account
chain or explicitly supplied historical prefix, predecessor and sequence, unique setting,
request and audit identities, chronology, scope and exact amounts before selecting the
latest effective day and then latest setting number. It retains the observed head number
and identity independently of the selected record, so a future or backdated assertion is
not silently forgotten. `MISSING`, `UNKNOWN` and an explicitly estimated zero remain
separate; an unknown amount may retain an explicitly known currency.

This pure calculation returns `UNVERIFIED_FEE_SELECTION_ONLY`. It does not read the
database, authenticate an owner, verify an audit or broker charge, check current account
or broker associations, calculate fee FX, establish eligibility or authorize income.
A caller using a historical prefix must have independently established its exact retained
head; a future economic writer must revalidate current scope and the complete audited
history in the same transaction.

## Internal fee-estimate history reader

`dividend_fee_service.history` reads a complete physical-revision/account fee chain
inside an existing database transaction. It validates the retained event and
rejection/resolution histories, exact setting scope and chronology, original
portfolio/broker ownership and observed version bounds, unique correlated audits,
canonical amounts and the entire predecessor chain. Its canonical audit fingerprint
binds the physical identities, versions, effective day, explicit amount/currency,
request and timestamp, a reason hash and the `OWNER_ESTIMATE_ONLY` label. The audit
ID is correlated separately, and its canonical timestamp cannot precede the setting.
A later inconsistent audit or setting fails the whole history even when an earlier
effective day is selected.

An owned scope with no settings returns an empty tuple; a missing or foreign event,
account or requested physical revision returns `None`. Neither means an estimated
zero. Rejected and subsequently resolved revisions can still be read historically,
but the read does not clear a rejection or make an old fee usable. The account or
broker may have changed since a setting; retained ownership and version bounds do not
prove their historical association, authenticate the caller or verify a broker charge.
The reader itself does not authenticate, calculate FX, establish eligibility or grant
income. Current use requires revalidation of all relevant pins and the rejection/source
state in the same writer transaction. The reader makes no provider access or database
mutation.

## Internal fee-estimate controller

`dividend_fee_writer.save` accepts an explicit owner assertion and its observed physical
revision, account, portfolio, broker, account/broker versions and exact fee-history
head number and ID. In a serialized writer it validates the complete retained history,
current event source and rejection state before appending the setting, correlated audit
and affected portfolio invalidation in one safe savepoint. An estimated amount requires
an explicit supported currency; unknown has no amount and may retain a known currency.
A new setting cannot predate the preceding setting or its correlated audit. A caught
failure rolls back the whole decision; uncertain savepoint failures abort the outer
writer. No HTTP or provider configuration is accessed inside the transaction.

An identical internal request and intent may replay its original historical setting
without writing, even after a later rejection, successor or account change. Such replay
is not a grant to use the setting now. Changed requests, reused identities, orphan audits,
stale heads and corrupt histories fail closed. `require_current` independently rechecks
the current event source, rejection state, complete fee chain, explicit effective day,
head and caller-observed portfolio/broker identities and versions, including when no
setting is selected. A selected assertion must still match the current account scope.
The result remains `UNVERIFIED_FEE_SELECTION_ONLY`; in particular, `MISSING` or
`UNKNOWN` is not zero. Callers must separately authenticate, establish eligibility and
verify all other tax, fee-FX, valuation and projection requirements. There is no confirmed
charge, authenticated economic grant or receipt writer.

## Protected fee-estimate API

For an authenticated owner and an explicit calendar day, `GET
/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings?effective_on=YYYY-MM-DD`
returns the current physical event revision, exact-string account/broker versions,
fee-history head number and ID, selected estimate and its account-scope label. An
unselected chain reports `MISSING`, not zero. The independent `revision_rejection` is
`REJECTED` or `NOT_REJECTED`; neither state proves document authenticity, current source
validity, eligibility or a charge. An earlier setting may be shown as
`ACCOUNT_SCOPE_CHANGED` after the account or broker changes. Every selection and setting
is labeled `OWNER_ESTIMATE_ONLY` and `REVALIDATION_REQUIRED`.

`GET .../fee-settings/history?revision_id={uuid}&revision_no={n}` returns bounded
keyset-paginated historical settings for the exact physical revision. Search matches
only setting ID or number, not private reasons. History remains readable after a later
rejection or successor, but validates the complete retained event/rejection/fee chain
in one snapshot. Missing or foreign scopes return 404; inconsistent history returns a
fixed 503. Reads are private and `Cache-Control: no-store`. Ordinary authentication may
refresh an idle session's `last_seen_at`; the fee read itself does not write financial
facts or contact a provider.

`POST .../fee-settings` requires the owner session, an allowed Origin, CSRF token,
Idempotency-Key, recent reauthentication and `If-Match` containing the observed event
version. Its JSON body includes the physical `revision_id`, `portfolio_id`, `broker_id`,
canonical decimal-string `account_version`, `broker_version`, `expected_head_no` and
explicit `expected_head_id` (`null` only for head `"0"`), plus `effective_from`,
`fee_status`, `fee`, `fee_currency` and a reason. For example, an explicitly unknown
amount with a known currency uses `"fee_status":"UNKNOWN"`, `"fee":null` and
`"fee_currency":"USD"`; an `ESTIMATED` amount is a nonnegative decimal string with
at most six fractional places and a required HKD/USD currency. An explicit `"0"` is an
estimate, not missing or unknown. Unsupported currencies and invalid precision are
rejected, not converted or rounded. INT64 versions and head numbers stay strings on the
wire so JavaScript does not lose digits.

The write rechecks session/CSRF/reauthentication, event version and physical revision,
source, rejection, account/broker versions and exact fee head inside the writer. The
fee assertion, canonical setting audit, separate HTTP-intent audit, invalidation and
idempotency response commit together. The HTTP audit binds the route, idempotency-key
digest, original normalized intent and physical setting identity; replay verifies the
complete history and binding before returning the original response without new writes.
A replay after a rejection or event successor is only a historical receipt, not renewed
current authority. Changed inputs on a reused key conflict; stale event/head pins return
412, source/rejection/account conflicts return 409, invalid inputs return 422, and
corrupt history fails closed with 503. Neither the write nor its replay verifies broker
charges or creates a dividend entitlement, projection, receipt, cash movement or FX rate.

## Projection fee provenance

Migration `0026_dividend_projection_fee` adds six nullable fee-provenance columns to
immutable projection versions without adding a business table. Existing projections,
fee settings, event decisions, entitlements, receipts, cash, historical FX and prior
audit fingerprints remain unchanged. Legacy rows retain unavailable provenance; an
old amount, a broker default or an existing fingerprint is not proof of an effective
fee setting or an actual broker charge. Provenance cannot be attached to an old
immutable projection after the fact.

A version 1 snapshot retains an explicit fee-effective day, observed fee-history head
number and identity, selected setting identity and independent `MISSING`, `UNKNOWN` or
`ESTIMATED` status. An empty head differs from a nonempty head with no applicable
setting. `EXPECTED` projections require an `ESTIMATED` setting whose original amount
and currency match the snapshot exactly; missing or unknown cannot become a zero fee.
Non-amount states do not acquire a financial amount. SQL checks the complete observed
head, effective-day selection, owned account/broker scope and each observed setting and
correlated audit timestamp. SQL shape is not authentication or canonical audit proof.

The historical projection reader validates the complete fee and event/rejection chains,
then repeats the original selection from the snapshot's explicitly retained prefix.
Later backdated settings do not relabel an old projection, while a later corrupt audit
fails the full read closed. Version 1 fee fields participate in that projection's
canonical audit fingerprint, and unversioned legacy fingerprints are preserved. The
original fee currency and fee-to-event FX rate/source are separate from the independent
HKD valuation FX already retained by the projection. A nonzero foreign fee needs its
explicit conversion; an explicit foreign zero needs no invented rate. These rates and
owner estimates are retained assertions, not proof of actual broker charges or market
FX. The internal recorded-projection writer below cannot authenticate or post a receipt;
event publication remains fenced until atomic rebuilding and reconciliation exist.

## Internal recorded-projection writer

`dividend_projection_writer.save` builds one immutable event/account projection from
recorded facts in an existing `BEGIN IMMEDIATE` writer transaction. The caller supplies
an active owner, new projection ID and request ID; the observed event physical revision,
account/portfolio/broker IDs and versions, portfolio data revision and projection head;
an exchange-local `as_of` and explicit eligibility rule; independent tax and fee
effective days, setting-head numbers and IDs, and selected-setting IDs; optional
fee-to-event and event-to-HKD FX rates with their sources; and a reason. Financial
values are decimal strings, not binary floats. Version numbers remain integers inside
this internal API; any future HTTP contract must preserve exact INT64 digits as strings.

Before writing, the service rechecks the complete retained projection and source
histories, current event/candidate/basis, rejection state, account/broker scope, the
portfolio revision, recorded trade/action/transfer history and both full tax and fee
setting chains. A newly created projection cannot predate the recorded source,
rejection, ledger, origin/review, settings, audit or portfolio-invalidation timestamps
it depends on. These timestamps are retained assertions and do not prove when a source
was published, authenticate an actor or establish complete ownership. The derived
snapshot and separate normalized-intent audit are appended atomically in a safe
savepoint. If a later operation in the same transaction changes an input, a caller must
revalidate it again before use. No provider request, password hashing, receipt, cash
posting or old financial fact is made or changed.

A positive recorded quantity with explicit `ESTIMATED` tax and fee can produce an
`EXPECTED` calculation. `MISSING` or `UNKNOWN` tax or fees, an affected unresolved
action, unresolved eligibility/basis, unverified retired-trade origin or a reviewed
creation gap produces `NEEDS_REVIEW` instead of an invented zero or amount. An empty
recorded position produces `NO_POSITION`, not proof of absent ownership; a withdrawn
event produces `CANCELLED`. Every result retains `history_completeness=UNVERIFIED`.
If `as_of` precedes the ex-date, even an `EXPECTED` result describes recorded holdings
on that day, not accrued income or final eligible shares. The caller must explicitly
choose review for uncertain due-bill or other nonordinary conventions.

Same-currency identity conversion is exact. A nonzero fee in a different currency
needs its own explicit conversion; an explicitly estimated zero needs no invented
fee rate. Missing USD-to-HKD valuation FX leaves the HKD amount unavailable with
`MISSING_FX`, not a default rate or zero. The snapshot retains the independent owner
estimates and conversion sources; computational `OK` is not a confirmed charge,
withholding or live-market FX.

An identical request and normalized intent replays its original validated historical
projection without writing, even after a later ledger or setting change. The replay
also checks the separate intent audit against the physical projection, including its
observed heads and FX. A changed request or reused identity conflicts; later corrupt
history fails closed. Historical replay is not a current-use grant. Derived snapshots
do not advance their own input `data_revision` or enqueue a new rebuild, which would
instantly stale the snapshot; a future authenticated client must invalidate its own
query caches and independently revalidate current facts before economic use.

This is an internal primitive, not a session/Origin/CSRF/reauthenticated API, receipt
reconciliation or a dividend UI. The separate current-recorded-input fence below does
not grant income or authenticate a caller. Do not invoke these primitives on deployment
data. Existing receipts and locked FX are preserved; creating a snapshot also retains
the event-revision publication fence until authenticated atomic rebuilding and receipt
reconciliation are installed.

## Internal current-recorded-projection fence

`dividend_projection_current.require_current` revalidates one supplied projection ID
and exact projection number for one owned event/account and explicitly supplied `as_of`
day in an existing active-owner `BEGIN IMMEDIATE` transaction. It reads the complete
retained projection history and requires the supplied projection to be the latest
physical version; a different as-of day or stale head is not silently substituted.
Legacy projections without both versioned tax and fee provenance remain unavailable for
this check rather than inheriting guessed settings.

The fence rechecks current event/candidate/basis and rejection scope, account/portfolio/
broker identities and versions, portfolio data revision, both full effective-day setting
chains and the original observed heads. It replays relevant recorded trades, transfers
and actions, checks the retained evidence and invalidation chronology, and recomputes the
projection state and components against the stored snapshot. This catches changed
shares or later source/review evidence even if `data_revision` alone appears unchanged.
Fee-to-event and HKD valuation FX remain independent retained assertions; the fence
does not refresh them or verify a market rate. A concurrent writer cannot commit an
input change while the fence holds its writer transaction. A caller that changes any
relevant input within that transaction must repeat the check before using the result.

The operation makes no writes. The returned record still has
`history_completeness=UNVERIFIED` and `scope=UNVERIFIED_PROJECTION_HISTORY_ONLY`.
This is not proof that the original writer was authenticated, that the owner actually
held all shares, that a cited source or tax/fee estimate is true, or that a receipt
may be posted. It reconstructs current calculation inputs from the retained snapshot;
it does not recover or authenticate the original writer's private request intent.
Callers must separately enforce session/Origin/CSRF/reauthentication, ex-date recognition,
receipt reconciliation and all other economic checks. The separate protected history
read API below does not expose this current-use fence or install a receipt workflow.

## Internal retained-projection history reader

`dividend_projection_history.history` reads one supplied owner, event and account in
a single existing transaction. It checks the complete retained event and projection
chains, original portfolio/broker associations, canonical timestamps, correlated
projection audit and exact component arithmetic. It preserves each physical version;
it does not sum prior amounts or select a current financial entitlement. A missing or
foreign event/account returns `None`; an owned scope with no projections returns an
empty history, not a zero entitlement. Unreadable or inconsistent history fails with
a fixed error, and private financial values and reasons are omitted from representations.

The projection audit fingerprint covers the retained snapshot fields, physical event
revision number, a hash of the reason and a fixed unverified-scope label. Independently
versioned provenance fields also cover the explicit tax and fee selections above; the
audit ID is correlated separately.
Future schema changes must preserve the fingerprint contract for existing rows. A
matching hash only verifies consistency with the stored audit: it does not prove that
the actor was authenticated, the account actually held the stated shares, the fee
setting was effective or the owner estimate represents actual withholding. The reader returns
`history_completeness=UNVERIFIED` and `scope=UNVERIFIED_PROJECTION_HISTORY_ONLY`;
its stored trade-history labels are also unverified assertions. An inactive owner's
history can remain readable internally but cannot pass the active-owner current-use
fence. Callers must authenticate separately and recheck the event, ledger, eligibility,
account configuration and authorization in a writer transaction before economic use.
There is no authenticated atomic event-revision rebuild or receipt writer; event publication
remains fenced, and computational `OK` is not confirmed tax. This read performs no writes
or provider access.

## Protected projection-history read API

`GET /api/v1/dividends/events/{event_id}/accounts/{account_id}/projections` requires
an active owner session and returns private `Cache-Control: no-store` keyset-paginated
retained versions in descending projection number. The reader validates the complete
owned history and source/audit chain in one snapshot before filtering or pagination;
a corrupt later record cannot be hidden by a search. An absent or foreign event/account
returns 404; an owned scope with no records returns an empty page, not a zero entitlement.

Every record retains the historical physical event revision, explicit `as_of` and that
revision's ex-date. `timing_scope` distinguishes a pre-ex-date recorded-holdings view
from a later recorded-history view. `recorded_shares` and `estimated_amounts` are exact
strings only when the retained state permits them; missing calculations and rates remain
null. `NO_POSITION` is an unverified recorded-history state, not proof of no holdings.
`quality=OK` describes arithmetic only. Tax and fee provenance include the original
observed head and explicit missing/unknown/estimated selection, or null for legacy
snapshots without retained provenance; no setting or FX is inferred. Projection, account,
broker and data versions are canonical INT64 strings.

All records explicitly carry `UNVERIFIED_PROJECTION_HISTORY_ONLY`,
`REVALIDATION_REQUIRED`, `history_completeness=UNVERIFIED` and
`eligibility_scope=NOT_ESTABLISHED`. Historical results may be stale, do not authenticate
the original writer, and are neither current entitlement nor received income. This read
never appends projections, contacts providers, posts cash or modifies receipts/FX. The
separate writer below does not establish a current-use grant or atomic reconciliation.

## Projection history in the browser

In the dividend event workspace, expand an event and select **View account projection
history**. Choose an account explicitly; the browser then reads bounded pages of that
stable event/account's recorded versions. It displays exact string-valued versions,
historical physical event revision, as-of/ex-date timing, recorded shares and estimated
components only when present, and separate missing/unknown tax and fee provenance. A
pre-ex-date snapshot is labeled recorded holdings, not accrued income. Missing HKD FX
is shown as unknown rather than zero. A different historical event revision or prior
account/broker scope is not silently relabeled as current, and versions are not summed.

The component validates response identity, ordering, state, labels, exact decimal bounds,
component arithmetic and basic FX/provenance consistency before display. Account/page
scopes and results remain in mounted component memory rather than persistent browser
storage or a query cache. Failed or offline reads are labeled stale and disable pagination;
late responses from a previously selected account are discarded. Reading this view does
not create a projection, entitlement, receipt, cash entry or current-use grant. Recording
a projection is a separate explicit action described below.

## Record a projection in the browser

After explicitly choosing an account in the projection-history workspace, select
**Prepare to record a projection**. Enter the as-of date, the recorded-history rule, and
separate tax and fee effective dates; none defaults to today or the ex-date. Choose
**Check projection inputs** to read one private input snapshot and separate tax/fee
selection details. These three reads are not one atomic snapshot. The form can open only
when their physical event, account, version, setting heads, selected IDs and rejection
states agree, the account scope remains valid, and the reads are neither stale nor paused.
A future setting head with no selected setting remains missing, not zero. The server
revalidates all inputs again in its serialized write transaction.

The password-confirmed sheet freezes the observed event, account, dates and exact pins.
It shows the selected owner estimates and their physical IDs, not confirmed tax or fees.
For each applicable cross-currency rate, explicitly choose either **unknown** or a
positive exact decimal and its source. The HKD valuation rate and fee-to-event rate are
independent; no rate is defaulted or inferred. Same-currency conversion uses the
mathematical identity rate, and a fee with unknown currency cannot be converted. A
pre-ex-date snapshot is recorded holdings, not accrued income.

**Important:** recording any projection, including one needing review or with no recorded
position, currently prevents revision, withdrawal and rejection of that event. Projection
history cannot be deleted to remove this fence; a supported atomic reconciliation workflow
does not yet exist. The form requires a separate acknowledgment of this limitation and
another acknowledgment that the result is not confirmed eligibility, receivable income or
receipt. Do not record a projection merely to try the feature on an event you may need to
change.

If the response is lost or cannot be safely verified, keep the sheet open and retry the
unchanged intent with its original in-memory key. Reauthentication may be required again.
A successful reply may be a historical replay, not the current projection; close only after
considering whether to retry and inspect the retained history. Private inputs and keys are
not saved in browser storage or query/mutation caches. The workflow does not create a
receipt, cash movement or entitlement, and atomic event revision and receipt reconciliation
remain unfinished.

## Protected projection-input snapshot

`GET /api/v1/dividends/events/{event_id}/accounts/{account_id}/projection-inputs`
requires an owner session and the explicit query parameters `as_of`, `eligibility_rule`,
`tax_effective_on` and `fee_effective_on`. The response is private and `no-store`.
It returns the current physical event revision, event ex-date/currency, owned account,
portfolio and broker identities, exact-string account/broker/data versions, and the
latest retained projection head. The selected eligibility rule and all requested dates
are echoed without choosing them for the owner.

The same WAL snapshot validates the complete retained projection, tax, fee and rejection
histories before reporting each explicit effective-day selection, observed setting head,
selected physical setting ID, status and account-scope match. `MISSING`, `UNKNOWN`
and `ESTIMATED` remain distinct; a future-dated head does not imply a selected setting.
An independently rejected current revision is labeled `REJECTED`; `NOT_REJECTED` is
not event acceptance. An absent or foreign event/account returns 404, while an unreadable
or inconsistent history returns a fixed private 503. A subsequent write must recheck
all pins in its own serialized transaction; these reads may become stale immediately.

The response states `RECORDED_PROJECTION_INPUTS_ONLY`, `REVALIDATION_REQUIRED`,
`CURRENT_SOURCE_NOT_REVALIDATED` and `eligibility_scope=NOT_ESTABLISHED`. It does not
verify current sources or historical share ownership, compute eligibility, choose exchange
rates, post income, change receipts or contact providers. Atomic event-revision/receipt
reconciliation remains unfinished.

## Protected recorded-projection writer

`POST /api/v1/dividends/events/{event_id}/accounts/{account_id}/projections` requires
an active owner session, Origin and CSRF validation, an idempotency key, a quoted event
`If-Match` and recent password confirmation. It rechecks session, CSRF and reauthentication
inside the serialized writer before either a new operation or a cached replay. The body
requires `RECORDED_PROJECTION_ONLY` and a separate `confirm_revision_fence=true`, the
observed physical event revision, account, portfolio, broker and exact-string data/head
versions, explicit as-of/rule and tax/fee selection pins, independent optional FX rates
and sources, and a reason. All required nullable FX and selection fields must be supplied
explicitly; no missing rate is guessed.

**Recording any projection currently fences new event revisions, withdrawals and
rejections**, even if the snapshot is `NEEDS_REVIEW` and no receipt exists. Projection
history is append-only; there is currently no supported way to remove that fence.
The explicit acknowledgment is required so an API client does not create this constraint
without the owner's knowledge. Atomic event-revision reconciliation is needed to allow
safe changes later.

For a new request the internal writer revalidates current event/source, rejection, ledger,
account, settings and projection history in the same transaction. The snapshot, its
canonical audits, a separate route/key/normalized-intent binding audit and the serialized
idempotency response commit together or roll back together. Changed pins are rejected;
unavailable or inconsistent history returns a private fixed 503. An unchanged retry
validates the complete retained chain and both the original intent and HTTP binding, and
returns the original historical projection with no durable writes, even after account or
setting changes. A matching replay is not current-use authority.

Every returned snapshot retains the history read's unverified/no-grant labels. Before
ex-date it is only recorded holdings, not accrued income; arithmetic `OK` does not verify
tax, fee or FX. The writer posts no receipt or cash, does not rewrite historical facts or
locked FX, and cannot advance a financially fenced event revision. Atomic event-revision
rebuild and stable-event/account/currency receipt reconciliation are still required.

## Internal retained-receipt link reader

The internal `dividend_receipt_history.history` reader resolves all retained entitlement
and receipt rows for an owned managed event and account through their original physical
revisions, rather than joining receipts only to the current event revision. It preserves
original currencies, amounts, status, tax/fee fields and locked FX without netting unlike
currencies or adding historical entitlements together. Missing recorded HKD values remain
unknown. A receipt with no cash link is explicitly `CASH_NOT_RECORDED`; a void whose
matching primary lacks a reversal is `CASH_REVERSAL_NOT_RECORDED`. Matching primary and
reversal cash structures have explicitly **unverified** labels, not a claim that the
receipt, actor or financial history is authentic. Contradictory account or cash links and
malformed retained values fail closed rather than being silently skipped.

The entire read requires an existing database transaction and validates the managed
event revision chain and current account ownership in one snapshot. It cannot prove
historical account ownership, the completeness of receipt or cash history, external
payment or the legacy receipt/entitlement audit content. Even a retained legacy quality
called `CONFIRMED` is only a recorded label, not newly established eligibility. This is an
internal building block, not a session-authorized API, current-use grant, receipt writer or
reconciliation result. It does not modify facts, cash, FX, audits, providers or event fences.

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
still-current trade. Before a correction it checks the retained root's owner,
sequence, revision links and available audits, including each retained original's
fingerprint against its actual successor. A missing creation/revision audit in
an otherwise readable root returns 409 `NEEDS_REVIEW`; inconsistent available
evidence or an unverifiable review history returns 503 `TRADE_AUDIT_UNAVAILABLE`.
These failures do not change the fact, cash, origin, audit, idempotency or
invalidation. A retired trade without a retained original remains unverified:
the writer cannot reconstruct the original fingerprint input from the retired
row alone. The separate unverified review workflow records an acknowledgment or
withdrawal but cannot yet authorize that blocked correction. Recovery of an
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
explicit unverified review API below; review alone does not unblock ordinary
PATCH/DELETE or authorize the separate correction operation.

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
separate explicit review API and browser workspace below remain unverified.

## Inert unverified trade-history review storage

Migration `0018_trade_history_reviews` adds an initially empty append-only review
decision table, bringing that schema revision to 57 business tables. It does not
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

## Inert unverified correction authorization storage

Migration `0019_trade_correction_scope` adds an initially empty, append-only
`trade_correction_authorizations` table, bringing that schema revision to 58 business
tables. No existing fact, review, receipt or audit is rewritten. A record stores
one corrected physical trade, the resulting revision or void, the correlated
request and audit, an intent hash, and a JSON array of review pins. Each pin
identifies its missing-creation trade, physical version, review ID, exact
review-revision string and gap. SQL checks the owner/account/root and resulting
trade-audit links, the latest acknowledgment, one pin per currently missing
creation audit, canonical field shapes and append-only behavior. It rejects
withdrawn reviews and later creation evidence for a new record; an existing
record remains historical evidence after later withdrawal.

SQL cannot authenticate a user, prove that an audit was created in the original
transaction, validate the canonical intent or audit digest, or verify all economic
scope. A valid-shaped but unrelated audit hash can pass the table guards. A
protected API uses the internal writer below, but inserting a row alone is **not**
a correction authorization or eligibility grant. Callers must recheck the entire
scope and append the fact, correlated audit, idempotency and invalidation atomically.

## Internal historical correction-authorization reader

The private `trade_correction_authorization` reader validates an owned record's
canonical scope and audit fingerprint, exact review decisions, root revision
chain and all available trade origins. It checks the target's actual retirement
fingerprint against its reviewed snapshot or retained origin, and the resulting
revision's creation and later retirement audits. Missing historical origins stay
unverified; malformed or contradictory retained history fails closed. A valid
historical record remains readable after a later withdrawal or a matching audit
appears, but neither event reauthorizes a new correction. The retained intent
hash binds the audit record but cannot by itself reconstruct or authenticate the
original request. The reader does not authenticate HTTP, establish ownership or
income, or grant current use. The internal writer below uses it to verify its
newly recorded authorization and historical replay.

## Internal unverified trade-correction writer

The private `trade_unverified_correction` writer accepts an explicit revision or
void, a reason and exact physical/review pins for every missing-creation gap in the
owned root. It rechecks the complete current scope inside the writer transaction;
withdrawn decisions, changed evidence and stale or incomplete pins do not grant a
new correction. It never invents the target's missing creation audit or original
state. If the target itself has a matching creation audit but an ancestor has a
gap, its verified original is captured before retirement as usual.

The ordinary financial replay and exact settlement reversal run before the writer
appends a canonical authorization audit and immutable scope record. A nested
savepoint rolls back facts, cash, audit, origins and invalidations even if a caller
catches an error. If starting, rolling back or releasing that savepoint fails, the
entire enclosing transaction is aborted; a failed outer rollback invalidates the
connection. Trade-origin recovery and trade-history review use the same guard.
Repeating the same internal request ID and normalized intent
returns the validated original result without writes, including after a later
withdrawal; changing the intent conflicts. A separate historical replay helper
rechecks the original authorization and result without making a new grant.

These functions do **not** authenticate a session, check CSRF or reauthentication,
or write an HTTP idempotency receipt. The separate protected API below supplies
those checks in the same outer transaction and serializes the response before
commit. Normal trade PATCH/DELETE remain fenced. Neither a successful correction
nor its audit proves ownership or authorizes an eligibility projection.

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

These helpers do not authenticate an HTTP session or establish ownership history
or complete economic scope. The correction-scope checker also validates the
retained trade root, without granting a correction. The protected API and browser
workspace below authenticate review decisions; a separate protected correction API
uses the internal writer. Independent eligibility integration remains unfinished.
A financial caller must consult review history even when an audit later appears,
and revalidate the complete relevant scope in its own transaction. No provider or
private-file access is performed.

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
an entitlement or posts a receipt. Before correcting or voiding a trade, the
writer validates all review histories attached to the same stable trade root. If
any recorded decision reported a missing creation audit, it returns 409
`NEEDS_REVIEW` even after withdrawal, a matching later audit or a successor
revision; malformed review history returns 503 `TRADE_AUDIT_UNAVAILABLE`. This
fence does not prove when an audit was created, resolve the historical gap, grant
eligibility. A separate protected correction API below requires a distinct
explicit confirmation and exact current scope; a review by itself is not that
confirmation. The internal scope checker revalidates all missing-creation reviews
against exact physical and review versions in a writer transaction; it rejects
withdrawn decisions, extra or omitted trades and later matching audits. It does
not authenticate the caller, record an authorization or change a financial fact
by itself.

## Protected unverified trade-correction API

`GET /api/v1/trades/{id}/unverified-corrections/scope` reads the current owned
physical trade and its complete retained root in one consistent transaction. It
returns the target and root IDs, physical version/status, and each relevant
missing-creation history in revision order. Each entry includes the physical
trade/version, exact decimal-string review revision, current evidence gap and one
of `NOT_REVIEWED`, `ACKNOWLEDGED_UNVERIFIED`, `WITHDRAWN` or `EVIDENCE_CHANGED`.
An original missing-creation gap remains `EVIDENCE_CHANGED` if a creation audit
later appears, even if the owner subsequently withdrew the old decision or
reviewed a different retired-origin gap. A zero revision means no decision was
recorded. The response deliberately omits reasons, amounts and audit hashes.

An empty list means this correction-scope check found no missing-creation gap;
it does **not** establish complete history, ownership or eligibility. The result
is an observed snapshot, not a grant: the write still rechecks the entire root.
The endpoint returns 409 for an inactive target or more than 100 scope entries,
without silently returning a partial list. Malformed history fails closed with 503;
foreign trades return 404. The response has no ETag, is private and `no-store`,
and explicitly labels its scope and history `UNVERIFIED`.

`POST /api/v1/trades/{id}/unverified-corrections/revise` and
`POST /api/v1/trades/{id}/unverified-corrections/void` are separate from normal
PATCH/DELETE. Both require the current session, matching Origin, CSRF token,
idempotency key, recent password confirmation and a quoted physical trade version
in `If-Match`. Their bodies must contain the explicit confirmation
`UNVERIFIED_TRADE_CORRECTION_ONLY` and 1–100 distinct `reviews` pins. Each pin
names a physical `trade_id`, its currently observed `trade_version`, a positive
canonical decimal-string `review_revision` and `gap=MISSING_CREATION_AUDIT`.
These are the reviews for **every** missing-creation gap in the stable root,
including retired ancestors; they are not merely the current trade's review.

For example, a void of one reviewed synthetic draft uses:

```json
{
  "confirmation": "UNVERIFIED_TRADE_CORRECTION_ONLY",
  "reviews": [
    {
      "trade_id": "00000000-0000-4000-8000-000000000001",
      "trade_version": 1,
      "review_revision": "1",
      "gap": "MISSING_CREATION_AUDIT"
    }
  ],
  "reason": "Synthetic example: void an explicitly unverified trade"
}
```

A revision uses the same confirmation and pins, with `revision` containing the
complete normal trade-create payload plus its reason. Decimal inputs remain
strings. Assemble pins from the complete scope response and inspect individual
review history when necessary. Every pin must still correspond to an acknowledged
current missing-creation gap. A current acknowledgment is not proof of complete
history. Withdrawn reviews, omitted or extra gaps, stale physical or review
versions, and creation evidence appearing after review cannot authorize a new
correction. A root with more than 100 required pins cannot currently be corrected
through these endpoints; do not omit pins or alter the database to work around the
limit. The browser workspace below uses the same complete-scope read and API.

The server rechecks the session, CSRF, password-confirmation age, complete root,
reviews and economic replay in the same writer transaction. Financial facts,
exact old-FX reversals, trade and authorization audits, invalidations and the
serialized idempotency response commit together. An oversell fails without writes.
The response contains the authorization ID and resulting trade, and explicitly
requires `scope=UNVERIFIED_TRADE_CORRECTION_ONLY` and
`history_completeness=UNVERIFIED`. It has no ETag. Responses are private and
`no-store`; do not include passwords or provider keys in reasons or notes. The
trade's `created_sequence` is a JSON integer that can exceed JavaScript's safe
integer range. If retaining the original response for later audit-origin recovery,
preserve the exact raw JSON digits rather than parsing and reserializing it through
JavaScript `Number`.

An unchanged retry with the same key returns the original response without new
writes, even after a later withdrawal or valid subsequent correction. The server
reauthenticates and compares the cached response with the original audited intent,
pins and exact financial result. Reordering the same pins does not change intent;
a different input or physical version requires a new key. A cached response with
missing or inconsistent unverified labels fails closed. Such historical replay
is not a new grant. Physical/review version conflicts return 412; changed or
incomplete current scope returns 409; inconsistent stored evidence or cached
responses return 503; foreign trades return 404. Neither a successful correction
nor an audit proves ownership, income or current dividend eligibility.

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

## Unverified trade correction in the browser

On `/transactions`, expand **Unverified correction scope** for a current trade.
The browser fetches the entire supported root scope on each opening, shows each
physical trade and exact review revision, and does not offer either operation if
any gap is unreviewed, withdrawn or has changed evidence. Reloading or reopening
rechecks the server; stale cached scope cannot be selected while a refresh is in
flight. An empty scope or a matching hash is not proof that history is complete.
For an unreviewed gap, inspect the relevant physical trade in the revision chain
and use its separate history-review workflow first.

When every gap is currently acknowledged, choose **Unverified revise trade** or
**Unverified void trade**. The editor displays and freezes all observed trade and
review versions, asks separately for a recent password confirmation, a specific
reason and explicit acceptance of the unverified scope. A revision reuses the full
trade form and preserves decimal input strings; a void retains the original facts
and reverses existing settlement at the original FX. Ordinary PATCH/DELETE stay
fenced. The server independently rechecks every version and the economic replay;
a changed review, evidence or trade does not get silently adopted. More than 100
scope entries is reported as unsupported, never partially submitted.

During either the password or correction request the editor cannot be closed and
the other operation is disabled. If a response is uncertain, keep the editor open
and retry the unchanged submitted values: the original version, pins and idempotency
key remain in memory, including across password confirmation. The reason is trimmed;
changing submitted financial terms or reason creates a new intent. Closing the
editor discards its unsaved input and key, so first inspect the trade and audit
history rather than assuming that a lost response meant no write. The UI checks the
response's unverified labels, physical identities and financial string shapes
before reporting success. Passwords and form inputs are not stored in browser
storage or query/mutation caches. A successful operation does not establish
ownership, income or dividend eligibility.

## Pure accrual arithmetic with foreign fees

The internal `dividend_math.accrue` calculates one supplied event/account amount;
it does not determine eligibility, authenticate a source or record a receipt. The
legacy positional fee is already denominated in the event currency. Alternatively,
pass its original `fee_currency` and an explicit `fee_rate_event` expressed as event
currency per unit of fee currency. Same-currency rates must be identity; a nonzero
foreign fee requires its own rate. A known zero foreign fee needs no invented rate.
The separate `rate` is the event-currency-to-HKD valuation rate; unavailable USD
valuation leaves HKD null and is `MISSING_FX`, unless the raw net requires the
stricter `NEEDS_REVIEW` quality. Only HKD and USD are supported.

The calculation retains the unrounded fee conversion through net and HKD, then
rounds gross, tax, fee, net and HKD independently to six places and records the
component rounding difference. A negative unrounded net requires review even if it
rounds to zero. This pure result does not retain the original fee, FX provenance or
an effective tax setting; a future audited writer must retain and validate those
inputs separately. Its computational `OK` is not confirmation of withholding tax.

`dividend_projection_arithmetic.checked_accrual` independently recomputes a supplied
projection's components from its event terms, recorded shares, tax fraction, original
fee and both FX rates. Inconsistent amounts, invalid source labels, a hidden negative
net or a changed event identity fail closed; non-amount states remain non-amounts.
This checks a caller-supplied snapshot only: it does not verify any audit, fee or tax
setting, historical holdings, session or current event and is not an eligibility or
receipt grant.

## Required future adoption and projection work

These constraints are defense in depth, not authentication, authorization,
cryptographic validation or a complete application workflow. SQL checks stored
source pins and audit hash *shapes*, not their canonical content. The protected
candidate-adoption API rechecks the active session and CSRF, original source/candidate
audits, canonical hashes and current basis/action scope inside its short transaction.
A future economic writer must independently recheck these and all applicable
eligibility and financial provenance in its own transaction. It must commit financial
projections, audit, idempotency response and invalidations atomically; no provider HTTP
or private-file reads belong in that transaction. A row or an existing audit alone
does not grant economic use. The database checks presence at publication but cannot
prove that every prerequisite was created in the same transaction or that an
entitlement amount was calculated correctly.

The reference schema has `UNIQUE(revision_id, account_id)` for entitlements.
Managed entitlement snapshots are protected from in-place rewrites, so that
schema alone cannot retain multiple projection versions for the same event
revision and account. The separate history table can retain multiple supplied
versions without changing those entitlements, but a protected rebuild controller
and receipt linkage are still required before economic use. They must preserve
old receipt links, aggregate active receipts by stable event/account/currency across
revisions, and treat cross-currency or missing FX as review rather than inventing a
rate. For example, an expected 10 USD with 6 USD received, later accepted as 11
USD, leaves 6 received and 5 outstanding; the old and new expectations must not be
added together.

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
not unblock ordinary trade PATCH or DELETE. These are isolated synthetic
HTTP/SQLite tests, not proof of external ownership or eligibility.

`uv run pytest tests/test_trade_correction_scope_api.py` exercises complete
root ordering, absence and withdrawal, changed or conflicting later evidence,
100/101-entry boundaries, foreign owners and a concurrent withdrawal during a
WAL read snapshot. The endpoint never changes financial facts or grants a write.

`uv run pytest tests/test_trade_unverified_correction_api.py` exercises explicit
confirmation, exact root pins, protected revision and void, original-FX reversal,
reauthentication, writer-time security and review races, cached-response validation,
concurrent retries and full rollback. It uses isolated temporary synthetic data;
it does not establish a dividend eligibility grant or external ownership.

`npm run build && npm run test:e2e:trade-review` runs the actual application and
Chromium against an exclusive temporary database. It checks reasoned review and
withdrawal, recent password confirmation, a committed-but-lost response and its
unchanged-key zero-write replay, and withdrawal after retained-origin recovery
changes the evidence. Real HTTP also reads the complete root scope before and after
review and withdrawal, with no writes. Before the correction exercise, it checks
that reviews leave existing financial facts and historical FX unchanged. Real
Chromium then uses the correction forms to revise and void a new synthetic draft,
including keyboard reopening/refetch, responsive and dark/reduced-motion checks,
password confirmation, a committed-but-lost response and unchanged-key replay.
It checks that withdrawal blocks new use but not the original replay, and verifies
old audits and cash entries remain unchanged. It does not exercise a worker,
provider, deployment volume, container, NAS, TLS or external ownership; private
diagnostics follow the pinned browser policy.

`uv run pytest tests/test_eligibility_trade_origins.py tests/test_dividend_eligibility_service.py tests/test_eligibility_trade_review_scope.py`
checks origin-to-retirement audit matching, mixed legacy histories, inconsistent
retained claims, concurrent WAL read snapshots, withdrawn/changed creation-gap
reviews, untrusted review audits and retained roots moved across securities. These
are internal recorded share checks, not authenticated event, entitlement or receipt
endpoints.

`uv run pytest tests/test_dividend_projection_selection.py tests/test_dividend_math.py tests/test_finance_goldens.py`
checks pure current-version selection, exact rounding, independent event/account
identities, cross-revision partial receipts and rejection of missing or corrupt
projection chains. These cases do not exercise a persistent projection writer.

`uv run pytest tests/test_dividend_cross_fee.py` checks foreign-fee FX direction,
missing versus explicit zero, independent valuation FX, overflow and exact rounding
against an independent rational-number oracle. It does not verify the source or
record a financial fact.

`uv run pytest tests/test_dividend_projection_arithmetic.py` verifies supplied
projection components against the underlying unrounded arithmetic, preserving
missing FX and unresolved states while rejecting inconsistent snapshots. It does
not establish that the supplied event, shares or settings are authentic.

`uv run pytest tests/test_dividend_fee_migration.py` checks empty additive storage,
explicit zero versus unknown amount/currency, owned physical revision and account
scope, rejection and append-only fences, exact private backups and failed-upgrade rollback.
SQL-shaped synthetic assertions in these tests are not authenticated fee estimates.
`uv run pytest tests/test_dividend_fee_selection.py` checks supplied chain integrity,
effective-day/prefix selection, missing versus unknown and explicit zero, exact INT64
boundaries and private representations without asserting database authority.
`uv run pytest tests/test_dividend_fee_history.py` checks complete audited event,
rejection and fee history, snapshot isolation, explicit unknowns and zero,
account changes, malformed audits and retained rows. Its synthetic history is not
authentication or proof of an actual broker charge.
`uv run pytest tests/test_dividend_fee_writer.py tests/test_dividend_fee_writer_safety.py`
checks exact current pins even for missing fees, source/rejection changes, backdated
settings, audit and savepoint rollback, concurrent decisions, historical replay and
preservation of synthetic receipts and locked FX. It is not an authenticated HTTP test.
`uv run pytest tests/test_dividend_fee_api.py tests/test_dividend_fee_api_safety.py tests/test_dividend_fee_candidate_api.py`
checks private authenticated reads, exact-string pins, CSRF/reauthentication, source
and rejection fences, HTTP-intent binding, replay, rollback and synthetic retained
receipt/FX preservation. `uv run pytest tests/test_dividend_fee_race_api.py` checks a
concurrent protected rejection and fee write. None verifies a live broker charge or
economic eligibility.

`uv run pytest tests/test_dividend_tax_migration.py tests/test_dividend_tax_service.py`
checks immutable setting storage, audited history, explicit effective-day/head
selection, current source and account revalidation, zero versus unknown, historical
replay, competing decisions and transaction failure rollback. Synthetic old receipts,
locked FX and projection rows remain unchanged. These checks do not authenticate an
HTTP caller or establish withholding, ownership or income.

`uv run pytest tests/test_dividend_tax_api.py` exercises the installed owner-scoped
HTTP routes, session/CSRF/reauthentication, exact INT64 strings, current and historical
selection, competing writes, zero-write replay, corruption and atomic rollback. It
also checks that existing receipts and locked FX remain unchanged. These synthetic
tests do not confirm tax or establish eligibility.

`uv run pytest tests/test_dividend_rejection_service.py` checks complete rejection
history, private references, override provenance, stale physical pins, WAL snapshot
isolation and fail-closed corruption. Its internal fence is not session authentication,
a current-source check or an economic grant.

`uv run pytest tests/test_dividend_rejection_writer.py` checks exact intent/replay,
retained receipts, projections and FX, owner-scoped invalidation, override origin,
competing writes and savepoint failure rollback. These synthetic internal writes are
not authenticated decisions or permission to use the writer on deployment data.

`uv run pytest tests/test_dividend_rejection_migration.py` checks an isolated upgrade,
private backup, retained receipts and locked FX, rejection scope and audit shapes,
immutable/replacement guards, financial fences and competing revision writes. It
does not authenticate a rejection or install a protected decision workflow.

`uv run pytest tests/test_dividend_rejection_resolution.py tests/test_dividend_resolution_integrity.py tests/test_dividend_resolution_history_integrity.py tests/test_dividend_resolution_safety.py tests/test_dividend_resolutions_migration.py tests/test_dividend_rejection_resolution_api.py`
checks explicit pins, repeated rejection/resolution chains, historical zero-write
replays, late or corrupted audits, SQL and application fences, private migration backups,
retained financial facts and caught-failure rollback. The existing event endpoints refuse
an unpinned rejected revision while retaining the original event ETag. These tests do
not by themselves establish economic eligibility.
`uv run pytest tests/test_dividend_rejection_decision_api.py` checks protected rejection,
financial-history fences, unchanged event ETags, historical replay, authentication,
atomic rollback and retained receipts and locked FX.
`uv run pytest tests/test_dividend_rejection_read_api.py` checks owner-scoped current
state, resolved and unresolved history, private URLs, bounded pages, complete-history
failure, one-snapshot concurrent reads and unchanged financial facts.
`uv run pytest tests/test_dividend_resolution_pins_api.py tests/test_dividend_resolution_pins_financial_api.py tests/test_dividend_candidate_resolution_api.py tests/test_dividend_candidate_resolution_safety_api.py`
checks the protected event operations' explicit pins, compatibility with old request
fingerprints, historical replays, current-candidate checks, concurrent requests and
preservation of existing receipts and locked FX.

`uv run pytest tests/test_dividend_projection_tax.py tests/test_dividend_projection_tax_migration.py`
checks legacy fingerprint preservation, private pre-upgrade backup, atomic migration
rollback, distinct missing/unknown/zero estimates, exact selected/head/day pins,
backdated decisions and fail-closed retained history. It does not establish session
authentication, confirmed tax or a financial projection writer.
`uv run pytest tests/test_dividend_projection_fee.py tests/test_dividend_projection_fee_safety.py tests/test_dividend_projection_fee_migration.py`
checks additive migration and private backup, unchanged historical fingerprints,
explicit fee head/day/status, original amount/currency and audit chronology, later
backdated settings, foreign zero and fail-closed corruption. These synthetic tests do
not establish eligibility, actual charges, live FX or an economic writer.

`uv run pytest tests/test_dividend_projection_migration.py` exercises isolated
upgrade/rollback and private backup, immutable version chains, owner and current
revision/data-revision fences, competing inserts, integer extremes, nullable FX
and coexistence with old entitlement/receipt/cash records. It also checks that a
well-shaped but economically inconsistent row can be stored and cannot advance
an event. These tests do not authenticate the row or implement a projection writer.

`uv run pytest tests/test_dividend_projection_writer*.py tests/test_dividend_projection_source_time.py`
checks actual recorded ledger replay, exact head/day/account pins, unresolved and
unverified states, independent foreign-fee and valuation FX, audit chronology,
concurrent heads, rollback, historical replay and retained receipt/cash/FX. These
isolated synthetic tests do not establish session authentication, verified ownership,
actual broker charges, current-use authority, live FX or receipt reconciliation.

`uv run pytest tests/test_dividend_projection_current*.py` checks exact projection
heads and as-of days, current source/setting/account/ledger revalidation, later source
audits without a portfolio revision change, independently missing FX, stale and legacy
snapshots, same-transaction changes and a concurrent fee writer. It does not establish
session authorization, complete ownership, live market FX or a receipt grant.

`uv run pytest tests/test_dividend_projection_history.py` checks complete retained
chains, canonical fingerprints, private fixed errors, exact component arithmetic,
owner/account scope, later audit changes, historical account configuration and WAL
snapshot consistency. A synthetic matching audit can coexist with zero recorded
shares, demonstrating that this reader does not establish financial authority.

`uv run pytest tests/test_dividend_event_current_use.py` checks exact revision pins,
active-owner and writer boundaries, later candidate changes or dismissals, changed
basis, explicit manual decisions, same-transaction revalidation and competing
writers. These synthetic tests establish no ownership or economic authorization.

`uv run pytest tests/test_dividend_manual_override_api.py` exercises complete explicit
override terms, lock/reopen confirmations, audited origin and legacy replay labels,
late or missing markers, security races, financial publication fences and atomic
rollback. These synthetic decisions are not document authentication or income.

`uv run pytest tests/test_dividend_verification_api.py` exercises owner acceptance,
physical and source pins, session and reauthentication races, unchanged historical
replay, competing decisions, atomic rollback and the entitlement fence. These
synthetic checks do not authenticate a cited document or establish eligibility.

`uv run pytest tests/test_dividend_withdrawal_api.py` checks explicit withdrawal
confirmation, physical/version pins, authentication and reauthentication races,
retained terms and locks, historical replay, competing writes, atomic rollback and
the existing entitlement/projection fence. Synthetic receipts and locked FX remain
unchanged; this is not a cancellation/reconciliation or receipt workflow.

`uv run pytest tests/test_dividend_candidate_adoption_api.py` checks the installed
candidate-adoption routes, explicit distinct/match confirmations and physical pins,
owner/session/CSRF/reauthentication races, same-day identities, preserved unresolved
assessments, publication fences, zero-write replay and atomic rollback. It uses
isolated synthetic data and does not establish document verification, eligibility,
provider coverage or a receipt workflow.

`uv run pytest tests/test_dividend_event_write_api.py` checks protected manual
creation and revision, explicit lock/reopen confirmation, exact DPS, session and
reauthentication races, identical same-day event identities, full cached-intent
binding, publication fences and atomic failure rollback. It uses isolated synthetic
data and does not establish document verification, eligibility or a receipt workflow.

`uv run pytest tests/test_dividend_event_read_api.py tests/test_dividend_event_candidate_api.py`
checks the installed owner-only event list, current and historical reads, same-day
identity, keyset pages, private citation handling, late audit corruption, candidate
lineage and no provider or financial writes. It does not establish current eligibility
or document verification.

`uv run pytest tests/test_dividend_event_migration.py tests/test_dividend_event_service.py tests/test_dividend_event_writer.py`
checks migration preservation and rollback, source and candidate scope, publication
audits, manual locks, immutable history, whole-chain and review corruption
detection, exact scaled values, audited receipt reversal, competing writers,
projection invalidation and the pre-rebuild entitlement fence. These are synthetic
database and internal-library checks, not HTTP authorization, provider coverage,
verified documents, receipt projection calculation, live-provider or NAS
acceptance. Downgrade refuses to delete history; restore a validated pre-upgrade
backup instead.
