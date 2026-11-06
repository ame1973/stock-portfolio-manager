# Source workspace

The authenticated browser workspace is at `/data-center`. The desktop sidebar
has a **Data center** destination. On phones/tablets, use **Settings → Data center
and source fetch**; the bottom navigation retains five destinations. UI labels
default to Traditional Chinese. These pages expose real source APIs, not financial
reconciliation, provider billing or authoritative market coverage.

## Read configuration before fetching

`/data-center/providers` lists the eight candidate profiles and their current
local file/dependency diagnosis. It does not probe an upstream or enable traffic.
A configured profile remains unprobed/STALE; an unavailable profile remains
UNCONFIGURED, including when older successful source pages exist. Expand a
provider's diagnosis to read its recorded page separately. Yahoo/yfinance share
one YAHOO origin and budget, not two vendor votes. Rights remain unverified;
manual/official references do not add independent corroboration.

Key import/settings and schedule editing are not available yet. Manage the
existing operator files according to [configuration](CONFIGURATION.md). Merely
viewing this page or possessing a key never enables a provider.

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

All private responses are no-store. Filters/search/cursor stacks live only in the
authenticated memory context, with distinct observation modes/job scopes. Reload,
logout or expiry clears them; no private filters are automatically added to the
browser URL and no local/session/IndexedDB/Service Worker storage is used. Source
writes invalidate source queries only, never pretend that financial projections
were recomputed. Encrypted opt-in offline support remains unfinished.

Paid/live-provider, target-NAS and production acceptance remain NOT_TESTED.
