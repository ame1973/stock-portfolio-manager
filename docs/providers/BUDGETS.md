# Upstream budgets and attempt safety

This is the installed server-side reservation/outcome contract, not a live
provider dashboard. The [worker dispatcher](WORKER.md) uses it for source jobs
queued through the [authenticated HTTP API](JOBS.md); automatic schedules remain
unfinished. All paid/live plan and target-NAS validation remains NOT_TESTED. Software limits
are not a provider invoice, remaining-account quota probe, data-rights grant or
proof of coverage.

## Explicit policy, shared upstream

Network profiles require explicit positive minute/day budgets and rights
acknowledgment in [private operator files](CONFIGURATION.md). There is no implicit
allowance. Manual/HKEX/SEC evidence does not have a network budget. Budget loading
reads local files only and does not load an SDK or probe upstream availability.

Budgets are keyed by the actual installation-wide upstream origin, not owner,
job, adapter wrapper, key version or configuration hash. Yahoo/yfinance therefore
share one YAHOO allowance. Their effective minute/day limits are the minima of
currently enabled, ready file profiles. An invalid/disabled profile cannot grant
traffic; an optional SDK's file policy can conservatively restrict its shared
upstream even before the SDK is installed. Stored policy contains exact limits,
non-secret member profile hashes and member limits only, never credentials/paths.

`BudgetSnapshot.unchanged` rechecks ready profiles through the credential-safe
store and also detects unavailable peers becoming ready. The dispatcher must
check all observed peers and private configuration before and after HTTP; a
primary-profile timestamp comparison alone is insufficient. Queued requests
remain pinned to their non-secret profile identity. A key's presence is not
permission to activate an unavailable queued selection.

## Exact software allowances

Minute spending uses an integer token bucket. One request costs 60,000,000 units;
a capacity of R requests/minute holds R times that amount, refilling R units per
microsecond. No binary floating point is used. A fresh explicit policy starts
with at most its capacity; normal bursts and refill remain bounded by that
policy. The minimum wait is rounded upward to a whole microsecond, never earlier.

The daily allowance is a conservative rolling 24-hour window, not a guessed
provider billing/reset timezone. Every reservation counts, including failed,
cancelled and crash-uncertain requests. The right boundary remains conservative
by one microsecond. If a lower limit is already exceeded, the library waits for
enough old reservations to leave the window, not merely the first one. Midnight,
new jobs, wrapper changes and key rotation do not reset the ledger.

A changed minute policy first refills elapsed time at the previously stored rate,
then clamps the retained balance to the new capacity. Increasing a limit never
mints fresh tokens. Daily history survives policy changes. Provider Retry-After
and circuit holds apply in addition to both allowances; the latest required
deadline wins. Clock regression waits for durable high water instead of minting
credits. Corrupt state/invalid bounds fail closed without enabling network access.

## Reserve once, then perform HTTP outside the transaction

`reserve_attempt` runs in a caller-owned short write UoW with a live fenced job
lease. It validates the selected profile, operation, alias/window/page prefix and
safe input cursor before spending. It retains the immutable non-secret fetch
context, records one immutable `upstream_attempts` row, debits the bucket and
appends audit atomically. A budget/circuit wait records no new reservation and
spends no logical work retry. The caller releases/parks waiting work rather than
holding a database transaction or busy-looping.

Only `ReservedAttempt.created=true` permits **one** bounded transport call. The
same attempt ID with the same sealed request returns `created=false` with zero
writes; it is not permission to issue the HTTP call again. Different input under
that ID conflicts. One unresolved attempt per work item is enforced in SQLite
and the service. The dispatcher must renew a short lease before I/O and preserve
the original token, prepared request and timings across its response callback.

No external HTTP or private configuration-file reads occur inside this write
transaction. Perform the actual adapter
call afterwards, within its verified timeout/body/host limits. Recheck private
configuration and every relevant budget peer after I/O. If intent/configuration
changed, discard the response for reconciliation; do not adopt it under a new
profile or silently try an unapproved source.

There is no distributed exactly-once guarantee for an external provider. A crash
between reservation and result cannot prove whether the provider charged a call.
The conservative rule is **never refund or reissue that reservation**. A fenced
successor records ABANDONED/CANCELLED after lease loss/cancellation and, only when
needed and allowed, creates a new attempt ID with another budget debit. The old
worker cannot commit a late response into the new epoch.

## Commit one durable outcome

`complete_attempt` binds the result to its reservation, original context, input
cursor and lease/work epoch. HTTP start cannot precede reservation; finish cannot
be in the future relative to commit. Source run/observations, a valid page
checkpoint, immutable `upstream_outcomes`, circuit changes, work progress and audit
share one transaction. A late constraint/audit failure rolls back those new
changes, while the earlier committed reservation stays spent.

A successful parsed tail completes the item/job in that transaction, eliminating
a crash gap between its final checkpoint and control success. A valid prefix
survives retries. Exhausting retries after a committed prefix is PARTIAL, not a
claim that the entire requested window failed or completed. Malformed/partial
pages preserve valid source assertions without a false complete checkpoint.
NO_DATA is processing evidence, never confirmed zero dividends.

Historical completed replay checks the original capture fingerprint and returns
its receipt without another result, quota debit, checkpoint, progress or audit.
Closed abandoned/cancelled attempts cannot accept a later source result. Outcomes
and reservations, including no-op updates, are append-only. No attempt outcome
rewrites canonical dividends, owner receipts, trades, cash or portfolio revisions.

Job result `provider_attempts` explicitly uses
`provider_attempts_scope=RESERVED_OR_RECORDED_HTTP`: reservations plus legacy
recorded HTTP without a reservation. `provider_http_recorded` is the recorded
HTTP subset; `provider_attempts_unresolved` keeps potentially spent requests whose
HTTP result is unknown. These are conservative software evidence, not verified
provider billing or a fabricated live quota counter.

## Retry and circuit behavior

Retryable rate/transient outcomes get an exponential floor of 1/2/4/8 seconds by
logical work attempt, plus integer jitter from zero through that floor. A valid
absolute upstream Retry-After can only extend it, including year-9999 parking;
it is never truncated to an earlier arbitrary cap. Work remains independently
bounded to four total logical attempts including the first. A single symbol's
transient backoff does not block unrelated symbols before the circuit threshold,
unless the provider itself supplied a shared Retry-After.

Five consecutive transient failures open a shared upstream circuit for 60 seconds.
Authentication failure also opens a cooldown without pretending that a key is
valid. After cooldown, exactly one half-open probe can reserve a request; other
work waits. Only that probe's healthy parsed result closes the circuit. A failed
or crash-uncertain probe reopens cooldown without a refund. Delayed ordinary
success does not prematurely close an already-open circuit. These are software
policy constants, not undocumented provider guarantees or a hidden fallback.

A quota/circuit pause between pages preserves `round_open` across parking and a
new lease, including in the fourth logical attempt. Actual failure/retry, crash
recovery, completion and cancellation close the round as appropriate. Full-page
I/O does not consume another logical round. Input cursor cycles and the 256-page
work-item ceiling are checked before quota spending; a longer loop returned by
a later page preserves that page's valid evidence and marks partial processing.

## Upgrade and recovery

Migration `0010_upstream_budgets` preserves all original source/job/financial
columns. The sole derived new control flag is open for an already RUNNING item;
corrupt retained running work is refused, not guessed. The migration creates no
quota allowance, attempt, outcome or provider success for retained jobs. New
shared state appears only on a real explicitly configured reservation path.

SQLite guards retain origin/member limits, owner/context/work/fence identity,
exact integer/time storage, one unresolved item attempt and immutable outcomes.
Late DDL failure restores schema/head/audit/all facts. Upgrade audit is append-only
per existing owner and repeating the migration is a no-op. Restore a validated
pre-upgrade backup instead of deleting quota/attempt history to downgrade or reset
limits. The [worker dispatcher](WORKER.md) uses this contract; automatic schedules,
owner-facing API/UI and licensed live validation remain separate acceptance work.
See [job control](JOBS.md) and [source evidence](EVIDENCE.md).
