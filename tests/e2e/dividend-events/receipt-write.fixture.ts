import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, stage, type Event } from "./helpers";

type Schemas = components["schemas"];

test("protected receipt creation records one cash entry and replays the original response", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-receipt-write-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click(); await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  stage("MASTER");
  const create = async (path: string, data: object) => {
    const response = await page.request.post(`/api/v1/${path}`, { headers: await headers(page), data }); expect(response.status()).toBe(201);
    return await response.json() as { id: string };
  };
  const portfolio = await create("portfolios", { name: "Synthetic receipt read portfolio" });
  const broker = await create("brokers", { name: "Synthetic receipt read broker" });
  const account = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic receipt read account", default_currency: "USD" });
  const security = await create("securities", { exchange_code: "NASDAQ", display_ticker: "RECEIPTE2E", name: "Synthetic receipt read security", quote_currency: "USD" });
  const created = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "1", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: "Synthetic receipt event reference", source_url: null, reason: "Synthetic event decision for receipt history review.",
  } });
  expect(created.status()).toBe(201); const first = await created.json() as Event;
  const revised = await page.request.patch(`/api/v1/dividends/events/${first.id}`, { headers: await headers(page, first.version), data: {
    security_id: security.id, revision_id: first.current_revision_id, resolve_rejection_id: null,
    event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null, pay_date: null,
    dps: "1.1", currency: "USD", adjustment_basis: "AS_DECLARED", citation: "Synthetic later receipt event reference",
    source_url: null, reason: "Synthetic successor before seeding retained receipt history.", confirm_unlock: false, confirm_reopen: false,
  } });
  expect(revised.status()).toBe(200); const event = await revised.json() as Event;
  stage("SEED");
  execFileSync("uv", ["run", "python", "-I", "tests/e2e/dividend-events/seed_receipts.py", event.id, account.id, first.current_revision_id, event.current_revision_id], { stdio: "ignore" });
  const base = `/api/v1/dividends/events/${event.id}/accounts/${account.id}/receipts`;
  const inputsResponse = await page.request.get(`${base}/inputs`);
  expect(inputsResponse.status()).toBe(200); expect(inputsResponse.headers()["cache-control"]).toBe("no-store");
  const inputs = await inputsResponse.json() as Schemas["ReceiptInputs"];
  expect(inputs.scope).toBe("RECORDED_RECEIPT_INPUTS_ONLY"); expect(inputs.eligibility_scope).toBe("NOT_ESTABLISHED");
  const entitlement = inputs.entitlements.items.find(item => item.revision_id === first.current_revision_id)!;
  expect(entitlement.scope).toBe("LEGACY_ENTITLEMENT_NOT_VERIFIED");
  const body: Schemas["ReceiptCreate"] = {
    receipt_id: randomUUID(), event_id: inputs.event_id, revision_id: inputs.revision_id,
    entitlement_id: entitlement.id, account_id: inputs.account_id, portfolio_id: inputs.portfolio_id, broker_id: inputs.broker_id,
    account_version: inputs.account_version, portfolio_version: inputs.portfolio_version, broker_version: inputs.broker_version,
    expected_data_revision: inputs.portfolio_data_revision, received_date: "2026-11-04", received_amount: "6.125", currency: "USD",
    tax: null, fee: "0", fx_rate_hkd: "7.8", fx_source: "Synthetic historical FX", actual_hkd: null, actual_hkd_reason: null,
    note: "Synthetic private receipt note", reason: "Synthetic recorded receipt from an explicit source.", confirmation: "RECORDED_CASH_ONLY",
  };
  const writeHeaders = await headers(page, event.version); const before = database();
  stage("COMMIT");
  const [left, right] = await Promise.all([
    page.request.post("/api/v1/dividends/receipts", { headers: writeHeaders, data: body }),
    page.request.post("/api/v1/dividends/receipts", { headers: writeHeaders, data: body }),
  ]);
  expect(left.status()).toBe(201); expect(right.status()).toBe(201);
  expect(left.headers()["cache-control"]).toBe("no-store"); expect(right.headers()["cache-control"]).toBe("no-store");
  const result = await left.json() as Schemas["ReceiptCreated"];
  expect(await right.json()).toEqual(result);
  expect(result).toMatchObject({ id: body.receipt_id, original_revision_id: first.current_revision_id,
    observed_revision_id: event.current_revision_id, received_amount: "6.125000", received_hkd: "47.775000", tax: null,
    fee: "0.000000", fx_rate_hkd: "7.800000000000", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED" });
  const after = database();
  for (const table of ["dividend_receipts", "cash_ledger_entries", "dividend_receipt_origins", "projection_invalidations", "idempotency_keys"]) {
    expect(after[table]).toHaveLength(before[table]!.length + 1);
  }
  expect(after.audit_events).toHaveLength(before.audit_events!.length + 3);
  const changed = new Set(["dividend_receipts", "cash_ledger_entries", "dividend_receipt_origins", "projection_invalidations", "idempotency_keys", "audit_events", "portfolios"]);
  for (const table of Object.keys(before)) if (!changed.has(table)) expect(after[table]).toEqual(before[table]);
  stage("REAUTH");
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  const beforeReplay = database();
  stage("REPLAY"); const replay = await page.request.post("/api/v1/dividends/receipts", { headers: writeHeaders, data: body });
  expect(replay.status()).toBe(201); expect(await replay.json()).toEqual(result); expect(database()).toEqual(beforeReplay);
  const newHeaders = await headers(page, event.version); const beforeStale = database();
  const stale = await page.request.post("/api/v1/dividends/receipts", { headers: newHeaders, data: { ...body, receipt_id: randomUUID() } });
  expect(stale.status()).toBe(409); expect(stale.headers()["cache-control"]).toBe("no-store");
  expect((await stale.json() as { code: string }).code).toBe("DIVIDEND_RECEIPT_SCOPE_CHANGED"); expect(database()).toEqual(beforeStale);
  const refreshed = await page.request.get(`${base}/inputs`); expect(refreshed.status()).toBe(200);
  const latest = await refreshed.json() as Schemas["ReceiptInputs"];
  const lostBody = { ...body, receipt_id: randomUUID(), expected_data_revision: latest.portfolio_data_revision };
  const lostHeaders = await headers(page, event.version); let lostResult: Schemas["ReceiptCreated"] | null = null;
  const routePath = "**/api/v1/dividends/receipts";
  await page.route(routePath, async route => {
    const response = await route.fetch(); expect(response.status()).toBe(201);
    lostResult = await response.json() as Schemas["ReceiptCreated"]; await route.abort("failed");
  });
  stage("LOST"); const beforeLost = database();
  try {
    const lost = await page.evaluate(async ({ intent, csrf, key, version }) => {
      try {
        await fetch("/api/v1/dividends/receipts", { method: "POST", headers: { "Content-Type": "application/json",
          "X-CSRF-Token": csrf, "Idempotency-Key": key, "If-Match": `"${version}"` }, body: JSON.stringify(intent) });
        return false;
      } catch { return true; }
    }, { intent: lostBody, csrf: lostHeaders["X-CSRF-Token"], key: lostHeaders["Idempotency-Key"], version: event.version });
    expect(lost).toBe(true);
  } finally { await page.unroute(routePath); }
  expect(lostResult).not.toBeNull(); const afterLost = database();
  for (const table of ["dividend_receipts", "cash_ledger_entries", "dividend_receipt_origins", "projection_invalidations", "idempotency_keys"]) {
    expect(afterLost[table]).toHaveLength(beforeLost[table]!.length + 1);
  }
  expect(afterLost.audit_events).toHaveLength(beforeLost.audit_events!.length + 3);
  for (const table of Object.keys(beforeLost)) if (!changed.has(table)) expect(afterLost[table]).toEqual(beforeLost[table]);
  const recovered = await page.request.post("/api/v1/dividends/receipts", { headers: lostHeaders, data: lostBody });
  expect(recovered.status()).toBe(201); expect(await recovered.json()).toEqual(lostResult); expect(database()).toEqual(afterLost);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
