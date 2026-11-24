import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, stage, type Event } from "./helpers";

type Schemas = components["schemas"];
const path = "/api/v1/dividends/direct-receipts";

test("direct receipt API records explicit cash without a legacy entitlement", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-direct-receipt-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click(); await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  stage("MASTER");
  const create = async (resource: string, data: object) => {
    const response = await page.request.post(`/api/v1/${resource}`, { headers: await headers(page), data }); expect(response.status()).toBe(201);
    return await response.json() as { id: string };
  };
  const portfolio = await create("portfolios", { name: "Synthetic direct receipt portfolio" });
  const broker = await create("brokers", { name: "Synthetic direct receipt broker" });
  const account = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic direct receipt account", default_currency: "USD" });
  const security = await create("securities", { exchange_code: "NASDAQ", display_ticker: "DIRECTREC", name: "Synthetic direct receipt security", quote_currency: "USD" });
  const created = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "1", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: "Synthetic explicit direct receipt event", source_url: null, reason: "Synthetic event decision without a financial grant.",
  } });
  expect(created.status()).toBe(201); const event = await created.json() as Event;
  const base = `/api/v1/dividends/events/${event.id}/accounts/${account.id}/recorded-receipts`;
  const inputResponse = await page.request.get(`${base}/inputs`);
  expect(inputResponse.status()).toBe(200); expect(inputResponse.headers()["cache-control"]).toBe("no-store");
  const inputs = await inputResponse.json() as Schemas["DirectReceiptInputs"];
  const body: Schemas["DirectReceiptCreate"] = {
    receipt_id: randomUUID(), event_id: inputs.event_id, revision_id: inputs.revision_id, account_id: inputs.account_id,
    portfolio_id: inputs.portfolio_id, broker_id: inputs.broker_id, account_version: inputs.account_version,
    portfolio_version: inputs.portfolio_version, broker_version: inputs.broker_version, expected_data_revision: inputs.portfolio_data_revision,
    received_date: "2026-11-04", received_amount: "6.125", currency: "USD", tax: null, fee: "0", fx_rate_hkd: "7.8",
    fx_source: "Synthetic historical FX", actual_hkd: null, actual_hkd_reason: null, note: "Synthetic private direct receipt note",
    reason: "Synthetic explicit recorded cash without entitlement.", confirmation: "RECORDED_CASH_ONLY", confirm_revision_fence: true,
  };
  const writeHeaders = await headers(page, event.version); const before = database();
  for (const table of ["dividend_entitlements", "dividend_receipts", "dividend_direct_receipts", "dividend_projection_versions", "fx_rates", "jobs", "upstream_attempts"]) expect(before[table]).toEqual([]);
  stage("COMMIT");
  const [left, right] = await Promise.all([
    page.request.post(path, { headers: writeHeaders, data: body }),
    page.request.post(path, { headers: writeHeaders, data: body }),
  ]);
  expect(left.status()).toBe(201); expect(right.status()).toBe(201);
  expect(left.headers()["cache-control"]).toBe("no-store"); expect(right.headers()["cache-control"]).toBe("no-store");
  const result = await left.json() as Schemas["DirectReceiptCreated"]; expect(await right.json()).toEqual(result);
  expect(result).toMatchObject({ id: body.receipt_id, event_id: event.id, account_id: account.id, revision_id: event.current_revision_id,
    revision_no: event.version, receipt_basis: "DIRECT_EVENT_ACCOUNT", received_amount: "6.125000", received_hkd: "47.775000", tax: null,
    fee: "0.000000", fx_rate_hkd: "7.800000000000", actual_hkd: null, actual_hkd_reason: null,
    scope: "HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY", payment_scope: "OWNER_RECORDED_NOT_VERIFIED", eligibility_scope: "NOT_ESTABLISHED" });
  expect(result).not.toHaveProperty("entitlement_id");
  const after = database();
  const changed = new Set(["dividend_direct_receipts", "dividend_direct_receipt_origins", "cash_ledger_entries", "projection_invalidations", "idempotency_keys", "audit_events", "portfolios"]);
  for (const table of ["dividend_direct_receipts", "dividend_direct_receipt_origins", "cash_ledger_entries", "projection_invalidations", "idempotency_keys"]) {
    expect(after[table]).toHaveLength(before[table]!.length + 1);
  }
  expect(after.audit_events).toHaveLength(before.audit_events!.length + 3);
  for (const table of Object.keys(before)) if (!changed.has(table)) expect(after[table]).toEqual(before[table]);
  stage("REPLAY");
  const repeated = await page.request.post(path, { headers: writeHeaders, data: { ...body, received_amount: "006.1250", fx_rate_hkd: "07.800" } });
  expect(repeated.status()).toBe(201); expect(await repeated.json()).toEqual(result); expect(database()).toEqual(after);
  const stale = await page.request.post(path, { headers: await headers(page, event.version), data: { ...body, receipt_id: randomUUID() } });
  expect(stale.status()).toBe(409); expect(stale.headers()["cache-control"]).toBe("no-store");
  expect((await stale.json() as { code: string }).code).toBe("DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED"); expect(database()).toEqual(after);
  const fresh = await page.request.get(`${base}/inputs`); expect(fresh.status()).toBe(200);
  const latest = await fresh.json() as Schemas["DirectReceiptInputs"];
  const lostBody = { ...body, receipt_id: randomUUID(), expected_data_revision: latest.portfolio_data_revision };
  const lostHeaders = await headers(page, event.version); let lostResult: Schemas["DirectReceiptCreated"] | null = null;
  const routePath = "**/api/v1/dividends/direct-receipts";
  await page.route(routePath, async route => {
    const response = await route.fetch(); expect(response.status()).toBe(201);
    lostResult = await response.json() as Schemas["DirectReceiptCreated"]; await route.abort("failed");
  });
  stage("LOST");
  try {
    expect(await page.evaluate(async ({ intent, csrf, key, version }) => {
      try {
        await fetch("/api/v1/dividends/direct-receipts", { method: "POST", headers: { "Content-Type": "application/json",
          "X-CSRF-Token": csrf, "Idempotency-Key": key, "If-Match": `"${version}"` }, body: JSON.stringify(intent) });
        return false;
      } catch { return true; }
    }, { intent: lostBody, csrf: lostHeaders["X-CSRF-Token"], key: lostHeaders["Idempotency-Key"], version: event.version })).toBe(true);
  } finally { await page.unroute(routePath); }
  expect(lostResult).not.toBeNull(); const afterLost = database();
  expect(afterLost.dividend_direct_receipts).toHaveLength(2); expect(afterLost.cash_ledger_entries).toHaveLength(after.cash_ledger_entries!.length + 1);
  expect(afterLost.audit_events).toHaveLength(after.audit_events!.length + 3);
  for (const table of Object.keys(after)) if (!changed.has(table)) expect(afterLost[table]).toEqual(after[table]);
  const recovered = await page.request.post(path, { headers: lostHeaders, data: lostBody });
  expect(recovered.status()).toBe(201); expect(await recovered.json()).toEqual(lostResult); expect(database()).toEqual(afterLost);
  const rejection = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, event.version), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic fenced direct receipt event",
    evidence_url: null, reason: "Synthetic rejection after retained direct financial history.",
  } });
  expect(rejection.status()).toBe(409); expect((await rejection.json() as { code: string }).code).toBe("DIVIDEND_EVENT_PROJECTION_REQUIRED");
  expect(database()).toEqual(afterLost);
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
    if (width >= 1024) await page.locator(`tr[data-record-id="${event.id}"]`).getByRole("button", { name: "查看原值與操作" }).click();
    else await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    await article.getByRole("button", { name: "查看帳戶收款紀錄" }).click();
    const receipts = article.getByRole("region", { name: "已記錄股息收款" });
    const read = page.waitForResponse(response => new URL(response.url()).pathname === base && response.request().method() === "GET");
    const choose = receipts.getByRole("button", { name: "選擇收款紀錄帳戶 Synthetic direct receipt account" }); await choose.focus(); await page.keyboard.press("Enter");
    const response = await read; expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    const history = receipts.getByRole("region", { name: "帳戶收款紀錄歷史" });
    await expect(history.getByRole("listitem")).toHaveCount(2); await expect(history).toContainText("USD 6.125000");
    await expect(history).toContainText("直接關聯事件與帳戶"); await expect(history).toContainText("保留稽核記錄相符，付款仍未驗證");
    await expect(history.getByRole("button", { name: /核對作廢收款/ })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  stage("COMPARE"); expect(database()).toEqual(afterLost);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
