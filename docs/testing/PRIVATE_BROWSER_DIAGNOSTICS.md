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
