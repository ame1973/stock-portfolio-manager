import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, readEvent, stage, type Event } from "./helpers";

type Schemas = components["schemas"];
const financial = ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "expenses", "security_transfers", "fx_rates", "upstream_attempts", "jobs"];

test("explicit tax and fee decisions preserve pins, historical retries and rejection races", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-estimate-write-${randomUUID()}`;
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
  const portfolio = await create("portfolios", { name: "Synthetic estimate write portfolio" });
  const broker = await create("brokers", { name: "Synthetic estimate write broker" });
  let accountName = "Synthetic estimate write account";
  const account = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: accountName, default_currency: "USD" });
  const security = await create("securities", { exchange_code: "NASDAQ", display_ticker: "ESTWRITE", name: "Synthetic estimate write security", quote_currency: "USD" });
  const eventResponse = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "0.00000001", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: "Synthetic event reference", source_url: null, reason: "Synthetic event decision for estimate write tests.",
  } });
  expect(eventResponse.status()).toBe(201); const event = await eventResponse.json() as Event;
  const taxPath = `/api/v1/dividends/events/${event.id}/accounts/${account.id}/tax-settings`;
  const feePath = `/api/v1/dividends/events/${event.id}/accounts/${account.id}/fee-settings`;
  const originalEvent = await readEvent(page, event.id); const initial = database(); for (const table of financial) expect(initial[table]).toEqual([]);

  async function openEstimates() {
    await page.setViewportSize({ width: 360, height: 900 }); await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
    await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    await article.getByRole("button", { name: "查看帳戶稅費估算" }).click();
    const panel = article.getByRole("region", { name: "帳戶稅費估算" });
    await panel.getByRole("button", { name: `選擇估算帳戶 ${accountName}` }).click();
    const taxRead = page.waitForResponse(response => new URL(response.url()).pathname === taxPath && response.request().method() === "GET");
    const feeRead = page.waitForResponse(response => new URL(response.url()).pathname === feePath && response.request().method() === "GET");
    await panel.getByLabel("查閱估算生效日").fill("2026-11-03");
    expect((await taxRead).headers()["cache-control"]).toBe("no-store"); expect((await feeRead).headers()["cache-control"]).toBe("no-store");
    const rejectionRead = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/events/${event.id}/rejection` && response.request().method() === "GET");
    const open = panel.getByRole("button", { name: "核對拒絕狀態以記錄估算" }); await open.focus(); await page.keyboard.press("Enter");
    expect((await rejectionRead).headers()["cache-control"]).toBe("no-store");
    return panel;
  }
  stage("NAVIGATE"); let panel = await openEstimates();
  await panel.getByRole("button", { name: "記錄稅率估算" }).click();
  const sheet = page.getByRole("dialog", { name: "記錄稅率估算" }); const form = sheet.getByRole("form", { name: "記錄稅率估算" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused(); await expect(form).toContainText(event.current_revision_id);
  await expect(form.getByLabel("估算生效日")).toHaveValue("");
  stage("PASSWORD"); const passwordForm = sheet.getByRole("form", { name: "重新驗證" });
  await passwordForm.getByLabel("目前密碼").fill(password); await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm.getByRole("status")).toContainText("已重新驗證");
  await form.getByLabel("估算生效日").fill("2026-11-01"); await form.getByLabel("稅率估算狀態").selectOption("ESTIMATED");
  await form.getByLabel("稅率估算小數").fill("0"); await form.getByLabel("估算依據").fill("Synthetic explicit zero estimated withholding.");
  await form.getByRole("checkbox", { name: /我明白這只是擁有人估算/ }).check();
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); const box = (await sheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; }); const saved = new Promise<void>(resolve => { committed = resolve; });
  let first!: Schemas["TaxSettingRecord"]; let firstBody = ""; let firstKey = ""; let retryBody = ""; let retryKey = ""; let attempts = 0;
  const routePath = "**/api/v1/dividends/events/*/accounts/*/tax-settings";
  await page.route(routePath, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    attempts++; const response = await route.fetch(); expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
    expect(route.request().headers()["if-match"]).toBe(`"${event.version}"`);
    const body = route.request().postData()!; const key = route.request().headers()["idempotency-key"]!;
    if (attempts === 1) { first = await response.json() as Schemas["TaxSettingRecord"]; firstBody = body; firstKey = key; committed(); await held; await route.abort("failed"); }
    else { retryBody = body; retryKey = key; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "記錄估算" }).click(); await saved;
    expect(first.tax_status).toBe("ESTIMATED"); expect(first.rate).toBe("0.000000"); expect(first.setting_no).toBe("1");
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click(); await expect(sheet).toBeVisible();
  } finally { release(); }
  stage("LOST"); await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  const taxSelectionResponse = await page.request.get(`${taxPath}?effective_on=2026-11-03`); const tax = await taxSelectionResponse.json() as Schemas["TaxSettingSelection"];
  const later = await page.request.post(taxPath, { headers: await headers(page, event.version), data: {
    revision_id: tax.revision_id, portfolio_id: tax.portfolio_id, broker_id: tax.broker_id, account_version: tax.account_version, broker_version: tax.broker_version,
    expected_head_no: tax.head_no, expected_head_id: tax.head_id, effective_from: "2026-11-02", rate: null, reason: "Synthetic later unknown tax estimate for replay review.",
  } });
  expect(later.status()).toBe(201); const beforeReplay = database();
  stage("REPLAY"); await form.getByRole("button", { name: "記錄估算" }).click(); await expect(sheet).toHaveCount(0); await page.unroute(routePath);
  expect(attempts).toBe(2); expect(retryKey).toBe(firstKey); expect(retryBody).toBe(firstBody); expect(database()).toEqual(beforeReplay);
  const sent = JSON.parse(firstBody) as Schemas["TaxSettingWrite"];
  expect(sent).toMatchObject({ revision_id: event.current_revision_id, portfolio_id: portfolio.id, broker_id: broker.id,
    account_version: "1", broker_version: "1", expected_head_no: "0", expected_head_id: null, rate: "0" });

  stage("SECOND"); panel = await openEstimates(); await panel.getByRole("button", { name: "記錄費用估算" }).click();
  let feeSheet = page.getByRole("dialog", { name: "記錄費用估算" }); let feeForm = feeSheet.getByRole("form", { name: "記錄費用估算" });
  await feeForm.getByLabel("估算生效日").fill("2026-11-01"); await feeForm.getByLabel("費用估算狀態").selectOption("UNKNOWN");
  await feeForm.getByLabel("費用估算幣別").selectOption("USD"); await feeForm.getByLabel("估算依據").fill("Synthetic unknown fee with an explicit currency.");
  await feeForm.getByRole("checkbox", { name: /我明白這只是擁有人估算/ }).check();
  const feeResponse = page.waitForResponse(response => new URL(response.url()).pathname === feePath && response.request().method() === "POST");
  await feeForm.getByRole("button", { name: "記錄估算" }).click(); const feeSaved = await feeResponse;
  expect(feeSaved.status()).toBe(201); expect(feeSaved.headers()["cache-control"]).toBe("no-store");
  const fee = await feeSaved.json() as Schemas["FeeSettingRecord"]; expect(fee.fee_status).toBe("UNKNOWN"); expect(fee.fee).toBeNull(); expect(fee.fee_currency).toBe("USD");
  await expect(feeSheet).toHaveCount(0);

  stage("CHANGED_EVIDENCE"); panel = await openEstimates(); await panel.getByRole("button", { name: "記錄費用估算" }).click();
  feeSheet = page.getByRole("dialog", { name: "記錄費用估算" }); feeForm = feeSheet.getByRole("form", { name: "記錄費用估算" });
  await feeForm.getByLabel("估算生效日").fill("2026-11-02"); await feeForm.getByLabel("費用估算狀態").selectOption("ESTIMATED");
  await feeForm.getByLabel("費用估算金額").fill("0"); await feeForm.getByLabel("費用估算幣別").selectOption("HKD");
  await feeForm.getByLabel("估算依據").fill("Synthetic fee against a previously observed account version.");
  await feeForm.getByRole("checkbox", { name: /我明白這只是擁有人估算/ }).check();
  accountName = "Synthetic renamed estimate write account";
  const changedAccount = await page.request.patch(`/api/v1/accounts/${account.id}`, { headers: await headers(page, 1), data: { name: accountName } });
  expect(changedAccount.status()).toBe(200); const beforeStale = database();
  const staleResponse = page.waitForResponse(response => new URL(response.url()).pathname === feePath && response.request().method() === "POST");
  await feeForm.getByRole("button", { name: "記錄估算" }).click(); const stale = await staleResponse;
  expect(stale.status()).toBe(409); expect((await stale.json() as { code: string }).code).toBe("DIVIDEND_FEE_SCOPE_CHANGED");
  await expect(feeForm.getByRole("alert")).toContainText("帳戶或券商範圍已變更"); await expect(feeForm.getByLabel("費用估算金額")).toHaveValue("0");
  expect(database()).toEqual(beforeStale); await feeSheet.getByRole("button", { name: "關閉編輯" }).click(); await expect(feeSheet).toHaveCount(0);

  panel = await openEstimates(); await panel.getByRole("button", { name: "記錄費用估算" }).click();
  feeSheet = page.getByRole("dialog", { name: "記錄費用估算" }); feeForm = feeSheet.getByRole("form", { name: "記錄費用估算" });
  await feeForm.getByLabel("估算生效日").fill("2026-11-02"); await feeForm.getByLabel("費用估算狀態").selectOption("ESTIMATED");
  await feeForm.getByLabel("費用估算金額").fill("0"); await feeForm.getByLabel("費用估算幣別").selectOption("HKD");
  await feeForm.getByLabel("估算依據").fill("Synthetic competing explicit fee estimate after rejection.");
  await feeForm.getByRole("checkbox", { name: /我明白這只是擁有人估算/ }).check();
  const rejected = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, event.version), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic competing rejection", evidence_url: null,
    reason: "Synthetic rejection after the estimate editor was opened.",
  } });
  expect(rejected.status()).toBe(200); const beforeDenied = database();
  const deniedResponse = page.waitForResponse(response => new URL(response.url()).pathname === feePath && response.request().method() === "POST");
  await feeForm.getByRole("button", { name: "記錄估算" }).click(); const denied = await deniedResponse;
  expect(denied.status()).toBe(409); expect((await denied.json() as { code: string }).code).toBe("DIVIDEND_REVISION_REJECTED");
  await expect(feeForm.getByRole("alert")).toContainText("此事件版本已記錄拒絕"); await expect(feeForm.getByLabel("費用估算金額")).toHaveValue("0");
  expect(database()).toEqual(beforeDenied); expect(await readEvent(page, event.id)).toEqual(originalEvent);
  for (const table of financial) expect(beforeDenied[table]).toEqual(initial[table]);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
