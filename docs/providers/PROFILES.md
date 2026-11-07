# Owned provider policies and private credential import

These APIs expose owned policies and a separate
[reauthenticated private credential import](CREDENTIALS.md). New records report
`scope: OWNED_PROVIDER_POLICY` and `runtime_selection_installed: true`: explicit
HTTP queueing, automatic schedules, worker dispatch and authenticated diagnostics
now use the current owner's greatest policy revision. This capability describes
installed code, not worker health, a valid key, a license or verified coverage.
Import binds an immutable private edition to a new **disabled** policy. The
responsive browser workspace is available at `/settings/providers`, with a
per-source current-policy/history page at `/settings/providers/{provider_id}`.

An owned policy always overrides installation operator files, including disabled,
cleared or unavailable selections; it never revives an older enabled policy or
another owner's key. Only an absent owned definition permits the existing
operator-file fallback. Disabling, clearing or importing a rotated edition blocks
new pages under the old selection, without deleting files or cancelling jobs.
An already sent request is not remotely cancelled; a changed selection causes
its late result to be discarded without refund. Use the [job stop-intent API](JOBS.md)
when cancellation itself is required. Historical committed evidence/replay stays
unchanged. The diagnostic **CLI** deliberately remains operator-file-only/no DB
or upstream probe, so its local report can differ from the authenticated owner
report.

## Browser workspace

Open **Source authorization and keys** from Settings, or follow its link from the
data-center provider catalogue. That catalogue remains a separate read-only
local/recorded diagnostic view; neither page sends an upstream probe.

- Create an explicit **disabled** policy; no provider, rights or software budget
  is inferred. Creating an owned decision overrides installation-file fallback,
  even when the new decision is disabled. Manual/official evidence profiles have
  no API-key or network-budget controls. Yahoo/yfinance share one upstream.
- Open a source's detail page to inspect the current decision and immutable
  version/reason/physical-ID history. Desktop tables and phone/tablet cards render
  only the server page/order; counts are not a total. History search/cursors stay
  in the authenticated in-memory workspace, not the URL or persistent storage.
- Every write Sheet includes the existing password-confirmation form. Confirming
  the password does not submit the policy/key form. The server remains the
  authority on the five-minute window; reauthentication preserves an unchanged
  failed command and its idempotency key. Passwords and keys never enter query or
  mutation caches.
- Only EODHD, Alpha Vantage and Massive offer write-only key import/rotation.
  Native password controls never display the key as text. Exact ASCII validation
  rejects whitespace/Unicode/overlength input. Unsafe paste is blocked **before**
  native newline removal/truncation and clears any previous key so it cannot be
  submitted accidentally. A confirmed import returns a disabled revision;
  separate explicit enablement still requires rights, limits and acknowledgment.
- Policy and key editors freeze the observed version when opened. A background
  refresh never silently upgrades an old edit. A 412 or other failed write keeps
  its input; cancel/reload/reconcile before submitting a new intent. Close/Escape
  cannot discard a pending policy, key import or password confirmation.
- Clear a key reference only through a reasoned policy revision; the checkbox
  also selects disabled access. It never deletes files, cancels work or restores
  operator fallback. A stored reference flag is not proof of file/key validity.
- Confirmed writes invalidate source reads only. Notices describe the returned
  revision, which can be an original replay; current state comes from a refreshed
  GET. No ledger/receipt/FX mutation or automatic source queue is created.

See the [private browser testing policy](../testing/PRIVATE_BROWSER_DIAGNOSTICS.md)
for controlled failure diagnostics and the separate real-credential workflow
fixture. Only isolated synthetic data is used; test keys are not seeded into
Compose deployment credential volumes.

## Operations

All responses, including errors, are private and `Cache-Control: no-store`.
Reads require the current authenticated owner; writes additionally require the
configured Origin, CSRF, an idempotency key and password confirmation within the
existing five-minute reauthentication window.

| Operation | Result |
| --- | --- |
| `GET /api/v1/provider-profiles` | Current explicit definitions for this owner, sorted by provider ID; at most eight items, no inferred default profiles |
| `POST /api/v1/provider-profiles` | 201 disabled initial definition, Location and quoted version ETag |
| `GET /api/v1/provider-profiles/{provider_id}` | Current definition and ETag, or 404; never adopts another owner's definition or an operator file |
| `PATCH /api/v1/provider-profiles/{provider_id}` | Full policy/reason revision with quoted `If-Match`; 200 new physical revision and ETag |
| `POST /api/v1/provider-profiles/{provider_id}/credential` | Separate write-only credential import with quoted `If-Match`; 200 new disabled definition/ETag, never activation |
| `GET /api/v1/provider-profiles/{provider_id}/revisions` | Immutable newest-version-first history, bounded `limit`, opaque cursor and literal `q` search over version/reason/ID |

The provider ID must be one of the eight registered profiles. A revision cannot
move provider identity. The first version must be disabled; subsequent enabled
**policies** need explicit rights acknowledgment and the access mode's
budgets/reference. Yahoo/yfinance still share one upstream. Manual/official
profiles accept no network budgets or API credentials. No raw-payload retention
is granted.

## Request bodies

An explicit disabled example for a keyless provider is:

```json
{
  "policy": {
    "provider_id": "yahoo",
    "enabled": false,
    "terms_acknowledged": false,
    "terms_reference": "",
    "terms_reason": null,
    "requests_per_minute": null,
    "requests_per_day": null,
    "raw_payload_storage": false
  },
  "reason": "Save an explicit disabled source policy for later review."
}
```

PATCH sends that complete policy and a new concrete reason, plus the strict
boolean `clear_credential` (default false). False keeps the current **internal
reference**; true clears it in the new definition only. Clearing does not delete
an edition, replace an operator file, rewrite history or stop a job. An enabled
API-key declaration without a bound reference is rejected, not reported ready.

The metadata create/update operations accept no credential value, artifact UUID,
owner ID, credential path or operator-file source/version. Unknown body fields
return a safe 422 without echoing their values. `credential_reference_present`
is only a stored-reference flag, **not file availability or key validity**;
metadata operations and reads do not load keys. The separate
[credential operation](CREDENTIALS.md) uses exact write-only SecretStr input,
preparation outside writers and actual-value session-HMAC idempotency. Never put
a secret in a policy reference/reason, URL, query parameter, shell argument or log.

## Reauthentication, CAS and replay

Use the existing `POST /api/v1/auth/reauth` password-confirmation workflow, not a
password field in a policy request. Password hashing remains outside writer
transactions. Confirmation uses the existing durable attempt limits and
session-keyed sensitive fingerprints; no limits are weakened for these APIs.

For definition writes, the shared short mutation transaction rereads the current
session/CSRF and checks reauthentication **before looking up an idempotent cached
response**, not merely before a new action. Expiry or a session change between
HTTP dependencies and the writer therefore rejects even a previously committed
request. Reauthenticate, then retry the same intent when appropriate.

Policy/reason/version inputs contain no key or password, so their fingerprint is
the normalized public body, including the observed PATCH version. A mismatched
reuse returns 409. A valid replay preserves the original DTO/ETag, even after a
later revision; it does not manufacture a current state or refresh authorization.
GET returns the current version and installed capability. A receipt committed
before runtime installation retains its original `POLICY_DEFINITION_ONLY` scope
and false capability during replay; it is not silently rewritten as a current
report. Both scope values remain readable for this historical compatibility.
Stale editors return 412 without overwriting
newer intent. Revision, audit and serialized idempotency result commit together;
late audit or serialization failure rolls back the entire write.

## History, privacy and errors

History pages return exact physical revision IDs and original definition times.
Version cursors are canonical positive ASCII integers bounded to the metadata
version domain before SQL binding; financial values are not converted or sorted.
The first page does not imply a total, successful fetches or income. Reads create
no definition, audit or idempotency rows; normal authenticated-session activity
tracking can still occur under the existing authentication policy.

Responses withhold owner IDs, credential edition IDs/paths/values, private
profile hashes and request bodies. Stored canonical fields, contiguous progress
and credential ownership are checked; corrupt retained definitions fail with a
fixed 503 `PROVIDER_PROFILE_UNAVAILABLE`, without echo, repair or fallback.

Common failures are 401 for an invalid session, 403 for Origin/CSRF/reauthentication,
404 for an absent owned definition, 409 for a conflict, 412 for a stale version
and 422 for invalid body/cursor/scope. A stored rights reference is still an owner
assertion, not independently verified licensing or live coverage. Paid/live
provider and NAS acceptance remain separate, untested gates.
