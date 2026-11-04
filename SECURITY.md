# Security Policy

The project is under active development and has not yet reached a production release. Do not use development snapshots for real financial records.

## Reporting a vulnerability

Use the repository hosting platform's private vulnerability reporting feature if enabled. If it is unavailable, contact the repository maintainer privately before publishing exploit details. Do not attach real holdings, account records, tokens, database files, or backups to a public report.

Include the affected commit, a minimal synthetic reproduction, the expected behavior, and the observed impact. Never test against someone else's installation without authorization.

## Deployment boundary

Run on a trusted local filesystem behind a trusted HTTPS reverse proxy, preferably restricted to a LAN or VPN. Provider outages and incomplete market data are distinct from ledger errors. Application licensing does not grant rights to redistribute third-party market data.
