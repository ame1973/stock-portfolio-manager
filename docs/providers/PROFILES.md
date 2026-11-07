# Owned provider policies and private credential import

These APIs expose owned policy definitions and a separate
[reauthenticated private credential import](CREDENTIALS.md). Every record reports
`scope: POLICY_DEFINITION_ONLY` and `runtime_selection_installed: false`.
Import binds an immutable private edition to a new disabled definition; it does
not activate managed source access, replace operator files, probe a provider,
grant a license or change financial facts. There is no settings editor UI yet.

The [operator-file loader](CONFIGURATION.md), existing provider diagnostics, CLI
and worker selection still use the current operator-file interface. In
particular, **disabling or clearing a definition is not a kill switch for
operator-configured jobs**. Manage operator access through its trusted file
provisioning boundary and use the [job stop-intent API](JOBS.md) when required;
these metadata writes do not cancel jobs or refund uncertain upstream spending.
Managed runtime integration and settings UI remain unfinished; import alone is
not active access.

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
**declarations** need explicit rights acknowledgment and the access mode's
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
GET returns the current version. Stale editors return 412 without overwriting
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
