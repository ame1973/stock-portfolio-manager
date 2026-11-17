import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, readEvent, stage, type Event } from "./helpers";

type Schemas = components["schemas"];
const financial = ["dividend_receipts", "dividend_entitlements", "cash_ledger_entries", "trades", "expenses", "security_transfers", "fx_rates", "upstream_attempts", "jobs"];

test("recorded projection writes retain exact intent, fences and uncertain retries", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-projection-write-${randomUUID()}`;
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
  const portfolio = await create("portfolios", { name: "Synthetic projection write portfolio" });
  const broker = await create("brokers", { name: "Synthetic projection write broker" });
  const account = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic projection write account", default_currency: "USD" });
  const security = await create("securities", { exchange_code: "NASDAQ", display_ticker: "PRJWRITE", name: "Synthetic projection write security", quote_currency: "USD" });
  await create("trades", { portfolio_id: portfolio.id, account_id: account.id, security_id: security.id, side: "BUY", trade_date: "2026-11-02",
    quantity: "10", price: "10", currency: "USD", fee: "0", tax: "0", fx_rate_hkd: "7.8" });
  await create("trades", { portfolio_id: portfolio.id, account_id: account.id, security_id: security.id, side: "SELL", trade_date: "2026-11-03",
    quantity: "2", price: "10", currency: "USD", fee: "0", tax: "0", fx_rate_hkd: "7.8" });
  async function makeEvent(securityId: string) {
    const response = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
      security_id: securityId, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "1", currency: "USD", adjustment_basis: "AS_DECLARED",
      citation: "Synthetic projection event reference", source_url: null, reason: "Synthetic event decision for projection write review.",
    } });
    expect(response.status()).toBe(201); return await response.json() as Event;
  }
  const event = await makeEvent(security.id); const base = `/api/v1/dividends/events/${event.id}/accounts/${account.id}`;
  async function changeFee(status: "ESTIMATED" | "UNKNOWN", currency: "USD" | "HKD") {
    const response = await page.request.get(`${base}/fee-settings?effective_on=2026-11-03`); expect(response.status()).toBe(200);
    const selection = await response.json() as Schemas["FeeSettingSelection"];
    const saved = await page.request.post(`${base}/fee-settings`, { headers: await headers(page, event.version), data: {
      revision_id: selection.revision_id, portfolio_id: selection.portfolio_id, broker_id: selection.broker_id, account_version: selection.account_version,
      broker_version: selection.broker_version, expected_head_no: selection.head_no, expected_head_id: selection.head_id, effective_from: "2026-11-01",
      fee_status: status, fee: status === "ESTIMATED" ? "0.5" : null, fee_currency: currency, reason: "Synthetic explicit fee for projection write review.",
    } });
    expect(saved.status()).toBe(201);
  }
  const taxRead = await page.request.get(`${base}/tax-settings?effective_on=2026-11-03`); expect(taxRead.status()).toBe(200);
  const tax = await taxRead.json() as Schemas["TaxSettingSelection"];
  const taxSaved = await page.request.post(`${base}/tax-settings`, { headers: await headers(page, event.version), data: {
    revision_id: tax.revision_id, portfolio_id: tax.portfolio_id, broker_id: tax.broker_id, account_version: tax.account_version, broker_version: tax.broker_version,
    expected_head_no: tax.head_no, expected_head_id: tax.head_id, effective_from: "2026-11-01", rate: "0", reason: "Synthetic zero tax for projection write review.",
  } });
  expect(taxSaved.status()).toBe(201); await changeFee("ESTIMATED", "USD");
  const before = database(); expect(before.dividend_projection_versions).toEqual([]); expect(before.dividend_receipts).toEqual([]); expect(before.dividend_entitlements).toEqual([]);
  const originalEvent = await readEvent(page, event.id);
  async function open(target: Event) {
    await page.setViewportSize({ width: 360, height: 900 }); await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${target.id}` }); await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    await article.getByRole("button", { name: "查看帳戶投影歷史" }).click();
    const panel = article.getByRole("region", { name: "已記錄股息投影" }); await panel.getByRole("button", { name: "選擇投影帳戶 Synthetic projection write account" }).click();
    const prepare = panel.getByRole("button", { name: "準備記錄投影" }); await prepare.focus(); await page.keyboard.press("Enter");
    const section = panel.getByRole("region", { name: "準備記錄股息投影" });
    await expect(section.getByLabel("投影查閱日")).toHaveValue(""); await expect(section.getByLabel("投影規則")).toHaveValue("");
    await section.getByLabel("投影查閱日").fill("2026-11-02"); await section.getByLabel("投影規則").selectOption("ORDINARY_PRE_EX_DATE");
    await section.getByLabel("投影稅率估算生效日").fill("2026-11-03"); await section.getByLabel("投影費用估算生效日").fill("2026-11-03");
    const targetBase = `/api/v1/dividends/events/${target.id}/accounts/${account.id}`;
    const responses = ["projection-inputs", "tax-settings", "fee-settings"].map(name => page.waitForResponse(response =>
      new URL(response.url()).pathname === `${targetBase}/${name}` && response.request().method() === "GET"));
    await section.getByRole("button", { name: "核對投影輸入", exact: true }).click();
    for (const response of await Promise.all(responses)) { expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store"); }
    const entry = section.getByRole("button", { name: "開啟記錄投影" }); await expect(entry).toBeEnabled(); await entry.click();
    const sheet = page.getByRole("dialog", { name: "記錄股息投影" }); const form = sheet.getByRole("form", { name: "記錄股息投影" });
    await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
    await expect(form).toContainText("任何已記錄投影"); await expect(form).toContainText("阻止此事件的修訂、撤回及拒絕");
    return { sheet, form };
  }
  async function fill(form: ReturnType<typeof page.getByRole>) {
    await form.getByLabel("港幣估值匯率").selectOption("UNKNOWN"); await form.getByLabel("投影依據").fill("Synthetic recorded projection from explicit browser input.");
    await form.getByRole("checkbox", { name: /我明白這只是未驗證的記錄型投影/ }).check();
    await form.getByRole("checkbox", { name: /我明白記錄投影後/ }).check();
  }
  stage("NAVIGATE"); let { sheet, form } = await open(event);
  stage("PASSWORD"); const passwordForm = sheet.getByRole("form", { name: "重新驗證" });
  await passwordForm.getByLabel("目前密碼").fill(password); await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm.getByRole("status")).toContainText("已重新驗證"); await fill(form);
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); const box = (await sheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; }); const saved = new Promise<void>(resolve => { committed = resolve; });
  let first!: Schemas["ProjectionRecord"]; let firstBody = ""; let firstKey = ""; let retryBody = ""; let retryKey = ""; let attempts = 0;
  const routePath = "**/api/v1/dividends/events/*/accounts/*/projections";
  await page.route(routePath, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    attempts++; const response = await route.fetch(); expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
    expect(route.request().headers()["if-match"]).toBe(`"${event.version}"`);
    const sent = route.request().postData()!; const key = route.request().headers()["idempotency-key"]!;
    if (attempts === 1) { first = await response.json() as Schemas["ProjectionRecord"]; firstBody = sent; firstKey = key; committed(); await held; await route.abort("failed"); }
    else { retryBody = sent; retryKey = key; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "記錄投影" }).click(); await saved;
    expect(first.state).toBe("EXPECTED"); expect(first.recorded_shares).toBe("10.00000000");
    expect(first.estimated_amounts?.net_local).toBe("9.500000"); expect(first.estimated_amounts?.estimated_hkd).toBeNull();
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click(); await expect(sheet).toBeVisible();
  } finally { release(); }
  stage("LOST"); await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  const firstSent = JSON.parse(firstBody) as Schemas["ProjectionWrite"];
  expect(firstSent).toMatchObject({ revision_id: event.current_revision_id, portfolio_id: portfolio.id, broker_id: broker.id,
    expected_projection_no: "0", expected_projection_id: null, fx_rate_hkd: null, fx_source: null, fee_rate_event: null, fee_rate_source: null,
    confirmation: "RECORDED_PROJECTION_ONLY", confirm_revision_fence: true });
  const beforeFence = database();
  const rejected = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, event.version), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic rejected fence attempt", evidence_url: null,
    reason: "Synthetic attempt after a recorded projection.",
  } });
  expect(rejected.status()).toBe(409); expect((await rejected.json() as { code: string }).code).toBe("DIVIDEND_EVENT_PROJECTION_REQUIRED");
  expect(database()).toEqual(beforeFence); expect(await readEvent(page, event.id)).toEqual(originalEvent);
  await changeFee("UNKNOWN", "USD");
  const inputQuery = new URLSearchParams({ as_of: "2026-11-03", eligibility_rule: "ORDINARY_PRE_EX_DATE", tax_effective_on: "2026-11-03", fee_effective_on: "2026-11-03" });
  const newerResponse = await page.request.get(`${base}/projection-inputs?${inputQuery}`); expect(newerResponse.status()).toBe(200);
  const newer = await newerResponse.json() as Schemas["ProjectionInputs"];
  const later = await page.request.post(`${base}/projections`, { headers: await headers(page, event.version), data: {
    revision_id: newer.revision_id, portfolio_id: newer.portfolio_id, broker_id: newer.broker_id, account_version: newer.account_version, broker_version: newer.broker_version,
    expected_data_revision: newer.portfolio_data_revision, expected_projection_no: newer.projection_head_no, expected_projection_id: newer.projection_head_id,
    as_of: newer.as_of, eligibility_rule: newer.eligibility_rule, tax_effective_on: newer.tax.effective_on, expected_tax_head_no: newer.tax.head_no,
    expected_tax_head_id: newer.tax.head_id, expected_tax_selected_id: newer.tax.selected_id, fee_effective_on: newer.fee.effective_on,
    expected_fee_head_no: newer.fee.head_no, expected_fee_head_id: newer.fee.head_id, expected_fee_selected_id: newer.fee.selected_id,
    fx_rate_hkd: null, fx_source: null, fee_rate_event: null, fee_rate_source: null, confirmation: "RECORDED_PROJECTION_ONLY", confirm_revision_fence: true,
    reason: "Synthetic later projection to test historical replay.",
  } });
  expect(later.status()).toBe(201); expect((await later.json() as Schemas["ProjectionRecord"]).state).toBe("NEEDS_REVIEW");
  const beforeReplay = database();
  stage("REPLAY"); await form.getByRole("button", { name: "記錄投影" }).click(); await expect(sheet).toHaveCount(0); await page.unroute(routePath);
  expect(attempts).toBe(2); expect(retryKey).toBe(firstKey); expect(retryBody).toBe(firstBody); expect(database()).toEqual(beforeReplay);

  stage("CHANGED_EVIDENCE"); await changeFee("ESTIMATED", "USD"); ({ sheet, form } = await open(event)); await fill(form);
  await changeFee("UNKNOWN", "HKD"); const beforeStale = database();
  const staleResponse = page.waitForResponse(response => new URL(response.url()).pathname === `${base}/projections` && response.request().method() === "POST");
  await form.getByRole("button", { name: "記錄投影" }).click(); const stale = await staleResponse;
  expect(stale.status()).toBe(409); expect((await stale.json() as { code: string }).code).toBe("DIVIDEND_PROJECTION_INPUT_CHANGED");
  await expect(form.getByRole("alert")).toContainText("投影輸入已變更"); await expect(form.getByLabel("投影依據")).toHaveValue("Synthetic recorded projection from explicit browser input.");
  expect(database()).toEqual(beforeStale); await sheet.getByRole("button", { name: "關閉編輯" }).click(); await expect(sheet).toHaveCount(0);

  stage("SECOND"); await changeFee("ESTIMATED", "HKD"); ({ sheet, form } = await open(event)); await fill(form);
  await form.getByLabel("港幣估值匯率").selectOption("PROVIDED"); await form.getByLabel("USD 兌 HKD 匯率").fill("7.8");
  await form.getByLabel("港幣匯率來源").fill("Synthetic HKD valuation rate");
  await form.getByLabel("費用兌事件幣別匯率").selectOption("PROVIDED"); await form.getByLabel("HKD 兌 USD 匯率").fill("0.128205128205");
  await form.getByLabel("費用匯率來源").fill("Synthetic independent fee rate");
  let convertedRecord!: Schemas["ProjectionRecord"]; let convertedKey = ""; let replayKey = ""; let convertedAttempts = 0;
  await page.route(routePath, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    convertedAttempts++; const response = await route.fetch(); expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
    if (convertedAttempts === 1) { convertedKey = route.request().headers()["idempotency-key"]!; convertedRecord = await response.json() as Schemas["ProjectionRecord"]; await route.abort("failed"); }
    else { replayKey = route.request().headers()["idempotency-key"]!; await route.fulfill({ response }); }
  });
  await form.getByRole("button", { name: "記錄投影" }).click(); await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  expect(convertedRecord.fx_rate_hkd).toBe("7.800000000000"); expect(convertedRecord.fee_rate_event).toBe("0.128205128205");
  expect(convertedRecord.fee_currency).toBe("HKD"); expect(convertedRecord.state).toBe("EXPECTED"); const beforeEqualReplay = database();
  await form.getByLabel("USD 兌 HKD 匯率").fill("0007.800000000000"); await form.getByLabel("HKD 兌 USD 匯率").fill("00.128205128205");
  await form.getByRole("button", { name: "記錄投影" }).click(); await expect(sheet).toHaveCount(0); await page.unroute(routePath);
  expect(convertedAttempts).toBe(2); expect(replayKey).toBe(convertedKey); expect(database()).toEqual(beforeEqualReplay);

  stage("REVIEW_ACK"); const otherSecurity = await create("securities", { exchange_code: "NASDAQ", display_ticker: "PRJREJECT", name: "Synthetic projection rejection security", quote_currency: "USD" });
  const other = await makeEvent(otherSecurity.id); form = (await open(other)).form; await fill(form);
  const rejection = await page.request.post(`/api/v1/dividends/${other.id}/verify`, { headers: await headers(page, other.version), data: {
    revision_id: other.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic competing rejection", evidence_url: null,
    reason: "Synthetic rejection after opening the projection editor.",
  } });
  expect(rejection.status()).toBe(200); const beforeRejectedWrite = database();
  const otherPath = `/api/v1/dividends/events/${other.id}/accounts/${account.id}/projections`;
  const deniedResponse = page.waitForResponse(response => new URL(response.url()).pathname === otherPath && response.request().method() === "POST");
  await form.getByRole("button", { name: "記錄投影" }).click(); const denied = await deniedResponse;
  expect(denied.status()).toBe(409); expect((await denied.json() as { code: string }).code).toBe("DIVIDEND_REVISION_REJECTED");
  await expect(form.getByRole("alert")).toContainText("此事件版本已記錄拒絕"); expect(database()).toEqual(beforeRejectedWrite);
  for (const table of financial) expect(beforeRejectedWrite[table]).toEqual(before[table]);
  expect(beforeRejectedWrite.dividend_projection_versions).toHaveLength(3);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
