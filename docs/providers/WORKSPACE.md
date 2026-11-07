# Source workspace

The authenticated browser workspace is at `/data-center`. The desktop sidebar
has a **Data center** destination. On phones/tablets, use **Settings → Data center
and source fetch**; the bottom navigation retains five destinations. UI labels
default to Traditional Chinese. These pages expose real source APIs, not financial
reconciliation, provider billing or authoritative market coverage.

## Read configuration before fetching

`/data-center/providers` lists the eight candidate profiles and their current
owner-scoped policy/private-file/dependency diagnosis. It does not probe an
upstream or enable traffic. A configured profile remains unprobed/STALE; an
unavailable profile remains UNCONFIGURED, including when older successful source
pages exist. Expand a provider's diagnosis to read its recorded page separately.
Yahoo/yfinance share one YAHOO origin and budget, not two vendor votes. Rights
remain unverified; manual/official references do not add independent corroboration.

Follow **Source authorization and keys** to the separate
[owned settings workspace](PROFILES.md#browser-workspace) for disabled creation,
password confirmation, full policy edits, write-only key import/rotation and
immutable history. The latest owned decision, even disabled/cleared/unavailable,
overrides operator fallback. The independent read-only CLI still inspects
[operator configuration](CONFIGURATION.md), not owner DB policy. Merely viewing
either page or possessing a key never enables a provider. Explicit source schedule
editing is available under **Source schedules**, not a credential/rights grant.

`/data-center/coverage` requires an explicit stable security selection before
loading bounded diagnostics. Select dividends or provider-only splits separately.
The displayed source symbol is historical, not guessed from today's display
ticker. Unique retained assertions, latest recorded state and current local
configuration are separate. Both coverage dates and provider billing balance
remain **unknown**, even after source/job success.

## Read retained observations

- `/data-center/dividends`: exact nullable eight-place DPS, event currency and
  dates, unresolved basis, first run/time/hash and repeated sightings.
- `/data-center/splits`: exact rational strings, including every INT64 digit;
  always PROVIDER_ADJUSTMENT_ONLY and never adopted shares/cost/cash.

Filters apply to event dates, not retrieval time. An explicit ex-date bound
excludes unknown ex-dates without assigning the retrieval date. Search and keyset
pages remain server-owned; observations are ordered by first retrieval time/ID
descending, not rearranged by later sightings. Desktop tables retain server rows
and expose one expanded detail; phone/tablet cards retain the same exact strings.
Missing values are not zero, and quote currency does not fill dividend currency.

The page reports omitted unresolved legacy rows; an empty v1 page does not prove
that no dividend or split exists. Repeated assertions retain the first identity,
hash and time, not additional income. Typed document citations remain unverified
user assertions. Query/fragment document links are withheld explicitly without
rewriting the original assertion/hash. Links open only on explicit user action;
the application does not fetch/verify those documents. Shared link rendering also
requires an absolute HTTP(S) URL of at most 2048 characters without embedded
credentials; malformed or non-web references remain inert without echoing them.
Integrity failures show a safe error rather than repairing, rounding or silently
dropping unsafe evidence.
See [observation reads](OBSERVATIONS.md) for the precise contract.

## Quote one source assertion

On `/data-center/dividends`, choose **Quote dividend source terms**
(`引用股息來源條款`). On `/data-center/splits`, choose **Quote split source factor**
(`引用拆股來源因子`). The existing responsive Sheet appends a quotation through the
[protected cited source API](CITED_ASSERTIONS.md), not a canonical dividend/action.

1. Search, select and explicitly add one stable security. Candidate searches do
   not replace an already-added identity. Select HKEX evidence, SEC evidence or
   manual input; no source is assumed and network vendors cannot be impersonated.
2. Supply the known event-local ex-date/effective date and exact dated source
   symbol. An optional bounded saved-alias reference offers explicit selection,
   not automatic ticker inference. Changing confirmed security, source or event
   date clears the old symbol. Reference search/refresh is not an input change.
3. Quote exact terms. Blank DPS means unknown; `0` is known zero. Event currency
   stays unknown unless explicitly chosen, even for a USD quote or an independent
   EUR source label. Optional dates remain blank, UNKNOWN basis/type remains
   unresolved, and frequency/period never implies REGULAR. Split numerator and
   denominator are positive INT64 strings without a guessed 1:1. AS_DECLARED is
   only the owner's quotation, not verification.
4. Provide the reference, explanation and optional document link. Official
   profiles require their exact HTTPS host/default port; manual references can be
   local without a URL. Attribution is not a document download/content check.
   Query/fragment links are withheld on reads without changing the original hash.
5. Acknowledge source-only/non-adoption and choose **Save quotation, do not adopt
   finances** (`儲存引用，不採納財務`). Existing source access/rights is required for a
   new quotation, but no API key, request quota or upstream call is involved. The
   separate policy/alias links open another tab; they do not grant access.

Close/Escape cannot discard a pending write. Failed input and an unchanged intent
key remain in the open form. If a response is lost after commit, retry unchanged:
current diagnostic/alias read failure, later policy disablement or alias correction
must not prevent the server's original response. Candidate searches, acknowledgment
and selecting the same source symbol do not create another intent. Changing actual
quoted fields does; first resolve an uncertain result rather than intentionally
creating a second quotation. Current session and CSRF remain required. A cancelled
sheet has no persistent draft or recoverable intent key.

A confirmed response may be the original replay, not a new/current version. Its
notice retains citation/observation IDs and asks for matching-filter review; the
form does not clear private list filters or insert a fabricated current row. Only
source reads invalidate, never financial projections. No quotation verifies a
document, corroborates another vendor, creates a receipt or adopts income/shares.

## Queue one explicit source intent

At `/data-center/jobs`, choose **Create source fetch job**:

1. Search the security catalogue, select a stable identity and press **Add
   security**. Only explicitly added identities are included (maximum 100).
   Delisted identities remain eligible for historical source retrieval. Candidate
   search changes do not remove already-added securities.
2. Supply both inclusive calendar dates. Neither date nor a whole-market scope
   is guessed.
3. Explicitly select network profiles. None is preselected; a current local
   UNCONFIGURED result is visible and selection does not enable it. HKEX, SEC and
   manual profiles require cited human input and are not fabricated by this form.
4. Dividends are selected by default. Provider-only split factors require an
   explicit checkbox. At least one operation is required; `force` stays false.
5. Acknowledge that this is source observation capture, not verification, income
   recognition or receipt confirmation, then submit.

HTTP 202 means **accepted source intent**, not successful retrieval. The sheet
remains busy until the actual response and cannot be dismissed with Close/Escape
while a submission is pending. Failed submissions retain input and the unchanged
intent key. Candidate searches and the client-only acknowledgment do not create
new public request bodies/keys. Changing a requested scope creates a new intent.
Do not deliberately create another job to work around an uncertain response.

## Inspect progress and request cancellation

Open an accepted job at `/data-center/jobs/{id}`. Lists and item pages are bounded;
follow previous/next, not a guessed total page count. Job order is creation time/ID
descending; item order is ordinal/ID ascending. Textual counts distinguish
processed securities (including failures/skips), completed work items, committed
source pages and new assertions. None represents verified dividends or income.
Four logical attempts include the initial attempt; paging and lease ownership are
not new rounds. Reserved/recorded HTTP counts include uncertain spending, not
confirmed upstream billing.

Active states refresh automatically. RETRYING and terminal states do not
continuously poll; read the preserved absolute UTC deadline and use **Reload job
status**. Very long Retry-After/quota/circuit waits are not shortened. A changed
job version fetches current item pages even when active polling stops.

**Request cancellation** opens a confirmation. This is a stop intent without
If-Match, not a financial revision. CANCEL_REQUESTED means cancellation is not yet
acknowledged; only the returned/current state may say CANCELLED. A race with a
completed job shows its real terminal result, not invented cancellation success.
Already saved source evidence and uncertain upstream spending remain; no ledger
fact, receipt, locked FX, financial revision or quota is reset. See
[job control](JOBS.md) for replay, ownership and worker guarantees.

## Create and explicitly enable a source schedule

At `/data-center/schedules`, choose **Create disabled source schedule**:

1. Enter a name and an explicit HK/US market. Exchange catalogue identity, not a
   USD/HKD quote, determines the market. Changing market clears confirmed security
   selection; candidate search alone does not remove already-added IDs.
2. Add the stable securities, select network providers and operations. Dividends
   alone default on; no provider or whole-market scope is assumed. Unconfigured
   profiles remain visible and may later skip without spending requests.
3. Supply the earliest market date and explicit **16:00–23:59** local minute. HK
   uses Asia/Hong_Kong, US America/New_York. The server applies historical DST;
   this is not a NAS-time or verified closing-auction calculation.
4. Choose DAILY (including weekends), literal WEEKDAYS or WEEKLY with an explicit
   weekday. These are local week patterns, **not a verified exchange calendar**.
5. Choose exactly one bounded recurring window: 1–3660 inclusive days or 1–120
   calendar months with same-day/month-end clipping. Calendar months are never
   silently converted to 30-day periods. Unit/cadence changes clear incompatible
   quantity/weekday inputs for deliberate review.
6. Optionally supply a first-backfill start no later than the earliest market
   date. It is consumed on first actual queue, not success; failure/cancellation
   does not automatically reissue it. Save the definition **disabled**.

Open **Edit, enable or pause schedule** to change future intent. Edits require a
specific reason and the version observed when the sheet opened; enabled intent
also requires a fresh source-only acknowledgment. The complete policy and quoted
If-Match are sent together. A concurrent owner or worker advance returns 412,
preserves local input and asks for cancel/reload/review rather than overwriting.
The consumed first-backfill date stays read-only and is retained in the body.
Pause is not cancellation of already queued work; follow its separate job link
when a stop intent is required. There is no delete or public run-now button.

The list filters saved enablement and uses bounded name/ID server pages. The detail
page `/data-center/schedules/{id}` shows current policy, actual receipt count,
explicit UTC next/progress pointers and immutable due-UTC/ID receipt pages. Each
receipt retains its original policy/window and QUEUED or SKIPPED_IN_FLIGHT state,
with a link to the queued/blocking source job. A requested window is not proven
coverage; a queue/skip/empty history is not provider success, zero dividends or
income. Source-only terms and unverified session-calendar scope remain visible.

Use **Reload schedule and evaluation history** to confirm current metadata; distant
schedules do not continuously poll. A refreshed version refetches its receipt
rows. Form sheets cannot close during an uncertain write and keep unchanged intent
keys across retry/candidate-search/acknowledgment changes. Only a confirmed response
shows saved state. No source mutation pretends to recalculate the financial ledger.
See [schedule timing, API and worker semantics](SCHEDULES.md).

All private responses are no-store. Filters/search/cursor stacks live only in the
authenticated memory context, with distinct observation modes/job scopes. Reload,
logout or expiry clears them; no private filters are automatically added to the
browser URL and no local/session/IndexedDB/Service Worker storage is used. Source
writes invalidate source queries only, never pretend that financial projections
were recomputed. Encrypted opt-in offline support remains unfinished.

Paid/live-provider, target-NAS and production acceptance remain NOT_TESTED.
