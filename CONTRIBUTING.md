# Contributing

Use English for source comments, documentation, and Conventional Commit messages. The user interface defaults to Traditional Chinese.

## Development workflow

1. Create a focused branch from the default branch.
2. Install locked dependencies with `uv sync --locked` and `npm ci`.
3. Write a regression test before changing financial behavior.
4. Run `make check` and review `git diff --check`.
5. Commit a small, independently verified change, for example `fix(ledger): reject backdated overselling`.

## Financial invariants

- Use Python `Decimal`, scaled SQLite integers, and JSON decimal strings. Never introduce financial binary floats.
- Do not change authoritative golden fixture answers to make an implementation pass.
- Financial writes, cash reversals, audit events, idempotency results, and projection invalidations must commit atomically.
- External network calls do not belong inside a database transaction.
- A missing quote or exchange rate is unknown, never zero or an assumed conversion rate.
- Evidence, accepted revisions, and audit are append-only. Reconciliation must not overwrite actual receipts.

## Contracts and migrations

Generate executable OpenAPI and TypeScript with `npm run generate:contracts`. Commit the generated artifacts alongside the implementation. Do not hand-edit generated TypeScript. Preserve the reference contract and source fixture baselines.

Use Alembic for schema changes. Include empty-database upgrade, repeat-upgrade, integrity, foreign-key, and rollback/failure tests. Never call `create_all` at application startup.

## Private material

Never commit credentials, `.env`, database files, backups, brokerage data, `dev/`, or local development/assistant notes. Use synthetic data for tests. Provider live tests need explicit permission and valid licensing; absent credentials must be reported as not tested.

## Dependency policy

Lock all dependencies. The root npm override pins `minimatch` to 11.0.4 to address GHSA-g3hv-75wg-m249 in lint/contract tooling; Node 24 supports this version. Remove the override when upstream dependency ranges include the fix, after rerunning lint and contract generation.

## Reporting security issues

Do not post credentials or private financial records in public issues. See [SECURITY.md](SECURITY.md).
