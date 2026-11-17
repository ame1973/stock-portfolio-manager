import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, stage, type Event } from "./helpers";

type Schemas = components["schemas"];

test("recorded projection history stays private, exact and unverified", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-projection-read-${randomUUID()}`;
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
  const portfolio = await create("portfolios", { name: "Synthetic projection read portfolio" });
  const broker = await create("brokers", { name: "Synthetic projection read broker" });
  const account = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic projection read account", default_currency: "USD" });
  const security = await create("securities", { exchange_code: "NASDAQ", display_ticker: "PROJE2E", name: "Synthetic projection read security", quote_currency: "USD" });
  await create("trades", { portfolio_id: portfolio.id, account_id: account.id, security_id: security.id, side: "BUY", trade_date: "2026-11-02",
    quantity: "10", price: "10", currency: "USD", fee: "0", tax: "0", fx_rate_hkd: "7.8" });
  await create("trades", { portfolio_id: portfolio.id, account_id: account.id, security_id: security.id, side: "SELL", trade_date: "2026-11-03",
    quantity: "2", price: "10", currency: "USD", fee: "0", tax: "0", fx_rate_hkd: "7.8" });
  const eventResponse = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "1", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: "Synthetic projection event reference", source_url: null, reason: "Synthetic event decision for projection history review.",
  } });
  expect(eventResponse.status()).toBe(201); const event = await eventResponse.json() as Event;
  const base = `/api/v1/dividends/events/${event.id}/accounts/${account.id}`;
  const taxResponse = await page.request.get(`${base}/tax-settings?effective_on=2026-11-03`);
  const feeResponse = await page.request.get(`${base}/fee-settings?effective_on=2026-11-03`);
  expect(taxResponse.status()).toBe(200); expect(feeResponse.status()).toBe(200);
  const tax = await taxResponse.json() as Schemas["TaxSettingSelection"]; const fee = await feeResponse.json() as Schemas["FeeSettingSelection"];
  const taxSaved = await page.request.post(`${base}/tax-settings`, { headers: await headers(page, event.version), data: {
    revision_id: tax.revision_id, portfolio_id: tax.portfolio_id, broker_id: tax.broker_id, account_version: tax.account_version, broker_version: tax.broker_version,
    expected_head_no: tax.head_no, expected_head_id: tax.head_id, effective_from: "2026-11-01", rate: "0", reason: "Synthetic explicit zero tax estimate for projection review.",
  } });
  const feeSaved = await page.request.post(`${base}/fee-settings`, { headers: await headers(page, event.version), data: {
    revision_id: fee.revision_id, portfolio_id: fee.portfolio_id, broker_id: fee.broker_id, account_version: fee.account_version, broker_version: fee.broker_version,
    expected_head_no: fee.head_no, expected_head_id: fee.head_id, effective_from: "2026-11-01", fee_status: "ESTIMATED", fee: "0.5", fee_currency: "USD",
    reason: "Synthetic explicit fee estimate for projection review.",
  } });
  expect(taxSaved.status()).toBe(201); expect(feeSaved.status()).toBe(201);
  async function projection(asOf: string) {
    const query = new URLSearchParams({ as_of: asOf, eligibility_rule: "ORDINARY_PRE_EX_DATE", tax_effective_on: "2026-11-03", fee_effective_on: "2026-11-03" });
    const snapshotResponse = await page.request.get(`${base}/projection-inputs?${query}`);
    expect(snapshotResponse.status()).toBe(200); expect(snapshotResponse.headers()["cache-control"]).toBe("no-store");
    const observed = await snapshotResponse.json() as Schemas["ProjectionInputs"];
    const response = await page.request.post(`${base}/projections`, { headers: await headers(page, event.version), data: {
      revision_id: observed.revision_id, portfolio_id: observed.portfolio_id, broker_id: observed.broker_id,
      account_version: observed.account_version, broker_version: observed.broker_version, expected_data_revision: observed.portfolio_data_revision,
      expected_projection_no: observed.projection_head_no, expected_projection_id: observed.projection_head_id, as_of: observed.as_of, eligibility_rule: observed.eligibility_rule,
      tax_effective_on: observed.tax.effective_on, expected_tax_head_no: observed.tax.head_no, expected_tax_head_id: observed.tax.head_id,
      expected_tax_selected_id: observed.tax.selected_id, fee_effective_on: observed.fee.effective_on, expected_fee_head_no: observed.fee.head_no,
      expected_fee_head_id: observed.fee.head_id, expected_fee_selected_id: observed.fee.selected_id,
      fx_rate_hkd: null, fx_source: null, fee_rate_event: null, fee_rate_source: null,
      confirmation: "RECORDED_PROJECTION_ONLY", confirm_revision_fence: true, reason: "Synthetic recorded projection for a private browser read.",
    } });
    expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
    return await response.json() as Schemas["ProjectionRecord"];
  }
  stage("SEED"); const first = await projection("2026-11-02");
  expect(first.state).toBe("EXPECTED"); expect(first.recorded_shares).toBe("10.00000000");
  expect(first.estimated_amounts?.net_local).toBe("9.500000"); expect(first.estimated_amounts?.estimated_hkd).toBeNull();
  const latestFeeResponse = await page.request.get(`${base}/fee-settings?effective_on=2026-11-03`);
  const latestFee = await latestFeeResponse.json() as Schemas["FeeSettingSelection"];
  const unknown = await page.request.post(`${base}/fee-settings`, { headers: await headers(page, event.version), data: {
    revision_id: latestFee.revision_id, portfolio_id: latestFee.portfolio_id, broker_id: latestFee.broker_id,
    account_version: latestFee.account_version, broker_version: latestFee.broker_version, expected_head_no: latestFee.head_no,
    expected_head_id: latestFee.head_id, effective_from: "2026-11-02", fee_status: "UNKNOWN", fee: null, fee_currency: "USD",
    reason: "Synthetic later unknown fee for projection history review.",
  } });
  expect(unknown.status()).toBe(201); const second = await projection("2026-11-03");
  expect(second.state).toBe("NEEDS_REVIEW"); expect(second.fee_provenance?.status).toBe("UNKNOWN");
  const before = database(); expect(before.dividend_receipts).toEqual([]); expect(before.dividend_entitlements).toEqual([]);
  expect(before.fx_rates).toEqual([]); expect(before.upstream_attempts).toEqual([]); expect(before.jobs).toEqual([]);
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
    if (width >= 1024) await page.getByRole("row").filter({ hasText: "1.00000000" }).getByRole("button", { name: "查看原值與操作" }).click();
    else await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    const accountRead = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/accounts" && response.request().method() === "GET");
    const open = article.getByRole("button", { name: "查看帳戶投影歷史" }); await open.focus(); await page.keyboard.press("Enter");
    const panel = article.getByRole("region", { name: "已記錄股息投影" });
    const choose = panel.getByRole("button", { name: "選擇投影帳戶 Synthetic projection read account" }); await expect(choose).toBeEnabled();
    expect((await accountRead).headers()["cache-control"]).toBe("no-store");
    const historyRead = page.waitForResponse(response => new URL(response.url()).pathname === `${base}/projections` && response.request().method() === "GET");
    await choose.click(); expect((await historyRead).headers()["cache-control"]).toBe("no-store");
    const history = panel.getByRole("region", { name: "帳戶投影歷史" });
    await expect(history).toContainText("投影版本 2；需要核對"); await expect(history).toContainText("費用估算：未知，不是零");
    await expect(history).toContainText("投影版本 1；已記錄估算"); await expect(history).toContainText("僅為除淨日前的已記錄持倉");
    await expect(history).toContainText("10.00000000"); await expect(history).toContainText("9.500000");
    await expect(history).toContainText("缺少港幣匯率"); await expect(history).toContainText("不是已確認資格或實收");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  stage("COMPARE"); expect(database()).toEqual(before);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
