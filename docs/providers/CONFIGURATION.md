# Server-only provider configuration

This is the implemented **read-only operator-file interface**. It is not an authenticated key-import API, a synchronization command, a live provider test or a license grant. No directory/key is created by the loader. Missing or invalid provider files do not make the financial application unready.

The future owner-facing key-import/settings workflow must require reauthentication, audit and version checks. Privileged NAS/filesystem provisioning is a separate operator boundary; this interface does not bypass those future HTTP controls.

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
5. Set `enabled` explicitly and inspect configuration status through `spm-manage providers-check` or the [authenticated diagnostic APIs](DIAGNOSTICS.md). The [worker](WORKER.md) dispatches source jobs explicitly queued through the [owner API](JOBS.md), but startup creates none. Automatic schedules and source UI remain under development.

`version` and `credential_version` are explicit positive integers, at most 2147483647. `credential_source`/`credential_version` must appear together or both be null. Accepted sources are `LOCAL_FILE` and `DOCKER_SECRET`; no client-selected filename/path is accepted.

Yahoo/yfinance do not accept API credentials. They share the **YAHOO** upstream/origin; separate profiles must not become independent corroboration votes or quota bypasses. The optional SDK still needs its installed, verified parser profile. HKEX/SEC/manual profiles accept neither credentials nor network budgets: they normalize supplied human evidence, not announcement scrapers.

`raw_payload_storage` currently accepts only strict `false`. An acknowledgment cannot grant raw retention or change UNKNOWN/SPLIT_ADJUSTED dividend terms into verified income.

## Rotation and concurrent changes

Create a **new versioned credential file**, then atomically replace the private metadata file with an increased policy version and the new credential reference. Do not overwrite/reuse an old credential version. Local and Docker-secret references follow the same versioned naming rule.

The loader checks descriptor/path identity before/after bounded reads and rechecks metadata after loading the credential. A loaded server-only snapshot carries private file signatures and a checksum of the **validated non-secret metadata**, never a plaintext-key hash.

Callers must use `ProviderConfigurationStore.unchanged(snapshot)` before/after external I/O and retain the observed alias/config versions. File timestamps alone are insufficient: a same-length rewrite can retain identical filesystem timestamps. The recheck also compares current/observed credential values in constant time **in server memory**, without logging, serializing or hashing the credential. A changed/unsafe/unreadable snapshot is not current.

This does not make privileged operator filesystem edits into audited HTTP mutations. Managed key import, active-config persistence and job/version coordination remain subsequent implementation work.

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
