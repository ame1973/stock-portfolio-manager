# Fenced source-worker execution

The installed worker consumes explicitly queued, managed `SOURCE_ONLY` jobs. It
uses the actual provider adapters, [private profiles](CONFIGURATION.md),
[upstream reservations](BUDGETS.md) and [source checkpoints](JOBS.md). It is not a
canonical-dividend reconciler, announcement scraper, provider billing probe or
proof of licensed live coverage. Paid/live providers and the target NAS remain
NOT_TESTED.

Owner-authenticated source enqueue/read/cancel APIs are available; see the
[HTTP contract](JOBS.md#authenticated-http-intents-and-safe-diagnostics).
The [source workspace](WORKSPACE.md) provides explicit queueing, progress and
cancellation. [Explicit schedules](SCHEDULES.md) also create source-only jobs through
the same control/dispatch path; scheduling UI, cited evidence creation/review APIs
and key-import settings are still unfinished. Provider configuration/recorded-source
reads and [exact observation lists](OBSERVATIONS.md) are available separately,
never as a live probe. No default schedule or provider enablement is created at
startup; only explicitly enabled, due definitions can create scheduled work.
Empty/unconfigured installations perform no upstream requests. Retained
unmanaged jobs are not automatically adopted.

## Single leader, independent heartbeat

The separate worker process holds the existing local-filesystem OS leader lock.
SQLite claims additionally fence each owned job by generation and expiry, so a
second claimant cannot execute the same job. The daemon polls for eligible work;
future durable deadlines do not become loops that sleep while holding a writer.

A separate asynchronous task checks database readiness and updates the heartbeat
while provider I/O is awaited. Automatic schedule evaluation is another independent
task; its private-file preparation runs outside the event loop and all DB writers.
A readiness-task failure stops the worker instead
of leaving a falsely healthy dispatcher. Unexpected dispatch failures log a fixed
`SOURCE_DISPATCH_FAILED` code without exception text, credentials, paths, provider
bodies or full upstream queries. Per-item errors remain bounded control codes.

The single leader processes ready items/pages serially, not concurrent writers.
One failed, unsupported or waiting security/provider/operation does not prevent
another ready item from progressing. When all remaining items wait, the job
parks at its earliest absolute deadline and releases its lease.

## One checked grant per page

For each ready page the dispatcher:

1. Checks the sealed provider selection and supported operation/market. Missing,
   disabled, unsafe or unacknowledged private configuration cannot grant traffic.
   A null queued selection cannot activate because files appear later. A changed
   pinned profile stops the item rather than silently adopting new intent.
2. Resolves the entire requested alias window from the security/exchange
   catalogue. Gaps, overlaps and identity collisions reject before I/O. A retained
   continuation must still match its original alias/version/window/context.
3. Checks local adapter readiness, including the optional SDK, without a paid
   probe. Manual/HKEX/SEC jobs without explicit cited input are unsupported; the
   worker does not fetch their links or invent an empty announcement result.
4. Rechecks private primary/peer snapshots and reserves exactly one request in a
   short writer UoW. A lease with 30 seconds or less remaining is renewed before
   the bounded call. No HTTP or private configuration-file reads are performed
   inside that transaction. An existing grant never authorizes reissue.
5. Releases the database transaction, rechecks configuration and performs one
   actual bounded adapter call. The client closes before response persistence.
   Private profiles, credentials and all relevant budget peers are checked again.
6. Commits source assertions/checkpoint/outcome/circuit/work progress/audit
   together under the original lease/context. Changed configuration/aliases,
   cancellation, expired ownership or late failures cannot adopt a new response
   under different intent. Reserved spending is never refunded.

A completed source-only page/job is not verified income, a receipt, share/cash
adoption, authoritative coverage or an independent vendor vote. NO_DATA remains
processing evidence rather than confirmed zero dividends. Existing financial
facts, historical FX, owner receipts and portfolio revisions are untouched.

## Full pagination and recovery

Every safe continuation is followed through another reservation; a successful
first page is not the whole job. Pages retain the same immutable fetch context
and input cursor. Contiguous dated alias segments use their own exact windows
and contexts. The dispatcher never guesses a symbol from the display ticker.

Input cursor cycles and the 256-page item ceiling reject before another budget
debit. A longer loop returned by a later page retains that page's valid evidence
and finishes partial processing. Errors after a committed prefix retain it;
retry exhaustion or later configuration/identity failure is PARTIAL, not an empty
whole-window failure. Independent work can still finish.

Quota/circuit waits preserve an open logical work round across parking and a new
lease, including round four. Actual failure/retry/crash closes that round; four
logical rounds are not four HTTP calls or four ownership generations. Checkpoints
survive process restarts, so the worker resumes the next page/alias window instead
of fetching a committed prefix again.

Before claiming work, the worker closes stale/cancelled unknown reservations even
if no new provider job is ready. This appends outcomes without refunds or invented
HTTP responses. A valid retained legacy final checkpoint can finish after lease
recovery without another request; an exhausted retained prefix is PARTIAL.

## Shutdown boundary

SIGTERM/SIGINT stop new work and allow the current bounded operation to return.
A completed response is committed only if its original lease and configuration
remain valid. Cancellation or task loss cannot create a financial fact. Compose
allows a 40-second worker grace period; forcible termination is still safe through
conservative reservation spending and fenced lease recovery.

An unfinished job is not owner-cancelled merely because its process stops. Its
remaining lease must expire before another worker can recover it. Completed
pages remain retained; an interrupted started logical round is conservatively
consumed. No shutdown path deletes source, quota, job or audit history. Schedule
evaluation also checks stop intent around preparation and before materialization;
its committed receipts prevent duplicate queueing after restart. Operational
backup/restore quiescing remains separate work.
