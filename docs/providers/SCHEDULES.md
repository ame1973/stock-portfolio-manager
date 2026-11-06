# Explicit source schedule timing

**Current implementation:** the validated timing policy/date calculations and
migration `0011_source_schedules` are installed internally. The migration adds
owned disabled definitions and append-only occurrence storage, not automatic
scheduling. Owner CRUD, the due-occurrence controller and worker/UI integration
are **not yet installed**. Importing a policy or migrating the database creates
no schedule, job, provider enablement, HTTP request or financial mutation. The
existing manual source-job workspace remains available; this foundation is not
a scheduler API.

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

A synthetic **internal policy**, not an available HTTP request, is:

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
be updated/deleted and schedule identity cannot be deleted. The future controller
must enqueue the job, insert the occurrence and append its audit in the **same short
writer transaction**. A late failure rolls all three and parent progress back.
SQLite validates policy shape, catalogue market/timezone, civil dates and window
arithmetic; callers must still verify canonical JSON/SHA-256 and exact `zoneinfo`
slot/next-slot calculations. There is no SQL approximation of DST or a trusted
exchange-session calendar, and no supported operator SQL activation workflow.

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

## Integration still required

The due controller must recheck owner/version/enablement, coalesce missed slots
without an unbounded backlog, retain original occurrence receipts on replay, and
atomically materialize unique jobs/audit/advance. Pin current private configuration
outside writer transactions per occurrence, then reuse the existing sealed job
controller, aliases, shared quotas/circuit and fenced worker. Actor-bound CRUD,
CAS/idempotency, explicit enablement, safe diagnostics and actual worker/UI
integration must pass their own gates before exposing owner controls. An enabled
schedule never resets shared upstream spending or adopts dividends, receipts,
shares, cash or historical FX.

Paid/live providers, verified trading-session calendars, target NAS and production
schedule acceptance remain NOT_TESTED.
