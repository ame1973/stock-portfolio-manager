# Reauthenticated private credential import

`POST /api/v1/provider-profiles/{provider_id}/credential` imports one immutable
private credential edition and binds it to a **new disabled owned policy
revision**. It is available for `eodhd`, `alphavantage` and `massive` only.
Import does not enable access, acknowledge rights, probe an upstream, establish
key validity or coverage, reset a budget, cancel work or change financial facts.

**Managed runtime selection is installed.** New responses report
`scope: OWNED_PROVIDER_POLICY` and `runtime_selection_installed: true`, not health
or live coverage. Import/rotation still saves a disabled policy: the latest owned
decision overrides operator fallback and blocks future pages under superseded
pins. Explicit later enablement with rights/budgets is required for new source
work. Already sent requests are not remotely cancelled and uncertain spending
is not refunded. The [browser workspace](PROFILES.md#browser-workspace) provides
write-only import and rotation. There is no key-export endpoint or key-writing
CLI. The read-only diagnostic CLI remains operator-file-only.

## Prerequisites and request

1. Use the configured trusted application origin and a current owner session.
   Production remote access requires the trusted HTTPS deployment boundary.
2. Confirm the password through `POST /api/v1/auth/reauth`; the existing
   five-minute window and durable authentication limits apply.
3. Create an explicitly disabled [owned policy definition](PROFILES.md) if none
   exists. Import never adopts an operator file or another owner's definition.
4. Ensure the [separate managed private directory](CONFIGURATION.md#separate-managed-credential-storage)
   is provisioned. Compose provides an initially empty mode0700 volume, writable
   to API and read-only to worker, outside data/backups and operator secrets.
   Local Python requires explicit `MANAGED_PROVIDER_SECRET_DIR`; no directory
   is created and no fallback path is used.
5. Send JSON containing only `credential` and `reason`, with matching `Origin`,
   `X-CSRF-Token`, a 16–200-character `Idempotency-Key`, and the current quoted
   positive `If-Match` version.

`credential` is a write-only strict string of **1–256 printable nonspace ASCII
characters**. It is not trimmed: spaces, control characters, terminal line
endings, Unicode, empty strings and non-string values are rejected. This differs
from the operator-file reader's explicitly permitted terminal newline. `reason`
is a concrete non-secret 10–1000-character explanation; NUL is rejected before
private preparation. A key cannot be supplied through a URL, query, filename,
artifact UUID, owner ID, policy field or operator-file reference.

Never put a credential in a reason/reference, shell argument, environment
variable, Git, logs or browser storage. Keep it only in the transient private
request flow. There is deliberately no example key or credential-bearing shell
command in this guide.

## Successful result and rotation

A successful import returns **200**, the new policy record and its quoted ETag.
`credential_reference_present: true` reports the committed association, not a
health check or a promise that the file will remain available. Credential values,
edition IDs, private paths, owner IDs and private fingerprints are withheld.

The current policy's other declarations are retained, but **enabled is always
false**, including when rotating an enabled declaration. Enabling a later
policy remains a separate reasoned CAS operation with explicit rights/budgets.
The installed owner selector then uses that current policy and exact private
edition, with DB/private-file/alias/shared-budget checks around I/O. Neither
operation rewrites the separate operator-file configuration. Old queued null or
superseded profile pins never become permission for the new edition; review
current state and create a fresh source intent when required.

Rotation prepares a new server-named edition; it never overwrites an old key.
Each import appends `PROVIDER_PROFILE_REVISED` and
`PROVIDER_CREDENTIAL_IMPORTED` audit events correlated to the server's request
UUID and the actual new revision. Audit hashes contain only non-secret metadata,
never a credential or a plaintext-key hash. Revision, both audits and the
serialized idempotency response commit in one short writer transaction.
Clearing the reference through the policy API retains history and private files;
it is not a filesystem deletion or an upstream cancellation.

## Replay and authorization

The fingerprint covers the **actual credential value**, normalized reason and
observed version, using the current opaque session secret as an HMAC key. Masked
SecretStr serialization is not used as the fingerprint, and no fast unkeyed
credential-verification hash is retained.

A read-only advisory check validates the current session/CSRF/reauthentication
and rejects an input-conflicting key before private preparation. A committed
matching replay skips all private storage access and preparation, even after
rotation, clearing or loss of the configured directory. The authoritative short
writer rechecks session/CSRF/reauthentication **before** returning a cached result
or performing a new CAS change. The advisory read is not an authorization grant
or a reservation.

A valid replay returns the original DTO/ETag, not the latest state; use GET for
current metadata/capability. Historical pre-install receipts retain their original
scope and false capability rather than being rewritten. Expired authorization rejects even a previously committed
import. Confirm the password and retry the unchanged intent within the same
session when appropriate. A new session changes the sensitive fingerprint;
inspect current state before starting a new explicit intent rather than blindly
reusing an old session's key. Changed credential, reason or observed version with
the same key returns 409. A stale new intent returns 412.

If a cached receipt disappears between advisory read and writer, the operation
fails closed with 409 `CREDENTIAL_RETRY_REQUIRED`; it does not create files from
inside the writer. Refresh state before choosing a fresh intent.

## Filesystem/database boundary and failures

The filesystem and SQLite are **not one atomic resource**. All private path
checks, exclusive mode0600 creation, key/ready/directory fsyncs, reopening and
actual constant-time value rechecks occur outside database transactions. The
server supplies the provider-scoped UUID names. Neither a ready file nor a
successful preparation is owner authorization or an active binding.

A new intent checks the owned current version before preparation and rechecks
it at commit. Session expiry/revocation/CSRF change, a concurrent policy change,
late audit/serialization failure or process interruption can leave private files
without a committed association. These failed/crash-uncertain files are
**retained and unselected**, never overwritten, deleted, scanned for adoption or
silently attached on retry. A retry without a committed receipt prepares a new
edition. Concurrent identical requests can therefore prepare extra unselected
editions, but only one revision/import audit/idempotency receipt commits. Distinct
competing intents for one observed version have one successful CAS writer.

Storage absence, unsafe/unreadable paths, preparation failure and a changed
snapshot return fixed 503 `PROVIDER_CREDENTIAL_STORAGE_UNAVAILABLE`, without
private exception details or a false import success. All handled responses,
including validation/auth/CAS/storage failures, are `Cache-Control: no-store`.
Orphan reconciliation and private-volume recovery remain separate unfinished
operator workflows; do not manually repoint a definition at discovered files.

Credentials are outside SQLite and backup volumes. Preserve private storage
separately; a database backup alone does not restore referenced credentials.
Paid/live provider coverage, target NAS storage durability and orphan recovery
remain separate, unfinished acceptance boundaries. The responsive settings UI is
available, with a separate temporary real-HTTP/browser fixture rather than seeded
deployment keys; see the [browser/testing boundaries](PROFILES.md#browser-workspace).
