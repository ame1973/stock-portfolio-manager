# Server-only provider configuration

This is the implemented **read-only operator-file interface**. It is not an authenticated key-import API, a synchronization command, a live provider test or a license grant. No directory/key is created by the loader. Missing or invalid provider files do not make the financial application unready.

The [owned policy-definition API](PROFILES.md) enforces reauthentication, audit, idempotency and version checks for metadata. A separate [private credential-import operation](CREDENTIALS.md) now prepares and binds immutable editions to disabled definitions; it does not activate them. Trusted local directory provisioning and active managed runtime integration remain separate boundaries; current operator files stay authoritative.

## Paths and permissions

| Content | Local Python/CLI | Compose containers |
| --- | --- | --- |
| Metadata | `DATA_DIR/provider-config/<provider_id>.json` | `/data/provider-config/<provider_id>.json` |
| Local credential | `PROVIDER_SECRET_DIR/<provider_id>.<credential_version>.key` | `/run/spm-provider-secrets/<provider_id>.<credential_version>.key` |
| Docker secret | `/run/secrets/spm-<provider_id>-<credential_version>` | Same fixed path |

`PROVIDER_SECRET_DIR` defaults to the repository's ignored `secrets/` directory for local commands. Compose explicitly uses a separate `provider_secrets` volume, mounted **read-only** into API/worker; it is outside both the SQLite data and backup volumes. The image initializes that directory as UID/GID **10001:10001**, mode **0700**. The empty volume contains no generated credentials.

- Metadata/local-key directories must belong to the process UID and have mode **0700** or **0500**. Files must belong to that UID and have mode **0600** or **0400**.
- Provision files through a trusted NAS/secret-management interface with the correct ownership. Do not place a key value in command-line arguments, shell history, environment variables, Git, configuration JSON or logs. The application does not currently offer a key-writing command.
- Local credentials must be outside the data/backup trees; ancestor/descendant overlaps are rejected. They must not be baked into an image. `secrets/`, `.env*`, `*.key` and `*.pem` are excluded from the relevant Git/build contexts.
- Symlinks in any traversed directory/file, hard-linked files, nonregular files/FIFOs and insecure ownership/permissions are rejected. Directory-relative no-follow descriptors and bounded reads are used; checks do not create files or wait on a FIFO.
- A Docker-secret reference must actually be on a **read-only mount**, not merely an ordinary world-readable file. Its owner must be root or the process UID, and executable/group-writable/other-writable modes are rejected. Actual mounted UID/mode is authoritative; some Compose host-file mounts do not implement requested ownership remapping.

Metadata is limited to **16 KiB**. A key is **1–256 printable ASCII bytes** with no spaces/control characters. One terminal LF or CRLF is accepted; other whitespace is not silently stripped. No key is supplied by default.

## Separate managed credential storage

`MANAGED_PROVIDER_SECRET_DIR` is an explicit optional local path; unset/empty means
managed storage is not configured, **not a fallback to operator/data storage**.
The `managed_artifacts(settings)` factory creates no directory, file, database or
provider grant. It reuses immutable preparation and rejects resolved equal,
ancestor or descendant overlap with operator secrets, data and backups. Original
no-follow owner/mode/file checks still apply; the private setting is hidden in repr.

Compose provisions a separate initially empty `managed_provider_secrets` volume
at `/run/spm-managed-provider-secrets`, owner **10001:10001**, mode **0700**. Only
the API mounts it writable; the worker mounts the same volume **read-only**.
Both processes retain read-only root filesystems and the original
`provider_secrets` mount remains read-only in both. This is an explicit storage
role boundary for the [authenticated importer](CREDENTIALS.md), not permission to
rewrite operator credentials. A read-only worker can reopen an exact edition but cannot
prepare another one; `providers-check` still inspects operator files only, without
DB/bootstrap/upstream access or managed-file adoption.

For local Python, pre-provision a separate process-owned mode0700 directory
outside all three trees and Git/build contexts. The conventional `managed-secrets/`
name is excluded from Git and Docker contexts, including private ready metadata.
Storage presence never enables import or runtime access without its authorization
checks. The import API is installed separately; no key-writing CLI/UI or active
managed runtime is supplied by the mount. Do not place synthetic test keys in
deployment volumes. Keys are outside SQLite/backup volumes; preserve their private
volume separately and do not assume a database backup restores credentials.

## Metadata format

Each of the eight provider IDs has its own optional file: `eodhd`, `alphavantage`, `massive`, `yahoo`, `yfinance`, `hkex_evidence`, `sec_evidence`, `manual`. The body's `provider_id` must match its filename. Unknown fields, inline keys, arbitrary credential paths, duplicate JSON keys, nonfinite numbers and coerced numeric/boolean strings are rejected.

A safe **disabled** EODHD metadata example is:

```json
{
  "provider_id": "eodhd",
  "version": 1,
  "enabled": false,
  "terms_acknowledged": false,
  "terms_reference": "",
  "terms_reason": null,
  "requests_per_minute": null,
  "requests_per_day": null,
  "credential_source": "LOCAL_FILE",
  "credential_version": 1,
  "raw_payload_storage": false
}
```

There is deliberately no example key or guessed plan budget. Before enabling a network profile:

1. Check the actual provider terms, permitted normalized-data use, plan coverage and request budgets.
2. Set `terms_reference` to a bounded non-secret reference and `terms_reason` to an explicit explanation (10–1000 characters), then acknowledge the terms. This is an operator assertion, **not verified licensing or live coverage**.
3. Supply positive integer minute/daily limits from that plan. Enabled network profiles require both; booleans, strings, zero, negative values and values above 2147483647 are rejected. The loader validates them; the worker enforces [durable shared-origin reservations and retry policy](BUDGETS.md).
4. For EODHD/Alpha Vantage/Massive, provision the referenced private credential. Presence alone never enables traffic.
5. Set `enabled` explicitly and inspect configuration status through `spm-manage providers-check`, the [authenticated diagnostic APIs](DIAGNOSTICS.md), or the [source workspace](WORKSPACE.md). The [worker](WORKER.md) dispatches source jobs explicitly queued through the [owner API](JOBS.md), workspace or explicitly enabled schedules; startup creates no default schedule or provider grant. Managed key settings remain under development.

`version` and `credential_version` are explicit positive integers, at most 2147483647. `credential_source`/`credential_version` must appear together or both be null. Accepted sources are `LOCAL_FILE` and `DOCKER_SECRET`; no client-selected filename/path is accepted.

Yahoo/yfinance do not accept API credentials. They share the **YAHOO** upstream/origin; separate profiles must not become independent corroboration votes or quota bypasses. The optional SDK still needs its installed, verified parser profile. HKEX/SEC/manual profiles accept neither credentials nor network budgets: they normalize supplied human evidence, not announcement scrapers.

`raw_payload_storage` currently accepts only strict `false`. An acknowledgment cannot grant raw retention or change UNKNOWN/SPLIT_ADJUSTED dividend terms into verified income.

## Rotation and concurrent changes

Create a **new versioned credential file**, then atomically replace the private metadata file with an increased policy version and the new credential reference. Do not overwrite/reuse an old credential version. Local and Docker-secret references follow the same versioned naming rule.

The loader checks descriptor/path identity before/after bounded reads and rechecks metadata after loading the credential. A loaded server-only snapshot carries private file signatures and a checksum of the **validated non-secret metadata**, never a plaintext-key hash.

Callers must use `ProviderConfigurationStore.unchanged(snapshot)` before/after external I/O and retain the observed alias/config versions. File timestamps alone are insufficient: a same-length rewrite can retain identical filesystem timestamps. The recheck also compares current/observed credential values in constant time **in server memory**, without logging, serializing or hashing the credential. A changed/unsafe/unreadable snapshot is not current.

This does not make privileged operator filesystem edits into audited HTTP mutations. The separate managed import now supplies its authenticated/audited binding boundary; active runtime/job/version integration remains subsequent work. Neither policy history nor import replaces this operator interface.

## Inactive immutable credential preparation

An internal `CredentialArtifactStore` library prepares a private immutable
credential edition for EODHD, Alpha Vantage or Massive. **Preparation itself is
not owner authorization, an active profile, terms acknowledgment or provider
readiness.** The [protected importer](CREDENTIALS.md) now uses this library and
commits its separate owned association. Startup, diagnostics and current operator
selection do not discover or adopt artifacts; read-only operator mounts remain
unchanged and no key-writing CLI/UI is installed.

Preparation requires an explicitly supplied, pre-provisioned owner-only directory
outside both data and backup trees, including resolved aliases. It creates no
directory. Inputs are server-only `SecretStr` values of exactly 1–256 printable
nonspace ASCII characters; unlike operator-file line endings, this input is not
trimmed. Yahoo/yfinance and manual/official evidence profiles cannot prepare API
credentials. Provider and server-generated UUID form fixed basenames; a caller
cannot supply a path or overwrite an existing edition.

The writer uses directory-relative no-follow/exclusive creation and mode0600,
handles short writes, fsyncs the key and canonical non-secret ready manifest, then
fsyncs the directory and rereads matching identities/value before returning.
Symlinks, hard links, FIFOs, unsafe modes/owners, unsupported secure-open platforms,
malformed/duplicate/nonfinite/oversized metadata, identity changes and I/O failures
fail with fixed safe codes. The ready manifest contains only schema/provider/UUID
and bounded length, **never a key or plaintext-key hash**. Snapshot repr hides
credentials, paths and file signatures. `unchanged` compares current signatures
and the actual credential in constant time only in server memory; timestamps alone
are insufficient even after a same-length rewrite.

Failed/crash-uncertain files are retained and unselected, not deleted or adopted.
A complete file/ready manifest alone is not proof of a committed import. The
protected importer verifies preparation outside the writer, then atomically binds
the server reference to a new disabled owned revision with current reauthentication,
CAS, audit and sensitive idempotency. This is not filesystem/database atomicity:
a failed or concurrent request may leave unselected files. See the
[import failure and replay contract](CREDENTIALS.md#filesystemdatabase-boundary-and-failures).
The preparation primitive itself writes no database, active binding or operator
file and makes no provider request, quota reset or financial change. Managed runtime
activation, orphan reconciliation and settings UI remain subsequent work. Never
provision test artifacts or synthetic keys in deployment credential volumes.

## Append-only managed policy schema

Migration `0012_provider_profiles` adds an initially empty
`provider_profile_revisions` table. This storage is now exposed through the
[owned policy-definition API](PROFILES.md), with a separately authorized private
import operation, **not an active runtime selector**. The current
operator-file loader, diagnostic CLI, worker selection and read-only secret
mounts remain unchanged. No existing settings, jobs, profiles or credential files
are adopted by the migration.

Each owner/provider has a contiguous, increasing revision history. The first
revision must be disabled; later changes, including disablement, credential
rotation and credential clearing, append rather than rewrite or delete history.
Competing inserts for the same next version cannot both commit. The controller selects the greatest version, **not the last enabled version**;
a future managed runtime consumer must preserve that selection. A disabled or
unavailable managed selection must never silently fall back to an
older policy or discovered file.

Typed columns retain declared enablement/rights, bounded integer minute/day
budgets, an optional opaque credential-artifact UUID, a concrete change reason,
a server request UUID and canonical UTC time. Credentials, filesystem paths and
plaintext-key hashes are not part of this table's contract. Only the three
API-key providers accept artifact references; a referenced edition cannot be
reassigned to another owner/provider. Manual/official evidence profiles accept
no network budgets, and raw-payload storage remains false. An enabled declaration
requires acknowledged terms and its access mode's budgets/credential reference;
these declarations still do not prove licensed access, file readiness or health.

SQLite enforces metadata shape, ownership, monotonic versions/times and immutable
history. It cannot prove password reauthentication, private-file preparation or
an HTTP idempotency result. The definition API now checks current session/CSRF,
reauthentication before cached replay, version, audit and serialized result in
one short transaction. The credential importer separately verifies its prepared
edition outside writers and uses actual-value session-HMAC idempotency. Future
runtime integration must recheck the committed selection and private credential
around provider I/O. Runtime use is not installed by this schema or import API;
no provider request or quota reset is introduced.

## Internal versioned controller

`providers/profiles.py` now provides internal `latest` and `save` operations over
that history. Both operator files and managed definitions reuse the same frozen
`ProviderPolicy` validation; operator credential-source/version rules and current
file-loading behavior are unchanged. These are server library functions, **not
HTTP routes, import commands, password checks or active runtime selection**.

The caller supplies an already authorized owner, a server-minted request UUID,
a full non-secret policy, a concrete reason and an aware server clock. Creation
requires no prior version and remains disabled. Revision requires the observed
positive version; stale versions return the shared 412 error. Retaining a
credential uses explicit internal `KEEP`, clearing uses null, and binding uses a
checked server-only `CredentialRef`, never a browser-selected path or UUID.
Private preparation/rechecks must complete outside the writer before that call;
the controller does not load a key or treat the reference itself as file proof.

The short caller-owned writer appends one normalized revision and correlated
audit together; a late audit failure rolls back both. Audit hashes cover only
non-secret policy metadata and credential presence, never the credential or its
plaintext hash. Reads select the greatest version even when disabled/cleared,
validate canonical mirrored fields, contiguous progress and edition ownership,
and reject corrupt retained rows with a fixed code without echo, repair or
fallback. Reads write no definition/audit facts. The [definition API](PROFILES.md)
now supplies HTTP session/CSRF/reauthentication and non-secret idempotency around
this controller. The separate private importer adds sensitive actual-value
idempotency and checked filesystem preparation outside the writer. Runtime/diagnostic
consumption and settings UI remain unfinished. Existing operator profiles remain
the current runtime path; managed declarations/imports alone cause no provider
request, quota reset or financial change.

## Owned selection core, not installed runtime consumption

The internal `OwnerConfigurationStore(settings, engine, owner_id)` now reads an
explicitly supplied active owner's greatest managed decision. An owned decision
always takes precedence, including disabled/cleared policies and missing or unsafe
editions; it never chooses the last enabled version or another owner's key. Only
an absent owned definition permits the existing installation-provisioned operator
file fallback. Missing/inactive owners, unreadable/corrupt managed history and
metadata races fail closed, not through operator recovery.

Private file access occurs between closed read-only metadata UoWs. A second read
must still match the observed decision after either operator-file or managed-edition
loading. `unchanged` retains actual constant-time credential comparison as well as
metadata/edition signatures; identical reported timestamps are insufficient.
Shared Yahoo/yfinance budget snapshots use the same owned decisions and remaining
operator peers, detect even changed unconfigured peer versions, and create no
reservations or provider requests.

`ManagedPolicySnapshot` records the real owner/revision/version/edition identity
and `ManagedConfigurationRevision` retains the actual key/ready signatures. These
are private types, not invented `LOCAL_FILE`/`DOCKER_SECRET` versions or filesystem
profile signatures. Existing operator snapshots/hashes remain unchanged. Prepared
managed contexts reject a different owner before capture or network use.

**This library is not yet wired into production jobs, schedules or diagnostics.**
Migration `0013_managed_source_contexts` now permits exact managed provenance and
adds writer-side current-selection fences, but does not switch those consumers.
Current public records still report `runtime_selection_installed: false`;
importing/disabling a definition remains no kill switch for operator-configured
traffic. The default store and `providers-check` stay operator-only/no DB/bootstrap/probe.
Library capture/quota checks prove transactional compatibility, not installed
managed worker execution, key validity, live coverage or a settings UI.

### Managed provenance and current-selection fences

The new migration preserves existing context/profile/financial rows and tables.
A managed context identifies its actual owner, provider, revision/version and
nullable private credential edition through `configuration_source: MANAGED_PROFILE`.
It cannot contain operator credential-source/version fields. SQLite requires the
exact enabled latest owned revision and mirrored policy fields, rejects duplicate,
missing and unknown JSON keys, and blocks `INSERT OR REPLACE` on an existing
context. No operator snapshots or file signatures are fabricated; this migration
creates no policy, key, job, request or access grant.

For new reservations and page outcomes, the short writer rechecks the selected
owner and greatest managed revision using database metadata only, including when
the context already exists and the returned page is empty, partial or failed.
An owned operator fallback carries a private scope claim and can commit only
while that owner still has no definition. Existing **unscoped operator producers**
retain their old behavior until explicit consumer integration; a definition-only
write must not silently become an operator traffic switch. Scope claims stay out
of public responses, operator profile hashes and credential values.

Completed page/checkpoint/reservation replay still returns its original receipt
without new writes after policy rotation or disablement. A replayed reservation
never permits another HTTP call. A reservation rejected after external I/O remains
spent; recovery records the unknown outcome rather than refunding/reissuing it.
Private file/value checks before and after I/O, alias CAS and shared-origin peer
checks remain required outside the writer. These database fences do not replace
filesystem checks or establish current provider rights/coverage. The owned
[shared-budget envelope](BUDGETS.md#owned-peer-transaction-fence-not-installed-runtime-selection)
also fences every current peer decision in new grant/result transactions, including
unconfigured peers becoming enabled or gaining a managed override. This internal
prerequisite still does not switch production consumers or runtime flags.

## Read-only diagnostics

```sh
# Does not need a database, owner bootstrap or provider request.
uv run spm-manage providers-check

# Runs as the service UID and checks the actual mounted permissions.
docker compose exec -T api spm-manage providers-check
```

The JSON output has `scope: CONFIGURATION_ONLY` and eight sanitized provider reports. It never includes keys, credential paths, profile bodies or private snapshot tokens. Exit success means the inspection completed, **not** that a provider is online or synchronized.

Typical reasons include:

- `CONFIG_NOT_FOUND`, `PROVIDER_DISABLED`, `RIGHTS_NOT_ACKNOWLEDGED`: normal explicit unconfigured states.
- `CONFIG_INVALID`, `CONFIG_PROVIDER_MISMATCH`: invalid/incorrect metadata, not empty dividends.
- `CONFIG_UNSAFE_DIRECTORY`, `CONFIG_UNSAFE_PERMISSIONS`, `CREDENTIAL_UNSAFE_PATH`, `CREDENTIAL_UNSAFE_FILE`: fix the operator-provisioned ownership/path/type rather than weakening checks.
- `NO_API_KEY`, `CREDENTIAL_NOT_FOUND`, `INVALID_CREDENTIAL`: no usable referenced key; no request is made.
- `UNSAFE_SECRET_LOCATION`, `CREDENTIAL_UNSAFE_DOCKER_SECRET`: use separate private local storage or a real read-only secret mount.
- `SDK_NOT_INSTALLED`, `SDK_VERSION_UNSUPPORTED`: optional parser availability, not upstream no-data.
- `NOT_PROBED`, `MANUAL_INPUT_ONLY`, `SDK_PARSER_READY_NOT_PROBED`: valid local configuration only; last success/quota remaining remain unknown.

One invalid provider is reported independently of other valid profiles. Every live-validation field remains **NOT_TESTED**. Use the separate [live integration report template](LIVE_INTEGRATION_REPORT_TEMPLATE.md) only with actual permission, credentials/plan and authorized samples; file checks do not satisfy that gate.
