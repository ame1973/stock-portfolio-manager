import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, readEvent, stage, type Event } from "./helpers";

type Schemas = components["schemas"];
const financialTables = ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "expenses", "security_transfers", "fx_rates", "upstream_attempts", "jobs"];

test("isolated dividend estimates show exact owner settings without granting eligibility", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-estimates-${randomUUID()}`;
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
  const portfolio = await create("portfolios", { name: "Synthetic estimate portfolio" });
  const broker = await create("brokers", { name: "Synthetic estimate broker" });
  const account = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic estimate account", default_currency: "USD" });
  const security = await create("securities", { exchange_code: "NASDAQ", display_ticker: "ESTE2E", name: "Synthetic estimate security", quote_currency: "USD" });
  const eventResponse = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "0.00000001", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: "Synthetic estimate event reference", source_url: null, reason: "Synthetic event decision for separate estimate review.",
  } });
  expect(eventResponse.status()).toBe(201); const event = await eventResponse.json() as Event;
  const taxPath = `/api/v1/dividends/events/${event.id}/accounts/${account.id}/tax-settings`;
  const feePath = `/api/v1/dividends/events/${event.id}/accounts/${account.id}/fee-settings`;
  const taxResponse = await page.request.get(`${taxPath}?effective_on=2026-11-03`); const feeResponse = await page.request.get(`${feePath}?effective_on=2026-11-03`);
  expect(taxResponse.status()).toBe(200); expect(feeResponse.status()).toBe(200);
  const tax = await taxResponse.json() as Schemas["TaxSettingSelection"]; const fee = await feeResponse.json() as Schemas["FeeSettingSelection"];
  expect(tax.tax_status).toBe("MISSING"); expect(fee.fee_status).toBe("MISSING");
  const taxSaved = await page.request.post(taxPath, { headers: await headers(page, event.version), data: {
    revision_id: tax.revision_id, portfolio_id: tax.portfolio_id, broker_id: tax.broker_id, account_version: tax.account_version, broker_version: tax.broker_version,
    expected_head_no: tax.head_no, expected_head_id: tax.head_id, effective_from: "2026-11-01", rate: "0", reason: "Synthetic explicit zero estimated withholding.",
  } });
  const feeSaved = await page.request.post(feePath, { headers: await headers(page, event.version), data: {
    revision_id: fee.revision_id, portfolio_id: fee.portfolio_id, broker_id: fee.broker_id, account_version: fee.account_version, broker_version: fee.broker_version,
    expected_head_no: fee.head_no, expected_head_id: fee.head_id, effective_from: "2026-11-01", fee_status: "UNKNOWN", fee: null, fee_currency: "USD",
    reason: "Synthetic explicit unknown fee amount with a known currency.",
  } });
  expect(taxSaved.status()).toBe(201); expect(feeSaved.status()).toBe(201);
  expect((await taxSaved.json() as Schemas["TaxSettingRecord"]).rate).toBe("0.000000");
  expect((await feeSaved.json() as Schemas["FeeSettingRecord"]).fee).toBeNull();
  const before = database(); for (const table of financialTables) expect(before[table]).toEqual([]);
  const originalEvent = await readEvent(page, event.id);
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
    if (width >= 1024) await page.getByRole("row").filter({ hasText: "0.00000001" }).getByRole("button", { name: "查看原值與操作" }).click();
    else await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    const accountRead = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/accounts" && response.request().method() === "GET");
    const open = article.getByRole("button", { name: "查看帳戶稅費估算" }); await open.focus(); await page.keyboard.press("Enter");
    const panel = article.getByRole("region", { name: "帳戶稅費估算" });
    const accountButton = panel.getByRole("button", { name: "選擇估算帳戶 Synthetic estimate account" }); await expect(accountButton).toBeEnabled();
    expect((await accountRead).headers()["cache-control"]).toBe("no-store"); await accountButton.click();
    await expect(panel.getByLabel("查閱估算生效日")).toHaveValue("");
    const selectedTax = page.waitForResponse(response => new URL(response.url()).pathname === taxPath && response.request().method() === "GET");
    const selectedFee = page.waitForResponse(response => new URL(response.url()).pathname === feePath && response.request().method() === "GET");
    await panel.getByLabel("查閱估算生效日").fill("2026-11-03");
    expect((await selectedTax).headers()["cache-control"]).toBe("no-store"); expect((await selectedFee).headers()["cache-control"]).toBe("no-store");
    const taxPanel = panel.getByRole("region", { name: "稅率估算" }); const feePanel = panel.getByRole("region", { name: "費用估算" });
    await expect(taxPanel).toContainText("稅率估算 0.000000；不是已確認扣稅"); await expect(feePanel).toContainText("金額未知；幣別 USD");
    await expect(feePanel).toContainText("未記錄拒絕；不是接受決定或財務資格");
    if (width === 360) {
      const earlyTax = page.waitForResponse(response => new URL(response.url()).pathname === taxPath && new URL(response.url()).searchParams.get("effective_on") === "2026-10-31");
      const earlyFee = page.waitForResponse(response => new URL(response.url()).pathname === feePath && new URL(response.url()).searchParams.get("effective_on") === "2026-10-31");
      await panel.getByLabel("查閱估算生效日").fill("2026-10-31"); expect((await earlyTax).status()).toBe(200); expect((await earlyFee).status()).toBe(200);
      await expect(taxPanel).toContainText("未設定，不是零"); await expect(taxPanel).toContainText("觀察到設定頂端 1");
      await expect(feePanel).toContainText("未設定，不是零"); await expect(feePanel).toContainText("觀察到設定頂端 1");
      const restoredTax = page.waitForResponse(response => new URL(response.url()).pathname === taxPath && new URL(response.url()).searchParams.get("effective_on") === "2026-11-03");
      const restoredFee = page.waitForResponse(response => new URL(response.url()).pathname === feePath && new URL(response.url()).searchParams.get("effective_on") === "2026-11-03");
      await panel.getByLabel("查閱估算生效日").fill("2026-11-03"); expect((await restoredTax).status()).toBe(200); expect((await restoredFee).status()).toBe(200);
      await expect(taxPanel).toContainText("稅率估算 0.000000"); await expect(feePanel).toContainText("金額未知");
    }
    const taxHistory = page.waitForResponse(response => new URL(response.url()).pathname === `${taxPath}/history`);
    await taxPanel.getByRole("button", { name: "查看稅率估算歷史" }).click(); expect((await taxHistory).headers()["cache-control"]).toBe("no-store");
    await expect(taxPanel.getByRole("region", { name: "稅率估算歷史" })).toContainText("設定 1");
    const feeHistory = page.waitForResponse(response => new URL(response.url()).pathname === `${feePath}/history`);
    await feePanel.getByRole("button", { name: "查看費用估算歷史" }).click(); expect((await feeHistory).headers()["cache-control"]).toBe("no-store");
    await expect(feePanel.getByRole("region", { name: "費用估算歷史" })).toContainText("金額未知");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  stage("COMPARE"); expect(database()).toEqual(before);
  const rejected = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, event.version), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic separate rejection reference", evidence_url: null,
    reason: "Synthetic rejection after reading separate estimate state.",
  } });
  expect(rejected.status()).toBe(200); expect(await readEvent(page, event.id)).toEqual(originalEvent);
  const afterRejection = database();
  const feePanel = page.getByRole("region", { name: "費用估算", exact: true });
  await feePanel.getByRole("button", { name: "重新載入費用估算" }).click();
  await expect(feePanel).toContainText("此讀取顯示事件版本已拒絕");
  await expect(page.getByText(/稅率與拒絕狀態不是同一讀取/)).toBeVisible();
  expect(database()).toEqual(afterRejection);
  const changedAccount = await page.request.patch(`/api/v1/accounts/${account.id}`, { headers: await headers(page, 1), data: { name: "Synthetic renamed estimate account" } });
  expect(changedAccount.status()).toBe(200); const afterAccountChange = database();
  await feePanel.getByRole("button", { name: "重新載入費用估算" }).click();
  await expect(feePanel).toContainText("帳戶或券商範圍已變更");
  const taxPanel = page.getByRole("region", { name: "稅率估算", exact: true });
  await taxPanel.getByRole("button", { name: "重新載入稅率估算" }).click();
  await expect(taxPanel).toContainText("帳戶或券商範圍已變更");
  expect(database()).toEqual(afterAccountChange);
  for (const table of financialTables) expect(afterAccountChange[table]).toEqual(before[table]);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
