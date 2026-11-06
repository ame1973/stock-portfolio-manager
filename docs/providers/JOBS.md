# Source-job control and recovery

This contract describes the installed server-side job-control library, not an
available sync API or scheduled provider service. The worker still supervises
leadership/heartbeat only. Quota reservations, circuit/backoff policy, dispatcher,
manual-sync/job HTTP APIs and monitoring UI are not yet integrated. No default
job/provider traffic is enabled, and paid/live coverage remains NOT_TESTED.

## Sealed source-only work

A managed `DIVIDEND_SOURCE_SYNC` job has `runtime_version=1` and an immutable
`SourceJobPlan`. Its schema version and `SOURCE_ONLY` scope are explicit. It
contains up to 100 distinct security IDs, up to eight distinct provider selections,
an inclusive calendar-date window, dividends/splits operations and an explicit
force flag. Sets are canonicalized before fingerprinting. Arbitrary JSON fields,
credentials, paths and financial inputs are not accepted.

Provider selections contain only a server-derived non-secret profile hash or
null for unavailable configuration. A null selection cannot become an enabled
capture because an operator adds a key later; a new explicit intent is needed.
A successful page must match the selected profile hash. This does not replace
private file/secret rechecks before and after HTTP, nor grant data rights.

The dedupe namespace includes owner, job kind and intent ID. Reusing that intent
with the same sealed plan returns the existing job without writes/audit; changing
it conflicts. A new intent permits a deliberate rerun. Security metadata remains
an installation-wide catalogue, while jobs and their audit are owner-scoped.
Inactive owners are not dispatched. HTTP authentication/CSRF/idempotency remains
the future API boundary; these internal functions are not unauthenticated routes.

## Work, progress and retry semantics

Each security/provider/operation has one durable work item. A symbol/provider
failure does not consume another item's retry budget or stop its progress. A
single job has at most one RUNNING item. Pending, running and waiting work remain
separate from successful, partial, failed, skipped and cancelled outcomes.

`progress_total` counts requested distinct securities. `progress_done` counts a
security only after all its work items reach a non-cancelled terminal outcome;
it does not mean every provider succeeded or dividends were verified. Cancelling
unprocessed work does not fabricate 100% scanned progress. Results identify
`SOURCE_ONLY`, actual new observations, committed pages, failed work and skipped
unsupported work. Canonical dividend/receipt/valuation results are not invented.

The existing `max_attempts=4` is interpreted conservatively as **at most four
logical work attempts including the initial attempt**, independently per item.
A normal multi-page fetch remains one logical attempt; completed pages survive
retry/restart. The job's `attempt_count` is the highest item attempt reached, not
HTTP call count or lease generation. Upstream request spending will be counted
separately by the quota/attempt layer; these values must not masquerade as live
usage diagnostics.

Waiting for a quota/circuit deadline does not increment the work-attempt count.
Waits are absolute aware UTC times, persisted without a shorter arbitrary clamp.
A waiting job releases its lease and is not claimable before its earliest work
is due. Retry policy must separately honor Retry-After/backoff/jitter; the control
library stores deadlines but does not yet compute or enforce upstream quotas.
Repeated crashes while an item is RUNNING conservatively consume its started
attempt; the fourth exhausted attempt fails that item without preventing another
item from starting.

## Lease ownership and cancellation

Claims occur in a short BEGIN IMMEDIATE unit of work. A default lease lasts 90
seconds; explicit durations must be strict integers from 30 to 300 seconds. Each
claim, including recovery by the same worker ID, increments `lease_generation`.
A frozen token binds job, owner, worker and generation. Every new worker mutation
checks the current token, state and non-expired UTC deadline, rather than trusting
worker ID alone. Renewals never shorten a live lease or revive an expired token.
Clock-regressed mutations are rejected; future-dated queue state is not reclaimed
early. The dispatcher must supply a fresh server clock, never a client timestamp.

A queued/unleased job can be cancelled immediately. A live leased/running job
becomes CANCEL_REQUESTED; a new page commit is then forbidden. Its worker can
acknowledge cancellation, or expiry recovery finishes cancellation after a crash.
Committed source runs/observations/checkpoints/audit remain intact. Terminal jobs
and work items cannot be reopened or deleted; a deliberate rerun creates a new
intent/job. Owner cancellation does not reverse financial facts.

## Atomic source checkpoints

External HTTP and private file reads happen outside the writer transaction.
`record_page` fences a new result, calls the actual evidence writer, appends its
checkpoint and audit, and updates job results in one caller-owned short UoW.
Lease loss, cancellation, changed configuration, alias CAS, cursor/window gaps,
constraint errors and late audit failures roll the complete change back.

Only fully parsed OK_WITH_DATA/NO_DATA pages advance a checkpoint. Partial pages
retain valid evidence without a false complete checkpoint. Successful work needs
a proven processed prefix through the requested end with no pending continuation;
no data/partial prefix cannot silently become whole-window success. A partial
outcome needs retained source evidence or a committed prefix. Processing NO_DATA
still does not confirm zero dividends or authoritative market coverage.

Checkpoints retain ordered immutable run links and the safe input cursor. They
must follow contiguous alias windows, or the preceding continuation with the same
alias/config/parser context. Each run links once. Requested cursors are also
retained on runs and included in non-null attempt fingerprints, so a changed
input cursor cannot replay as the same attempt. Null-cursor fingerprints remain
compatible with earlier captures. The safe fixed-provider/query-credential guards
apply to both input and output cursors; queries never override trusted credentials.
The dispatcher still needs bounded full-pagination and pre-I/O cycle policy.

An identical already-committed page replay returns its original capture receipt
with zero new rows, checkpoints, progress or audit, even after later cancellation
or alias changes. Its original new-observation count must not be added again.
Job results derive new observations and pages from committed records, not replayed
return values. See [evidence integrity](EVIDENCE.md) for financial and licensing
boundaries; source-only success is never canonical dividend adoption.

## Upgrading old queues

Migration `0009_source_jobs` retains every original jobs/source/financial column
and adds control versions, generation, request fingerprint and update time, plus
`job_items` and append-only `job_checkpoints`. Existing jobs keep
`runtime_version=0`; no request shape, attempt, owner or recovery state is guessed.
They are not automatically dispatched or adopted by the managed controller.
Existing runs gain only a nullable requested-cursor field.

Malformed managed plans, unsafe control transitions, premature success, stale
checkpoint generations and history deletion are rejected by SQLite guards as
well as the service. A late migration failure restores the original schema/head,
queue, audit and all facts. Success appends one schema audit per existing owner;
repeating the upgrade adds no duplicate audit. Downgrade remains restoration of a
validated pre-upgrade backup, not deletion of retained execution history.
