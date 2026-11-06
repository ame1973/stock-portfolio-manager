# Explicit source schedule timing

**Current implementation:** validated timing, durable storage, the internal
controller and protected owner HTTP CRUD/receipt reads are installed. Automatic
leader-worker materialization and scheduling UI are **not yet installed**.
`automatic_materialization_installed=false` explicitly distinguishes persisted
owner enablement from an operational automatic scheduler. Importing, migrating,
creating a disabled definition or reading due candidates does not queue work,
enable providers, send HTTP or mutate financial facts. Existing explicit source
jobs remain available; schedule receipts describe queue/skip, never income.

## Explicit policy, no inferred scope

`SourceSchedulePolicy` requires stable securities (1–100), selected network
providers, market HK/US, an earliest local `first_date`, a local `HH:MM` run time,
a cadence and exactly one positive recurring window. Dividends are the sole
default operation; provider-only splits require explicit selection. Raw array
limits apply before set canonicalization, as with manual source intents.

- Run time is an explicit **16:00–23:59** market-local minute in the nominal
  regular-close/evening window, never a guessed NAS/UTC hour or a verified session
  end. Choose a time after the relevant closing auction; this core does not infer
  holidays, early closes or auction completion. HK uses `Asia/Hong_Kong`, US uses
  `America/New_York`; the machine's timezone is not the market timezone.
- `DAILY` includes every local calendar day, including weekends, for recent
  correction sweeps. `WEEKDAYS` includes Monday–Friday only. `WEEKLY` requires an
  explicit weekday (Monday 0 through Sunday 6).
- `lookback_days` is 1–3660 and counts both endpoints; 30 means the occurrence
  date minus 29 calendar days through that date.
- `lookback_months` is 1–120 and uses calendar-month subtraction, including the
  clipped anniversary through the occurrence date. For example, one month before
  2025-03-31 is 2025-02-28, not a guessed 30-day period. Month-end clipping is an
  explicit derived-calendar rule, not rounding a financial input.
- An optional `backfill_from_date` must be supplied explicitly and cannot follow
  `first_date`. Only an explicit initial-window evaluation uses it. Ordinary
  evaluations always retain the recurring window. There is no guessed full-history
  origin, fixed 1970 date or automatic initial request.
- Keys, credentials, caller timezone/paths, force flags, financial inputs and
  manual/official cited-evidence profiles are not accepted. Automated evaluation
  cannot invent cited human input. Strict integer/boolean/date boundaries reject
  coercion; policy values are frozen and their JSON identity is canonical.

An explicit policy, used as the `policy` member of an HTTP create/update body, is:

```json
{
  "security_ids": ["00000000-0000-4000-8000-000000000003"],
  "provider_ids": ["eodhd"],
  "market": "HK",
  "first_date": "2026-01-01",
  "local_run_time": "17:15",
  "cadence": "DAILY",
  "lookback_days": 30,
  "operations": ["DIVIDENDS"]
}
```

An independent weekly 24-calendar-month policy can use `cadence=WEEKLY`, an
explicit `weekday`, `lookback_months=24` and no day-window value. Creating a Python
policy does not activate either strategy or grant provider rights/quota.

## Calendar evidence and UTC boundaries

Every policy declares **SOURCE_ONLY** and
**LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR**. No verified exchange holiday/session
calendar is installed. Monday–Friday rules must not be presented as confirmed
trading days; a source run on an unverified holiday is not a market-open, coverage,
NO_DATA or zero-income assertion. Trusted exchange-session calendar integration
remains a separate unresolved gate, not a silent weekday approximation.

`next_due` reconstructs the chosen local wall time on each selected date, then
returns aware UTC. The default is strictly after the reference; equality is
included only with an explicit real boolean. `latest_due` finds the latest elapsed
slot in constant work rather than iterating a potentially years-long backlog.
US DST changes the UTC hour, not the owner's chosen local hour. A weekly interval
across DST can be 167 or 169 hours; adding fixed UTC days is incorrect.

`occurrence_window` accepts only an aligned, not-before-policy occurrence and uses
its **market-local date** as the inclusive end. A US 23:45 run can have a next-day
UTC timestamp without moving the source event-date window forward. Month/day
underflow is rejected, never clipped to an arbitrary history origin. Unrepresentable
clocks fail explicitly; calendar exhaustion returns no next slot, not unavailable
provider data or zero income.

## Durable storage boundary, not an activation API

`source_schedules` stores owner/name, the non-secret policy and its SHA-256,
explicit enablement/version, UTC next-run/progress metadata and retained job
references. New definitions must begin disabled at version 1 with no progress or
next run. The migration leaves both new tables empty, preserves all existing
facts/settings/jobs/spending and appends one schema audit per existing owner.
Old settings JSON and unmanaged queue rows are never adopted as schedules.

`source_schedule_occurrences` retains the frozen policy/window, observed schedule
version/expected UTC slot, actual due/materialization times, sequence, next slot
and either a newly queued managed source job or an in-flight blocking job.
Schedule/owner foreign keys and unique slot/sequence/version/job identities prevent
cross-owner references, duplicate occurrences and sharing one job between schedules.
Queued job scope, operations, exact window, explicit `force=false` and the complete
committed job-item set must agree. These are source jobs, not canonical adoption.

An occurrence insert advances the matching enabled definition/version atomically
through an SQLite trigger; direct forged progress is rejected. Occurrences cannot
be updated/deleted and schedule identity cannot be deleted. The internal controller
enqueues the job, inserts the occurrence and appends its audit in the **same short
writer transaction**. A late failure rolls all three and parent progress back.
SQLite validates policy shape, catalogue market/timezone, civil dates and window
arithmetic; the controller separately verifies canonical JSON/SHA-256 and exact
`zoneinfo` slot/next-slot calculations. There is no SQL approximation of DST or a
trusted exchange-session calendar, and no supported operator SQL activation
workflow.

Only one nonterminal job per schedule is permitted. QUEUED, LEASED, RUNNING,
RETRYING and CANCEL_REQUESTED all exert backpressure; a durable
`SKIPPED_IN_FLIGHT` receipt advances the evaluated slot without another job,
work round or upstream reservation. A skip is not NO_DATA, coverage or zero income.
The first explicit backfill marker is consumed only when its job is **queued**,
not when a slot is skipped or a provider succeeds. Failed/cancelled/uncertain
initial work is not silently reissued as another full-history backfill. After
that first initial enqueue, its backfill date cannot be rewritten; a deliberate
manual source request or a new disabled definition is required for another one.
Pausing/editing metadata changes future scheduling, not already queued work;
existing job cancellation remains a separate stop intent.

## Internal controller and HTTP boundary

`schedule_service` provides disabled creation, reasoned full-body CAS revision,
owned reads, bounded due owner/ID candidates, frozen due evaluation and atomic
materialization. Creation does not accept an enabled flag. Revision requires a
strict explicit boolean/reason and the observed version; a worker advance also
makes an older editor stale. Catalogue markets/timezones, not quote currency,
determine scope. Explicit edits/re-enablement recompute the next slot from the
server clock, without replaying earlier missed slots. Equality may schedule a
not-yet-materialized current instant, but never reuse the last materialized slot.
Pause retains existing work/history; no HTTP/secret-file access occurs in writers.

A due evaluation coalesces elapsed slots to **one latest** market-local occurrence
instead of emitting a potentially years-long backlog. Bounded scans return IDs so
a future worker can isolate corrupt definitions. Materialization rechecks owner,
enablement, version, policy, expected slot, latest elapsed slot and the first-job
marker. If another slot elapsed while private selections were prepared, reevaluate
rather than enqueue stale work. Each observed version has a deterministic internal
intent and one immutable receipt. Concurrent writers/replays return the original
receipt with zero additional writes, including after pause, job cancellation or
configuration rotation; changed frozen intent conflicts instead of relabelling it.
The receipt describes enqueue/skip, not completed provider work or financial income.

Server-derived provider selections must exactly match the policy for a new job;
missing configuration can remain explicitly null, never become enabled on replay.
Replay and in-flight skips need no new selections. The caller owns the short
writer UoW and must let failures unwind it; validation/audit/response errors must
not be swallowed and committed as a partial write. Aware UTC clocks, finite
calendar/window bounds, strict internal datetime types and fixed safe corruption
errors prevent timestamp guessing, silent rehash/repair or private exception dumps.

## Protected owner API

All paths are under `/api/v1`, authenticated and **no-store**. These handlers do
not read private provider files, probe providers, reserve quota or reconcile facts.

| Method and path | Meaning |
| --- | --- |
| `GET /source-schedules` | Owned definitions, bounded `q`/`enabled`/`limit`/`cursor`, SQLite NOCASE name then physical ID ascending |
| `POST /source-schedules` | Create an explicitly named disabled policy, 201 + Location + version ETag |
| `GET /source-schedules/{id}` | Current owned metadata/committed progress + version ETag |
| `PATCH /source-schedules/{id}` | Full name/policy/strict enabled/reason body + quoted If-Match, 200 + new ETag |
| `GET /source-schedules/{id}/occurrences` | Bounded immutable receipts, due UTC/ID descending, literal `q`/`limit`/`cursor` |

Create accepts `{ "name": "Owner-selected source schedule", "policy": { ... } }`,
where the complete policy is as documented above. It never accepts enabled, owner,
credentials, force, caller-clock or server-progress fields. Update requires the
complete name/policy plus an explicit real boolean `enabled` and a 10–1000-character
reason. Both mutations require session/CSRF and Idempotency-Key, rechecked inside
the short writer UoW. PATCH also binds its observed version into the idempotency
fingerprint; a different If-Match under the same key conflicts instead of silently
replaying a different command. A stale editor returns 412. Current required-header
validation uses 422; invalid CSRF uses 403. Audit, metadata and serialized cached
response commit together, including late validation/serialization rollback.

Responses contain explicit SOURCE_ONLY/NOT_RUN scope, typed policy/timezone,
enablement, UTC next/created/updated/last occurrence times, actual receipt count,
first-backfill-queued flag and owned last-job reference. They deliberately report
`automatic_materialization_installed=false`: enabled means a persisted owner
intent, **not** an installed/healthy automatic scheduler or queued job. Current GET
shows current state; idempotent mutation replay retains its original response and
ETag after later revisions. No schedule deletion, public materialize endpoint or
implicit stop of an existing job is provided. Pause is a reasoned full revision;
use the separate owned job cancellation API when work must be stopped.

Occurrence reads preserve the frozen policy/window, expected/due/materialization
UTC times, observed version/sequence, next slot, queue/skip outcome and owned job
links. They remain readable after key removal and do not inherit today's edited
policy. Canonical hashes/zoneinfo windows, actual count/progress and complete
managed-job scope/deterministic intent are validated before serialization. Invalid
retained metadata/receipts fail with fixed **503 SOURCE_SCHEDULE_UNAVAILABLE**;
GET never silently repairs, rehashes or echoes invalid private data. DTOs do not
dump owner IDs, raw policy/request JSON, private profile hashes, leases, upstream
cursors or credential metadata. Name/time cursor values are checked before SQL;
clients must use the returned opaque cursor rather than reconstruct one from a
formatted timestamp.

## Integration still required

Automatic leader-worker scheduling must prepare private profiles outside writers,
then call the controller and reuse existing aliases, shared quotas/circuit and
fenced execution. There is no startup-created schedule, implicit provider
permission, quota reset or automatic canonical dividend/receipt/share/cash/FX
adoption. Worker stop/restart/backpressure and responsive scheduling UI need their
own end-to-end acceptance. Current HTTP metadata enablement must not be presented
as completed automatic source execution.

Paid/live providers, verified trading-session calendars, target NAS and production
schedule acceptance remain NOT_TESTED.
