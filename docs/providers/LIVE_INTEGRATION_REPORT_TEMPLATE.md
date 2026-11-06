# Provider Live Integration Report Template

This is a reusable operator acceptance template, not an executed report or coverage claim. Copy it to a private location such as `.local/provider-reports/`. Never commit a completed report containing real account/security selections, credentials, licensed data or private evidence.

Without valid credentials, lawful plan coverage and explicit permission for live testing, keep every applicable live verdict **NOT_TESTED**. A passing mocked-HTTP test, resolved alias, HTTP 200, installed SDK or candidate market list is not live coverage or financial verification.

## Run and authorization

| Field | Recorded value |
| --- | --- |
| Provider / adapter version / application commit | NOT_TESTED |
| Operator authorization reference and UTC time | NOT_TESTED |
| Plan name, historical limits and permitted use | NOT_TESTED |
| Terms/data-rights reference and review date | NOT_TESTED |
| Server-side profile identifier, never the key/value | NOT_TESTED |
| Permitted normalized fields, retention and evidence use | NOT_TESTED |
| Raw-payload retention permission | Disabled unless separately documented and protected |
| Request, per-minute and daily budget approved for this run | NOT_TESTED |
| UTC start/end / maximum attempts / timeout policy | NOT_TESTED |
| Official reference/source documents used for comparison | NOT_TESTED |

Do not paste tokens, cookies, passwords, complete credential-bearing URLs, raw payloads or private portfolio contents into the report or logs. Source pointers must omit the complete query. Respect provider rate limits, Retry-After, licenses and website restrictions; do not bypass anti-automation measures. HKEX/SEC evidence is manual/legal evidence unless a separately authorized official interface exists.

## Provider-level status

| Provider | Origin group | Runtime configuration | Live/plan verdict |
| --- | --- | --- | --- |
| eodhd | EODHD | UNCONFIGURED until explicit server setup | NOT_TESTED |
| alphavantage | ALPHAVANTAGE | UNCONFIGURED until explicit server setup | NOT_TESTED |
| massive | MASSIVE | UNCONFIGURED until explicit server setup | NOT_TESTED |
| yahoo | YAHOO | Disabled until explicit terms/use acknowledgment | NOT_TESTED |
| yfinance | YAHOO | Optional SDK; not an independent source | NOT_TESTED |
| hkex_evidence | HKEX_OFFICIAL | Manual/legal evidence only | NOT_TESTED |
| sec_evidence | SEC_OFFICIAL | Manual/legal evidence only | NOT_TESTED |
| manual | USER_VERIFIED | Explicit owner evidence, not another vendor vote | NOT_TESTED |

## Representative sample

Use at least **20 explicitly selected securities**, at least **10 HK and 10 US**, with at least **50% uncommon identifiers in each market**. Record why an identifier is uncommon. Include leading-zero/dotted/renamed symbols, small-cap, suspended/delisted, ETF/REIT, dual-currency counters, ADR/OTC, special dividends and rational/reverse split examples where lawful coverage permits. These categories are selection requirements, not claims that a provider supports them.

Each row needs an explicit security ID, exchange and effective provider-alias window. Do not strip leading zeros/dots or infer a current alias for an earlier identity period. Use separate evidence when event currency differs from quote currency.

| Sample | Market | Security ID / exchange / identifier | Uncommon category and alias period | Live verdict |
| --- | --- | --- | --- | --- |
| HK-01 | HK | To select | To record | NOT_TESTED |
| HK-02 | HK | To select | To record | NOT_TESTED |
| HK-03 | HK | To select | To record | NOT_TESTED |
| HK-04 | HK | To select | To record | NOT_TESTED |
| HK-05 | HK | To select | To record | NOT_TESTED |
| HK-06 | HK | To select | To record | NOT_TESTED |
| HK-07 | HK | To select | To record | NOT_TESTED |
| HK-08 | HK | To select | To record | NOT_TESTED |
| HK-09 | HK | To select | To record | NOT_TESTED |
| HK-10 | HK | To select | To record | NOT_TESTED |
| US-01 | US | To select | To record | NOT_TESTED |
| US-02 | US | To select | To record | NOT_TESTED |
| US-03 | US | To select | To record | NOT_TESTED |
| US-04 | US | To select | To record | NOT_TESTED |
| US-05 | US | To select | To record | NOT_TESTED |
| US-06 | US | To select | To record | NOT_TESTED |
| US-07 | US | To select | To record | NOT_TESTED |
| US-08 | US | To select | To record | NOT_TESTED |
| US-09 | US | To select | To record | NOT_TESTED |
| US-10 | US | To select | To record | NOT_TESTED |

## Per-security / per-provider evidence

Repeat this block for each tested provider/sample/window. Use PASS, FAIL, PARTIAL or NOT_TESTED only with corresponding evidence; unsupported/permission/error/no-data states remain distinct.

| Required check | Verdict / evidence reference |
| --- | --- |
| `symbol_resolved`: exact security/alias ID/version and complete effective window | NOT_TESTED |
| `ex_dates_covered`: requested window, observed event range and any plan gaps | NOT_TESTED |
| `dps_correct`: exact decimal strings, event type and authoritative comparison | NOT_TESTED |
| `pay_date_available`: known date or explicit null, never today by default | NOT_TESTED |
| `currency_correct`: dividend currency, never inferred from quote currency | NOT_TESTED |
| `unadjusted_correct`: documented share basis, reference date and complete adjustment evidence | NOT_TESTED |
| `last_success`: actual sanitized run/request metadata and UTC timestamps | NOT_TESTED |
| `license_ok`: plan/data rights and retention reviewed for this use | NOT_TESTED |
| Pagination: all declared pages visited, no loop or silent first-page truncation | NOT_TESTED |
| Per-page decoded hash / normalized hash / safe source pointer | NOT_TESTED |
| 429/Retry-After/timeout/error outcomes and preserved existing financial facts | NOT_TESTED |
| Independent origin groups, excluding duplicate Yahoo/yfinance votes | NOT_TESTED |
| Review reasons, date mismatches, special/regular separation or unresolved basis | NOT_TESTED |

EODHD documents historical per-share cash and a current-share-adjusted value. That alone does not establish declaration/ex-date terms or an owner's receipt. Preserve uncertainty and request authoritative basis evidence rather than marking income VERIFIED or cash RECEIVED. Provider split factors can include other adjustments and must not automatically adopt ledger corporate-action terms.

## Outcome and remaining work

- Evidence-backed tested markets/windows: **NOT_TESTED**.
- Unconfigured, unauthorized, unsupported, partial, stale, empty and failed scopes: **NOT_TESTED**.
- Outstanding adjustment/currency/date/identity/licensing questions: **NOT_TESTED**.
- Approved budget consumed and remaining: **NOT_TESTED**.
- Ledger/receipt preservation evidence and unresolved reviews: **NOT_TESTED**.
- Operator acceptance with its limited scope, or reason acceptance is refused: **NOT_TESTED**.

Do not generalize a sample's result to all HK/US securities or all historical windows. Missing rows do not prove zero dividends. A successful fetch does not verify terms, create a receipt or establish a complete valuation.
