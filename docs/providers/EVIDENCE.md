# Provider evidence storage and integrity

Provider observations are source assertions, not financial facts. A successful
capture does not verify a dividend, adopt a corporate action, recognize income,
create a receipt, change shares/cash, or establish complete upstream coverage.
The [worker](WORKER.md) executes source jobs queued through the
[authenticated enqueue/read/cancel API](JOBS.md); automatic schedules and
provider-status/evidence/review HTTP APIs remain unfinished.
All paid/live coverage validation is NOT_TESTED.

## Immutable records

- `provider_fetch_contexts` retains the initiating owner, security, operation,
  inclusive requested window, explicit dated alias ID/version and exchange
  context, parser label, normalization version, and non-secret operator profile.
  The profile includes rights references and configured budgets, not credentials.
  A quota reservation may retain this planned context before external I/O; context
  presence alone is not evidence of a request, response or completed capture.
- `provider_runs` retains a **completed page attempt**, including sanitized state,
  timing, HTTP outcome where applicable, requested window, response fingerprint,
  decoded-payload checksum, query-free source pointer, parsing issues and a safe
  continuation reference. A run is not a mutable job or a whole-history claim.
- `dividend_observations` retains exact nullable DPS/currency/dates/basis and the
  normalized source assertion. Missing DPS is different from known zero. The
  source currency, frequency and period do not imply canonical event terms.
- `split_observations` retains positive INT64 rational terms and the normalized
  assertion. Its scope is always `PROVIDER_ADJUSTMENT_ONLY`, never ledger adoption.
- `provider_observation_sightings` links each run's ordered source assertions to
  their immutable observations, including assertions seen on earlier attempts.

Updates and deletions, including no-op updates, are rejected for this history.
The run, new observations, sightings and capture audit commit or roll back in one
short write unit of work. External HTTP and credential-file reads do not belong
inside that transaction. No capture changes canonical dividends, entitlements,
receipts, ledger facts, portfolio revisions or provider-health projections.

The exchange/security catalogue and normalized observations are installation-wide
for this single-owner product. Owner attribution is retained on contexts, runs
and audit; this is not a multi-tenant market-data isolation contract. A supplied
job must belong to the initiating owner.

## Repeated retrievals and corrections

A source assertion is deduplicated by provider, security and canonical normalized
hash. A second retrieval adds a run and sighting, preserving the first run,
retrieval time, source pointer and raw checksum on the original observation.
Changed terms or citations produce a different immutable assertion, not an
in-place correction or another financial event. Same-date regular and special
assertions remain distinct. Canonical matching/review is a separate operation.

A repeated attempt ID must retain the same context, page, job and original timing
metadata. An identical replay returns the original receipt with **zero writes**,
including zero new audit rows. Its `observations_new` is the original committed
attempt's count, not permission to increment job progress again. Progress must
aggregate unique committed attempts/checkpoints. Reusing the ID with changed
input is a conflict and rolls back.

An existing normalized hash with missing or different normalized content is an
explicit conflict. It is not silently linked, rewritten or backfilled. Historical
attempt replay remains possible after a later alias edit; a new capture must
resolve to the exact observed alias/version/window/exchange context again.

## Identity, rights and privacy boundaries

The caller must load a trusted private configuration and check
`ProviderConfigurationStore.unchanged` before and after external I/O. Comparing
filesystem timestamps or the public revision alone is insufficient: a same-length
key rewrite can retain those timestamps. The store compares reloaded secrets in
memory without persisting a plaintext-key hash. The persistence function does not
perform these filesystem checks for its caller.

Snapshots retain metadata and fixed versioned credential references only. Keys,
full upstream queries, raw response bodies and arbitrary private configuration
fields are not stored. Full raw-payload retention remains disabled. A checksum
is not a license grant or permission to retain a payload. Configuration budgets
are retained as evidence; durable quota reservation/enforcement belongs to the
separate [upstream attempt library](BUDGETS.md), not this capture function.

Network successes require a matching fixed-provider source pointer, a successful
HTTP status and a decoded-payload checksum. Manual/HKEX/SEC assertions instead
retain typed citations and cannot claim an HTTP attempt. Official HTTPS host
attribution validates the supplied reference, not the document's contents; the
capture path does not fetch cited documents. See [configuration and rotation](CONFIGURATION.md)
for private files, rights acknowledgment and local-only diagnostics.

Requested dates are not observed coverage. Parsing completeness is not complete
pagination/history or financial verification. `NO_DATA`, HTTP/auth/rate errors,
unsupported/unconfigured operations and partial pages remain distinct. This
capture path cannot assert `EMPTY_CONFIRMED` without authoritative coverage proof.
Yahoo and yfinance remain one YAHOO origin, and human citations are not another
independent vendor vote.

## Upgrading retained evidence

Run `spm-manage migrate` explicitly through the normal deployment path. Migration
`0008_provider_evidence` preserves existing financial facts and all original
provider-run/observation columns. Retained rows receive `provenance_version=0`;
missing normalized content, owner, alias, configuration or parser context is not
invented. These rows remain unresolved legacy evidence, not automatically trusted
or newly verified observations. New captures require version-1 provenance.

The upgrade checks retained scaled DPS for exact nonnegative integer storage.
Corrupt REAL values are refused rather than rounded into validity. New dates use
strict calendar validation even on older SQLite versions whose plain `date()`
leaves impossible days unchanged. A failed preflight or later schema change rolls
back the complete upgrade, including its
head and audit. A successful upgrade appends a schema audit for each existing
owner; an unchanged rerun does not duplicate it. The normal local rollback
snapshot is sensitive and is not an encrypted recovery backup. Downgrade is not a
history-deletion operation: restore a validated pre-upgrade backup instead.
