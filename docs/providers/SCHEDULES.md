# Explicit source schedule timing

**Current implementation:** the validated, pure timing policy and its date/window
calculations are available internally. Schedule persistence, owner CRUD, durable
occurrence materialization and worker/UI integration are **not yet installed**.
No schedule, job, provider enablement, HTTP request or financial mutation is
created by importing or evaluating this policy. The existing manual source-job
workspace remains available; do not mistake this foundation for a scheduler API.

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

## Integration still required

The durable layer must validate actual catalogue markets/identities, default new
schedules to disabled, recheck owner/version/enablement, materialize a unique
occurrence/job/advance/audit atomically, and prevent concurrent/backlogged duplicate
work. Pin current private configuration outside writer transactions per occurrence,
then use the existing sealed job controller, aliases, shared quotas/circuit and
fenced worker. A persisted initial-backfill marker, missed-slot/coalescing policy,
pause/edit CAS/idempotency, cancellation meaning and safe diagnostics must be tested
before exposing owner controls. An enabled schedule never resets shared upstream
spending or adopts dividends, receipts, shares, cash or historical FX.

Paid/live providers, verified trading-session calendars, target NAS and production
schedule acceptance remain NOT_TESTED.
