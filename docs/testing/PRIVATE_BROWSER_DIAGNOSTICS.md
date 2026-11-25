# Private browser failure diagnostics

Run browser tests only against fresh, isolated synthetic data. This guard is not
permission to test real accounts, live provider credentials or a production NAS.
The application's no-store, authentication, provider-rights and secret-storage
boundaries still apply independently.

## Run the privacy gate

```sh
npx playwright install chromium
npm run test:e2e:privacy
```

The gate starts its own loopback-only in-memory fixture server; it needs no Docker
stack, account, database, provider key or bootstrap operation. It generates four
synthetic private markers only in process memory: bootstrap, password, session
and provider. No marker is passed in command arguments, environment variables or
fixture source files.

A deliberately unprotected control proves that ordinary assertion/call-log
failures would expose all four markers. The protected run executes two genuine
failures: an assertion containing the private values and a timed-out fill whose
argument is private. Both must retain their real failure exit status. Captured
stdout/stderr and **retained** error-context files must contain none of the
markers; those contexts must contain neither a page snapshot nor a source frame.
Unexpected trace/image/video artifacts fail that protected check. Reporter/trace
override cases must stop before the fixture server delivers private data.
All subprocess output and temporary files, including the unsafe control, stay
inside the checking process and are inspected/removed without being printed.
Only a fixed safe summary reaches the terminal.

## Normal application E2E policy

- Import `test` from `tests/e2e/private-test.ts`, not directly from Playwright.
  Lint enforces that import for application spec files. Shared helpers may use
  Playwright types, expectations and named steps inside the protected test.
- Use only fixed, non-secret test titles, step labels and file names. Never put a
  password, token, private URL, request body or credential in those metadata fields.
- The serial application's password is generated in worker memory, not embedded
  as a source constant. Bootstrap output remains captured directly in memory.
  Always use a fresh Compose project for another complete onboarding run.
- Traces, automatic screenshots and videos remain off. `PWDEBUG`, `DEBUG` and
  `NODE_DEBUG` must be unset. Overrides fail closed before credential-bearing test
  code runs; do not bypass this check to debug a private flow.
- A dependency-ordered automatic fixture replaces error messages, stacks,
  snippets, matcher snapshots, values and causes **before** page/context artifact
  teardown. Error status, exit code and application assertions are not changed.
- The private reporter prints only status, test-file location and final counts.
  It does not forward worker stdout/stderr, titles, steps, errors, source snippets
  or attachments. Failure detail is intentionally sacrificed rather than trying
  to guess which arbitrary string is a secret. Use the file/line and synthetic
  unit/API diagnostics to reproduce a failure safely.
- Normal generated output is not preserved. The privacy gate deliberately
  retains its synthetic failure files until inspection, proving that deletion is
  not the protection mechanism.
- Existing explicit screenshots are taken only on safe, post-consumption views.
  Never manually capture a filled credential form; capture before filling or mask
  every sensitive control. Do not add JSON/HTML reporters or artifact upload steps
  for private flows without extending and rerunning this gate.

## Pinned Playwright boundary

The guard is verified against the locked **Playwright 1.64.1** implementation.
That version consults `PLAYWRIGHT_NO_COPY_PROMPT` before collecting the automatic
failure DOM snapshot. Configuration sets it before workers start. It does **not**
by itself suppress error messages, source frames or generated Markdown: the
fixture redaction and reporter policy are also required. No dependency file is
patched, and no assertion, login budget, application timeout or TLS/proxy trust is
weakened.

This environment behavior is an implementation detail, not a version-independent
public API guarantee. The fixture rejects another installed Playwright version.
Before upgrading, inspect the installed artifact-recorder/error-context behavior,
reverify teardown ordering and rerun the unsafe/protected/override controls plus
the full application E2E suite. The upstream
[secrets guidance](https://playwright.dev/docs/ci-intro#properly-handling-secrets)
warns that traces, reports and console output can contain credentials.

CI defines this privacy gate after Chromium installation and before application
browser tests. A local pass is not proof that hosted CI ran or that live/NAS
acceptance is complete.

## Separate real credential-workflow fixture

```sh
npm run build
npm run test:e2e:credentials
```

This second command is **not** the intentional-failure privacy gate. It starts
the real FastAPI application through Uvicorn, serves the production-built SPA,
and drives actual Chromium onboarding, password confirmation, disabled policy
creation, exact write-only import, explicit enablement, rotation back to disabled,
reference clear and original replay after clear. API results are not mocked. A
real committed response is temporarily held to verify pending-close protection.
The test checks exact original/current versions, two real mode0600 editions in a
mode0700 private directory, no key/token/password in database rows, zero provider
work and unchanged financial/source facts. No key enters process arguments or
environment; bootstrap output is privately captured in the protected test worker.

A stdlib wrapper allocates a new private temporary root and unused loopback port;
the server rejects reused directories, non-test configuration and nonmatching
private paths/origin. Data, backups, operator and managed directories are separate.
No Docker deployment volume, existing owner, real key, provider request or worker
is used. The temporary synthetic tree is inspected and removed after the server
stops. This source-workspace fixture is not a container/NAS/TLS/OS-network-isolation
or successful provider-execution claim. The separate seven-test Compose suite
continues to verify the real Nginx/API/worker and strict CSP with empty deployment
credential vaults; its settings step covers frozen policy editors, genuine CAS
conflict, current password confirmation, pending writes and actual25+2 history.

The same pinned private fixture/reporter and no-recording/debug rules apply.
Subprocess diagnostics are captured, not published; the wrapper emits only a
fixed result. CI defines this gate after the privacy control and built-SPA checks;
that definition is not evidence that hosted CI ran.

## Separate trade-origin recovery fixture

```sh
npm run build
npm run test:e2e:trade-origin
```

This isolated real-browser flow uses the same private temporary server and
redaction policy, with a newly initialized synthetic owner and database. It adds
new synthetic legacy trades and matching audits without removing audit guards or
modifying existing audit events. The workflow verifies actual password confirmation,
a committed response dropped in transit, unchanged-key replay and exact raw JSON
integer digits at `2^53+1` and `INT64_MAX`. Its wrapper prints only a fixed result
and, on failure, an allowlisted non-secret stage label. It does not print request
bodies, database contents, token, password or subprocess errors.

Explicitly setting `SPM_E2E_SAFE_SCREENSHOT=1` captures only the synthetic recovery
sheet before its password, JSON and reason are filled, at three viewport widths.
The ignored `.local/screenshots/` files are not automatic failure artifacts. Do not
capture filled private forms. This fixture does not test provider traffic, a
container, NAS, TLS or external ownership.

## Separate unverified trade-history review fixture

```sh
npm run build
npm run test:e2e:trade-review
```

This separate private temporary application exercises actual review, withdrawal,
password confirmation and a committed-but-lost response followed by unchanged-key,
zero-write replay. It also verifies withdrawal after retained-origin recovery
changes the evidence. The fixture adds new synthetic legacy facts without changing
existing audits or guards, and checks financial and historical FX preservation.
It uses the same pinned reporter, restricted failure stages and private cleanup,
with no deployment volume, provider traffic or worker.

Optional `SPM_E2E_SAFE_SCREENSHOT=1` captures only the empty synthetic review sheet
at three widths under ignored `.local/screenshots/`; it is not a failure artifact.
Never capture filled private forms. This is not a container, NAS, TLS or external
ownership test.

## Separate protected receipt create-and-void browser fixture

```sh
npm run build
npm run test:e2e:receipt-write
```

This fixture starts the real FastAPI/Uvicorn application and a built SPA in Chromium against a
fresh synthetic owner and database. It seeds explicitly labelled legacy entitlements and receipt
history only in that isolated fixture, then exercises the protected receipt-input and create
APIs with actual session, CSRF, password reauthentication and no-store responses. Concurrent
identical requests produce one recorded cash entry; a second committed response is deliberately
dropped and its original response replays without writes. A new receipt with stale scope is
rejected. A real-socket reconciliation read also checks the explicit no-projection state,
active/void and unverified-origin counts, a missing cash link, `no-store` and unchanged synthetic
database tables. This is an API request from Chromium's request context, not a reconciliation
browser UI or proof of payment.

The form workflow explicitly selects an old basis, checks keyboard focus, pending close guards
and 360/768/1440 layouts, and reauthenticates through the real sheet. It drops a committed
response, verifies an equivalent-decimal same-key replay without writes, then verifies that a
changed uncertain intent keeps the receipt UUID and receives a conflict instead of a second
posting. It also checks that an HKD override with a different amount is blocked and an exactly
equal override records the explicit reason. The test compares synthetic database tables and
checks empty browser storage and Cache Storage. Bootstrap values, request data and database
contents remain private under the same reporter and cleanup rules; no screenshots are taken.

The fixture also obtains current void inputs and calls the protected void API over the real
socket. Concurrent identical requests create one reversal with unchanged original amount and
locked FX; another committed void response is deliberately dropped and replayed without further
writes. The browser then explicitly selects a legacy receipt and confirms its missing origin
blocks void, selects a matched receipt, checks responsive layout and keyboard focus, reauthenticates
and records a void through the real form. A held, committed response is dropped; the unchanged
key replays without another write. A separate account-version race rejects a stale form without
additional writes. Synthetic database-table comparisons verify the expected audit, idempotency,
reversal and invalidation changes while preserving unrelated data.

This is not payment or eligibility verification, nor a container, worker, live-provider, NAS
or TLS acceptance claim. The application still cannot create the legacy entitlement prerequisite
on a fresh installation.

## Separate direct recorded-cash browser fixture

```sh
npm run build
npm run test:e2e:direct-receipt-write
```

This fixture creates a fresh owner through real Chromium onboarding and reauthentication,
then uses authenticated requests against the actual Uvicorn API. No entitlement, projection,
receipt or financial seed is inserted: the protected direct-create API records explicit cash
in a new-install scope. Concurrent identical requests create one receipt/cash/origin/HTTP
intent, and equivalent-decimal same-key replay writes nothing. A second committed response
is deliberately dropped, then replayed with its unchanged body, UUID and key. Stale data pins
and event rejection after financial history remain fenced.

Full-table comparisons check the expected atomic audit, cash, origin, idempotency and
invalidation changes while legacy entitlements/receipts, projections and provider state stay
unchanged. The built SPA then reads both newly recorded direct receipts at 360/768/1440,
checks keyboard account selection, no-store and unverified labels, and offers no legacy void
action for direct records. Authenticated socket reads of both direct void-input snapshots check
matching current/original pins, exact cash/FX, null versus zero, original cash IDs and no-grant
labels without changing any tables. Missing IDs return 404; legacy-only void inputs remain
fenced rather than adopting direct receipts. The same real-socket fixture then concurrently
voids the first direct receipt once, rejects a stale second correction, deliberately drops the
second committed void response and recovers it with an unchanged body/version/key. It checks
exact original/reversal integers in a guarded read-only synthetic SQLite connection, expected
atomic table changes and zero-write replay.

The same built SPA then opens the actual direct-entry form after explicit account selection and
a fresh no-store input read. It checks initially blank currency/amount, no fabricated entitlement,
keyboard selection, close-button focus and 360/768/1440 layouts, then uses the real password
form. It records HKD identity cash with an explicit actual-HKD value/reason, unknown tax, zero
fee and separate no-grant/event-fence confirmations. A committed response is held while close
and Escape are blocked, then dropped. Equivalent-decimal retry keeps the receipt UUID and key,
returns the original response and leaves every table unchanged. A second form freezes fresh
inputs before a protected account edit; its stale submission is rejected with retained input
and no additional writes. Browser storage stays empty.

The actual direct-void form then reads a separately selected receipt's fresh no-store original
cash and current pins, checks empty reason/unselected acknowledgment, keyboard/focus and all
three widths, and uses its real password form. A protected account edit makes that frozen
submission stale: the 409 preserves its reason and changes no tables. After explicit reopening
with fresh pins, the response is held to verify busy-close, committed and dropped, then replayed
with an identical body, receipt version and key. The browser verifies one reversal/three audits,
unchanged retained origins and unrelated tables, and zero-write replay. A guarded read-only
SQLite check verifies exact original/opposite HKD identity integers and `VOID`/version 2; the
history then disables correction of that receipt. No filled form is captured.

This exercises direct entry/void forms, protected create/void APIs, history UI and private input
reads, not external payment, eligibility, deployment, NAS or TLS. The pinned private reporter,
exclusive temporary root,
no-debug/no-recording rules and fixed safe output remain mandatory. CI defines this fixture;
that definition is not evidence of a hosted CI run.

## Separate recorded-projection and comparison browser fixture

```sh
npm run build
npm run test:e2e:projection-write
```

The isolated actual-app fixture records synthetic holdings, estimate settings and projections,
then opens the receipt-account comparison in the built SPA. It checks an actual no-store API
response, the exact recorded projected amount with no active receipt, unavailable amounts rather
than zero, explicit no-grant copy, keyboard expansion and collapse, 360/768/1440 geometry and
unchanged synthetic database tables during reads. At the desktop breakpoint the fixture reopens
the event, account and comparison because the responsive table remounts the card. This scenario
does not create a receipt or prove a cross-revision cash reconciliation; dedicated backend tests
cover synthetic recorded receipts.
No screenshots, deployment volumes, worker or provider traffic are used.

## Separate cross-store recorded comparison fixture

```sh
npm run build
npm run test:e2e:reconciliation-direct
```

This isolated actual-app fixture creates an event successor before any financial history, then
records a synthetic projection through the protected API. A guarded test-only seeder adds one
legacy receipt on the earlier revision and one direct receipt on the current revision in an
exclusive temporary database. The seeder checks its environment, root, schema, named synthetic
scope, projection and empty receipt stores; a repeated seed is rejected without changing data.
The authenticated Chromium request context also reads two no-store combined-history pages over
the real socket, checking distinct direct/legacy basis, exact values, original revisions and
unverified labels without database changes. Another authenticated request reads the direct
receipt-input snapshot in the same mixed scope and checks exact version strings, explicit
no-grant/rejection labels and absence of entitlement or default cash fields. That input check
is an API request, not a direct cash-entry form. Separately, the built SPA selects the account by
keyboard and displays both records with their distinct basis, original revisions and audit
labels. Direct records never open a legacy void form, and visible mixed history disables legacy
void selection. Real-socket requests still verify that legacy-only history and void inputs fail
closed for this mixed scope.

The authenticated Chromium request context also requests the hypothetical revision preview over
the real socket. It supplies both current accounts and explicit estimates, reads two no-store pages
with the same session-bound token, and checks the hypothetical USD 12 against the retained USD 11
projection and USD 8 receipts: delta USD 1 and recorded difference USD 4. Missing HKD FX stays
explicit; the empty account is not called confirmed zero. Changing the proposed DPS while continuing
the page returns 409 without database changes. This tests the API, not a preview form or accepted
revision.

The browser also verifies an independent comparison of 11 expected, 8 recorded and 3 difference,
with unverified labels and
the event-rejection fence intact. It covers keyboard access, 360/768/1440, unchanged synthetic
tables and empty browser storage. This is not a direct receipt writer, a revision after financial
history, proof of payment or eligibility, or deployment/provider acceptance. No screenshots,
worker, provider traffic or deployment data are used.
