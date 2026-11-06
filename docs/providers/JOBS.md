# Source-job control and recovery

This contract describes installed source-only enqueue/read/cancel HTTP APIs and
server-side job control. The [worker](WORKER.md) dispatches explicitly queued
source jobs through shared upstream reservation/circuit policy, with an independent
heartbeat. The [source workspace](WORKSPACE.md) provides owner progress and
cancellation UI; scheduled job creation and canonical reconciliation remain
unfinished. Startup creates no job or provider enablement, and paid/live
coverage remains NOT_TESTED.

## Authenticated HTTP intents and safe diagnostics

All paths below have the `/api/v1` prefix and require an owner session. Mutations
also require the exact public Origin, `X-CSRF-Token` and `Idempotency-Key` described
in the [authentication guide](../../readme.md#owner-authentication). Responses are
`Cache-Control: no-store`; no provider HTTP runs in a request handler.

`POST /dividends/sync` requires explicit `security_ids` (1–100), `provider_ids`
(1–8), `from_date` and `to_date`. Dates form an inclusive valid calendar window;
there is no guessed history, security selection or paid-provider default. Optional
`operations` defaults to `["DIVIDENDS"]`; select `SPLITS` explicitly if wanted.
`force` defaults to false and is a strict JSON boolean, not a quota bypass. Input
array bounds apply before duplicate removal; sets are canonicalized for intent
comparison. Arbitrary fields, credentials, file paths and client profile hashes
are rejected.

A synthetic request, using an existing explicitly mapped security, is:

```json
{
  "security_ids": ["00000000-0000-4000-8000-000000000003"],
  "provider_ids": ["eodhd"],
  "from_date": "2026-01-01",
  "to_date": "2026-01-31",
  "operations": ["DIVIDENDS"],
  "force": false
}
```

The response is **202 Accepted**, with `job_id`, `status=QUEUED`,
`scope=SOURCE_ONLY`, `canonical_reconciliation=NOT_RUN` and a relative
`Location: /api/v1/jobs/{job_id}`. This accepts source intent, not provider
availability or financial synchronization. Unconfigured inputs can therefore
queue successfully and later become explicit skipped/failed work without HTTP.
A missing catalogue security rejects the entire request with 422.

Private profile files are read before the writer UoW. The transaction rechecks
session/CSRF and atomically commits job, items, audit and idempotent response; the
creation/cancellation audit links to the server-minted HTTP request ID. Only the
normalized public body participates in HTTP idempotency. An unchanged replay
returns its original accepted response without new work, even after private-file
rotation or worker completion. A changed body with the same key conflicts; use a
new key for a deliberate rerun. The key is not required to be a UUID.

- `GET /jobs` lists only the current owner's supported managed source jobs, newest
  `created_at,id` first. It supports bounded keyset pagination, exact `status` and
  literal `q` search over kind/ID. Retained legacy/unregistered jobs are not exposed
  or automatically adopted.
- `GET /jobs/{id}` returns the current typed control state, ETag and aggregates of
  committed facts. Internal request/result JSON, worker ownership, profile hashes,
  credential paths and upstream cursors are not returned. A stored opaque result
  is neither trusted for serialization nor rewritten by the read.
- `GET /jobs/{id}/items` lists safe per-security/provider/operation diagnostics in
  ascending `ordinal,id` order. It supports bounded pagination, `status`,
  `provider_id`, `security_id` and literal ticker/provider/operation search `q`.
  Follow returned `next_cursor`; do not reuse another resource's cursor.
- `POST /jobs/{id}/cancel` accepts no body or `{}` and returns **200** with the
  current job and ETag. It does not require If-Match: this is an owner stop intent,
  not replacement of a financial fact. A queued job can become CANCELLED; live
  work becomes CANCEL_REQUESTED until worker acknowledgement/expiry. The response
  does not claim an in-flight request has stopped. Already committed evidence and
  spent reservations remain. An unchanged idempotent retry returns its original
  response; reread the job for the later state.

A foreign or unsupported job ID returns 404 for reads, item lists and cancellation.
`provider_attempts` explicitly uses `RESERVED_OR_RECORDED_HTTP`, including uncertain
spending, not verified billing. `observations_new` counts newly stored assertions;
`pages_committed` counts checkpoints. Neither is verified income or market
coverage. Cancellation never fabricates scanned progress or adopts/reverses cash.

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
Inactive owners are not dispatched. HTTP authentication/CSRF/idempotency wraps
these internal functions; the controller is never exposed as an unprotected route.

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
HTTP call count or lease generation. Upstream request spending is counted
separately by the quota/attempt layer, with an explicit reserved/recorded basis;
these values must not masquerade as verified live usage or provider billing.

Waiting for a quota/circuit deadline does not increment the work-attempt count.
A mid-page wait preserves the open logical round across parking and a new lease,
including in the fourth attempt. Waits are absolute aware UTC times, persisted
without a shorter arbitrary clamp. A waiting job releases its lease and is not
claimable before its earliest work is due. The upstream reservation/outcome
library computes and enforces allowances, Retry-After, backoff and jitter; the
dispatcher must use that library rather than calling an adapter directly.
Repeated crashes while an item is RUNNING conservatively consume its started
attempt. Exhaustion with a committed prefix is PARTIAL; without one it is FAILED.
A proven retained tail can finish on recovery without another request. Independent
items remain dispatchable.

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
Reservation preflight checks input cursor cycles and the 256-page ceiling before
spending quota. The worker follows continuations through the reservation/outcome
library, using the same retained context and a new grant for each page.

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
