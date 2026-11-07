import { execFileSync } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import { test } from "./private-test";
import type { components } from "../../packages/contracts/src/schema";
import { citedSourceApi, citedSourceBrowserForms } from "./cited-sources";
import { providerProfileApi } from "./provider-profiles";
import { providerSettingsBrowserForms } from "./provider-settings";
import { sourceJobApi } from "./source-jobs";
import { sourceScheduleApi, sourceScheduleBrowserForms } from "./source-schedules";
import { sourceBrowserForms } from "./source-workspace";
import { workspaceBrowserForms } from "./workspace";

const PASSWORD = `synthetic-browser-${randomUUID()}`;
const REPLACEMENT_PASSWORD = `synthetic-updated-browser-${randomUUID()}`;
test.describe.configure({ mode: "serial" });

test("owner onboarding, responsive views, logout revocation, and login", async ({ page, context }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "建立私人帳戶" })).toBeVisible();

  // Capture directly into test memory; never print or persist a live bootstrap credential.
  const output = execFileSync("docker", [
    "compose", "-p", process.env.SPM_E2E_COMPOSE_PROJECT!, "exec", "-T", "api",
    "spm-manage", "setup-token",
  ], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  const bootstrap = output.trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap);
  await page.getByLabel(/設定密碼/).fill(PASSWORD);
  await page.getByLabel("再次輸入密碼").fill(PASSWORD);
  await page.getByRole("button", { name: "建立帳戶" }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();

  const session = (await context.cookies()).find(cookie => cookie.name === "spm_session")!;
  expect(session.httpOnly).toBe(true);
  expect(session.sameSite).toBe("Strict");
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);

  await mkdir(".local/screenshots", { recursive: true });
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByRole("button", { name: "登出" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const bounds = await page.getByRole("button", { name: "重新檢查" }).boundingBox();
    expect(bounds!.height).toBeGreaterThanOrEqual(44);
    await page.screenshot({ path: `.local/screenshots/authenticated-${width}.png`, fullPage: true });
  }
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: ".local/screenshots/authenticated-dark.png", fullPage: true });
  await page.getByRole("button", { name: "登出" }).click();
  await expect(page.getByRole("heading", { name: "登入持倉簿" })).toBeVisible();
  const replay = await page.request.get("/api/v1/auth/me", { headers: { Cookie: `spm_session=${session.value}` } });
  expect(replay.status()).toBe(401);

  await page.getByLabel("密碼", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect(errors).toEqual([]);
  await page.getByRole("button", { name: "登出" }).click();
  await expect(page.getByRole("heading", { name: "登入持倉簿" })).toBeVisible();
});

async function signIn(page: Page, password = PASSWORD) {
  await page.getByLabel("密碼", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page.getByRole("button", { name: "登出", exact: true })).toBeVisible();
}

async function createSecurity(page: Page, name: string, ticker: string, exchange: string, currency: string) {
  await page.getByRole("button", { name: "新增證券", exact: true }).click();
  await page.getByLabel("證券名稱").fill(name);
  await page.getByLabel("證券代碼", { exact: true }).fill(ticker);
  await page.getByLabel("交易所", { exact: true }).selectOption(exchange);
  await page.getByLabel("報價幣別").selectOption(currency);
  await page.getByLabel("代碼有效起日（選填）").fill("2020-01-01");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByText(`已儲存「${name}」。`, { exact: true })).toBeVisible();
}

test("master-data management, dated mappings, conflicts, and session security", async ({ page, context, browser }) => {
  test.setTimeout(150_000);
  const errors: string[] = [];
  const cspErrors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error" && /Content Security Policy|violates.*directive|Refused to apply/i.test(message.text())) cspErrors.push(message.text());
  });
  await page.goto("/settings/portfolios");
  await signIn(page);
  await expect(page.getByRole("heading", { name: "投資組合", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "新增投資組合", exact: true }).click();
  await page.getByLabel("組合名稱").fill("E2E retirement portfolio");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByText("已儲存「E2E retirement portfolio」。", { exact: true })).toBeVisible();

  // A second writer changes the record while this browser keeps the old version and its input.
  const portfolioPage = await (await page.request.get("/api/v1/portfolios")).json() as { items: { id: string; version: number }[] };
  expect(portfolioPage.items).toHaveLength(1);
  const portfolio = portfolioPage.items[0]!;
  await page.getByRole("button", { name: /編輯\s*E2E retirement portfolio/ }).click();
  await page.getByLabel("組合名稱").fill("Stale input must remain");
  const csrf = await (await page.request.get("/api/v1/auth/csrf")).json() as { csrf_token: string };
  const changed = await page.request.patch(`/api/v1/portfolios/${portfolio.id}`, {
    headers: { Origin: process.env.SPM_E2E_BASE_URL!, "X-CSRF-Token": csrf.csrf_token, "Idempotency-Key": randomUUID(), "If-Match": `"${portfolio.version}"` },
    data: { name: "Server-side updated portfolio" },
  });
  expect(changed.status()).toBe(200);
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("不會覆蓋新版");
  await expect(page.getByLabel("組合名稱")).toHaveValue("Stale input must remain");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: /編輯\s*Server-side updated portfolio/ }).click();
  await page.getByLabel("組合名稱").fill("E2E retirement portfolio");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByText("已儲存「E2E retirement portfolio」。", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "券商", exact: true }).click();
  await page.getByRole("button", { name: "新增券商", exact: true }).click();
  await page.getByLabel("券商名稱").fill("E2E synthetic broker");
  await page.getByLabel("每次股息估計代收費").fill("0.000001");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByText("已儲存「E2E synthetic broker」。", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "帳戶", exact: true }).click();
  await page.getByRole("button", { name: "新增帳戶", exact: true }).click();
  await page.getByLabel("帳戶名稱").fill("E2E USD account");
  await page.getByLabel("投資組合", { exact: true }).selectOption({ label: "E2E retirement portfolio" });
  await page.getByLabel("券商", { exact: true }).selectOption({ label: "E2E synthetic broker" });
  await page.getByLabel("帳戶預設幣別").selectOption("USD");
  await page.getByLabel("起始餘額（選填）").fill("1000.000001");
  await page.getByLabel("起始餘額日期").fill("2020-01-02");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("必須有當時鎖定的歷史匯率");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `.local/screenshots/account-form-${width}.png`, fullPage: true });
  }
  await page.getByLabel("起始歷史匯率（1 USD = HKD）").fill("7.812345678901");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByText("已儲存「E2E USD account」。", { exact: true })).toBeVisible();
  const accounts = await (await page.request.get("/api/v1/accounts")).json() as { items: { id: string; opening_cash: string; opening_cash_date: string; cash_completeness: string }[] };
  expect(accounts.items).toHaveLength(1);
  const accountRow = accounts.items[0]!;
  expect(accountRow.opening_cash).toBe("1000.000001");
  expect(accountRow.opening_cash_date).toBe("2020-01-02");
  expect(accountRow.cash_completeness).toBe("UNKNOWN");

  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `.local/screenshots/master-accounts-${width}.png`, fullPage: true });
  }
  await page.getByRole("button", { name: "封存", exact: true }).click();
  await expect(page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true })).toBeFocused();
  expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe("hidden");
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await page.getByRole("button", { name: "封存", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
  await page.getByRole("button", { name: "重新啟用", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
  await expect(page.getByRole("button", { name: "封存", exact: true })).toBeVisible();

  await page.getByRole("link", { name: "券商", exact: true }).click();
  await page.getByRole("button", { name: "封存", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
  await expect(page.getByRole("button", { name: "重新啟用", exact: true })).toBeVisible();
  const retained = await (await page.request.get(`/api/v1/accounts/${accountRow.id}`)).json() as { id: string };
  expect(retained.id).toBe(accountRow.id);
  await page.getByRole("button", { name: "重新啟用", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
  await expect(page.getByRole("button", { name: "封存", exact: true })).toBeVisible();

  await context.setOffline(true);
  await page.getByRole("button", { name: "重新載入券商", exact: true }).click();
  await expect(page.getByText(/連線已暫停/)).toBeVisible();
  await context.setOffline(false);
  await expect(page.getByText(/連線已暫停/)).toHaveCount(0);

  await page.getByRole("link", { name: "證券", exact: true }).click();
  await createSecurity(page, "E2E HK leading zero", "0005", "HKG", "HKD");
  await createSecurity(page, "E2E US same ticker", "0005", "NYSE", "USD");
  await createSecurity(page, "E2E US dotted ticker", "BRK.B", "NYSE", "USD");
  await expect(page.getByText("0005 / HKG / HKD", { exact: true })).toBeVisible();
  await expect(page.getByText("0005 / NYSE / USD", { exact: true })).toBeVisible();
  await expect(page.getByText("BRK.B / NYSE / USD", { exact: true })).toBeVisible();
  const hk = page.getByRole("listitem").filter({ has: page.getByRole("heading", { name: "E2E HK leading zero", exact: true }) });
  await hk.getByRole("button", { name: /編輯/ }).click();
  await page.getByLabel("已人工核實證券身分").check();
  await page.getByLabel("人工核實依據").fill("Synthetic identity checked against dated evidence");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(hk.getByText(/已人工核實身分/)).toBeVisible();
  await hk.getByRole("link", { name: /識別碼與供應商代碼/ }).click();
  await expect(page.getByText("TICKER: 0005", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "新增識別碼", exact: true }).click();
  await page.getByLabel("識別碼類型").selectOption("OTHER");
  await page.getByRole("textbox", { name: "識別碼", exact: true }).fill("SYNTHETIC-HK-ID");
  await page.getByLabel("有效起日", { exact: true }).fill("2020-01-01");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByText("OTHER: SYNTHETIC-HK-ID", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "新增供應商代碼", exact: true }).click();
  await page.getByRole("textbox", { name: "供應商代碼", exact: true }).fill("0005.HK");
  await page.getByLabel("有效起日", { exact: true }).fill("2020-01-01");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByText("yahoo: 0005.HK", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "新增供應商代碼", exact: true }).click();
  await page.getByRole("textbox", { name: "供應商代碼", exact: true }).fill("00005.HK");
  await page.getByLabel("有效起日", { exact: true }).fill("2020-06-01");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("有效期重疊");
  await expect(page.getByRole("textbox", { name: "供應商代碼", exact: true })).toHaveValue("00005.HK");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("listitem").filter({ hasText: "yahoo: 0005.HK" }).getByRole("button", { name: "更正有效期", exact: true }).click();
  await page.getByLabel("有效迄日（選填）").fill("2020-12-31");
  await page.getByLabel("更正／核實依據").fill("Synthetic dated source correction reviewed");
  await page.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(page.getByRole("listitem").filter({ hasText: "yahoo: 0005.HK" })).toContainText("2020-12-31");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.setViewportSize({ width: 360, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: ".local/screenshots/security-mappings-dark.png", fullPage: true });

  const other = await browser.newContext({ baseURL: process.env.SPM_E2E_BASE_URL });
  try {
    const otherPage = await other.newPage();
    await otherPage.goto("/");
    await signIn(otherPage);
    await page.getByRole("link", { name: "帳戶安全", exact: true }).click();
    await expect(page.getByText("目前工作階段（此裝置）", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "撤銷登入", exact: true }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await otherPage.reload();
    await expect(otherPage.getByRole("heading", { name: "登入持倉簿", exact: true })).toBeVisible();
    await signIn(otherPage);
    const otherSession = (await other.cookies()).find(cookie => cookie.name === "spm_session")!;
    await page.getByLabel("目前密碼", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "確認目前密碼", exact: true }).click();
    await expect(page.getByText(/已重新驗證/)).toBeVisible();
    const replacement = REPLACEMENT_PASSWORD;
    await page.getByLabel("舊密碼", { exact: true }).fill(PASSWORD);
    await page.getByLabel("新密碼", { exact: true }).fill(replacement);
    await page.getByLabel("再次輸入新密碼", { exact: true }).fill(replacement);
    await page.getByRole("button", { name: "變更密碼並登出所有裝置", exact: true }).click();
    await expect(page.getByRole("heading", { name: "登入持倉簿", exact: true })).toBeVisible();
    const revoked = await otherPage.request.get("/api/v1/auth/me", { headers: { Cookie: `spm_session=${otherSession.value}` } });
    expect(revoked.status()).toBe(401);
    await signIn(page, replacement);
    await expect(page.getByRole("heading", { name: "帳戶安全", exact: true })).toBeVisible();
    expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
    expect(await page.evaluate(async () => (await caches.keys()).length)).toBe(0);
    await page.getByRole("button", { name: "登出", exact: true }).click();
    await expect(page.getByRole("heading", { name: "登入持倉簿", exact: true })).toBeVisible();
  } finally { await other.close(); }
  expect(errors).toEqual([]);
  expect(cspErrors).toEqual([]);
});

async function signedRequests(page: Page) {
  const csrf = await (await page.request.get("/api/v1/auth/csrf")).json() as { csrf_token: string };
  const headers = (version?: number, key = randomUUID()) => ({
    Origin: process.env.SPM_E2E_BASE_URL!,
    "X-CSRF-Token": csrf.csrf_token,
    "Idempotency-Key": key,
    ...(version === undefined ? {} : { "If-Match": `"${version}"` }),
  });
  async function post<T>(path: string, data: object): Promise<T> {
    const response = await page.request.post(`/api/v1/${path}`, { headers: headers(), data });
    expect(response.status()).toBe(201);
    expect(response.headers()["cache-control"]).toBe("no-store");
    return await response.json() as T;
  }
  return { headers, post };
}

test("trade HTTP postings, oversell rejection, immutable correction, void, and scheduled draft", async ({ page }) => {
  type Trade = components["schemas"]["TradeRecord"];
  await page.goto("/settings/accounts");
  await signIn(page, REPLACEMENT_PASSWORD);
  const { headers, post } = await signedRequests(page);
  const portfolio = await post<components["schemas"]["Portfolio"]>("portfolios", { name: "E2E trade portfolio" });
  const broker = await post<components["schemas"]["Broker"]>("brokers", { name: "E2E trade broker" });
  const account = await post<components["schemas"]["Account"]>("accounts", {
    portfolio_id: portfolio.id, broker_id: broker.id, name: "E2E trade anchor account",
    default_currency: "USD", opening_cash: "1000", opening_cash_date: "2026-01-10",
    opening_fx_rate_hkd: "7.7", cash_completeness: "COMPLETE",
  });
  const security = await post<components["schemas"]["Security"]>("securities", {
    exchange_code: "NASDAQ", display_ticker: "SYN", name: "E2E trade security", quote_currency: "USD",
  });
  const body = {
    portfolio_id: portfolio.id, account_id: account.id, security_id: security.id,
    side: "BUY", trade_date: "2026-01-02", quantity: "10", price: "100", fee: "1", tax: "0",
    currency: "USD", fx_rate_hkd: "7.8", fx_source: "MANUAL", note: "", override_reason: "",
  } satisfies components["schemas"]["TradeCreate"];
  const createHeaders = headers();
  const created = await page.request.post("/api/v1/trades", { headers: createHeaders, data: body });
  expect(created.status()).toBe(201);
  expect(created.headers().etag).toBe('"1"');
  const buy = await created.json() as Trade;
  expect(buy.settlement).toBe("1001.000000");
  expect(buy.booked_hkd).toBe("7807.800000");
  const retry = await page.request.post("/api/v1/trades", { headers: createHeaders, data: body });
  expect(retry.status()).toBe(201);
  expect(await retry.json()).toEqual(buy);
  const sale = await post<Trade>("trades", {
    ...body, side: "SELL", trade_date: "2026-01-03", quantity: "4", price: "120", fee: "2",
  });
  expect(sale.settlement).toBe("478.000000");
  expect(sale.booked_hkd).toBe("3728.400000");
  const oversell = await page.request.post("/api/v1/trades", {
    headers: headers(), data: { ...body, side: "SELL", trade_date: "2026-01-04", quantity: "7" },
  });
  expect(oversell.status()).toBe(409);
  expect((await oversell.json() as { code: string }).code).toBe("INSUFFICIENT_SHARES");
  expect(oversell.headers()["cache-control"]).toBe("no-store");
  const revisedBody = { ...body, quantity: "12", fx_rate_hkd: "7.9", reason: "Correct synthetic broker statement" };
  const stale = await page.request.patch(`/api/v1/trades/${buy.id}`, { headers: headers(99), data: revisedBody });
  expect(stale.status()).toBe(412);
  const revised = await page.request.patch(`/api/v1/trades/${buy.id}`, { headers: headers(1), data: revisedBody });
  expect(revised.status()).toBe(200);
  const replacement = await revised.json() as Trade;
  expect(replacement.root_id).toBe(buy.root_id);
  expect(replacement.created_sequence).toBe(buy.created_sequence);
  expect(replacement.revision).toBe(2);
  expect(replacement.booked_hkd).toBe("9487.900000");
  const original = await (await page.request.get(`/api/v1/trades/${buy.id}`)).json() as Trade;
  expect(original.status).toBe("SUPERSEDED");
  expect(original.booked_hkd).toBe("7807.800000");
  expect(original.fx_rate_hkd).toBe("7.800000000000");
  const reason = { reason: "Void synthetic duplicate trade" };
  const orphanedSale = await page.request.delete(`/api/v1/trades/${replacement.id}`, { headers: headers(1), data: reason });
  expect(orphanedSale.status()).toBe(409);
  const retained = await (await page.request.get(`/api/v1/trades/${replacement.id}`)).json() as Trade;
  expect(retained.status).toBe("ACTIVE");
  expect(retained.version).toBe(1);
  const voidedSale = await page.request.delete(`/api/v1/trades/${sale.id}`, { headers: headers(1), data: reason });
  expect(voidedSale.status()).toBe(200);
  const voidHeaders = headers(1);
  const voidedBuy = await page.request.delete(`/api/v1/trades/${replacement.id}`, { headers: voidHeaders, data: reason });
  expect(voidedBuy.status()).toBe(200);
  const voidRetry = await page.request.delete(`/api/v1/trades/${replacement.id}`, { headers: voidHeaders, data: reason });
  expect(voidRetry.status()).toBe(200);
  expect(await voidRetry.json()).toEqual(await voidedBuy.json());
  const draft = await post<Trade>("trades", {
    ...body, side: "SELL", trade_date: "2050-01-02", quantity: "1", fx_rate_hkd: null,
  });
  expect(draft.status).toBe("DRAFT_SCHEDULED");
  expect(draft.booked_hkd).toBeNull();
  expect(draft.fx_rate_hkd).toBeNull();
  const listing = await page.request.get("/api/v1/trades", { params: { portfolio_id: portfolio.id, include_history: true } });
  const history = await listing.json() as { items: Trade[] };
  expect(history.items).toHaveLength(4);
  expect(history.items.some(row => row.status === "ACTIVE")).toBe(false);
  expect(listing.headers()["cache-control"]).toBe("no-store");
  const preserved = await (await page.request.get(`/api/v1/accounts/${account.id}`)).json() as components["schemas"]["Account"];
  expect(preserved.opening_cash).toBe("1000.000000");
  expect(preserved.opening_cash_date).toBe("2026-01-10");
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  await page.getByRole("button", { name: "登出", exact: true }).click();
  await expect(page.getByRole("heading", { name: "登入持倉簿", exact: true })).toBeVisible();
});

test("native cash and expense HTTP corrections preserve FX, wallets, opening, and exact reversals", async ({ page }) => {
  type Cash = components["schemas"]["CashEntry"];
  type Expense = components["schemas"]["Expense"];
  type Balances = components["schemas"]["AccountCashBalances"];
  await page.goto("/settings/accounts");
  await signIn(page, REPLACEMENT_PASSWORD);
  const { headers, post } = await signedRequests(page);
  const portfolio = await post<components["schemas"]["Portfolio"]>("portfolios", { name: "E2E cash portfolio" });
  const broker = await post<components["schemas"]["Broker"]>("brokers", { name: "E2E cash broker" });
  const account = await post<components["schemas"]["Account"]>("accounts", {
    portfolio_id: portfolio.id, broker_id: broker.id, name: "E2E cash account", default_currency: "USD",
    opening_cash: "1000", opening_cash_date: "2026-01-01", opening_fx_rate_hkd: "7.8", cash_completeness: "COMPLETE",
  });
  const body = {
    account_id: account.id, effective_date: "2026-01-02", kind: "DEPOSIT", currency: "USD",
    amount: "10", fx_rate_hkd: null, note: "", external_flow: true,
  } satisfies components["schemas"]["CashEntryCreate"];
  const createHeaders = headers();
  const first = await page.request.post("/api/v1/cash-entries", { headers: createHeaders, data: body });
  expect(first.status()).toBe(201);
  const original = await first.json() as Cash;
  expect(original.quality).toBe("MISSING_FX");
  expect(original.booked_hkd).toBeNull();
  const replay = await page.request.post("/api/v1/cash-entries", { headers: createHeaders, data: body });
  expect(replay.status()).toBe(201);
  expect(await replay.json()).toEqual(original);
  const initial = await (await page.request.get(`/api/v1/accounts/${account.id}/cash-balances`)).json() as Balances;
  expect(initial.balances[0]!.balance).toBe("1010.000000");
  expect(initial.balances[0]!.historical_booked_hkd_net).toBeNull();
  const correction = { ...body, amount: "12", fx_rate_hkd: "7.9", reason: "Adopt synthetic historical cash evidence" };
  const stale = await page.request.patch(`/api/v1/cash-entries/${original.id}`, { headers: headers(99), data: correction });
  expect(stale.status()).toBe(412);
  const changed = await page.request.patch(`/api/v1/cash-entries/${original.id}`, { headers: headers(1), data: correction });
  expect(changed.status()).toBe(200);
  const revised = await changed.json() as Cash;
  expect(revised.supersedes_id).toBe(original.id);
  expect(revised.booked_hkd).toBe("94.800000");
  const retained = await (await page.request.get(`/api/v1/cash-entries/${original.id}`)).json() as Cash;
  expect(retained.status).toBe("SUPERSEDED");
  expect(retained.fx_rate_hkd).toBeNull();
  expect(retained.amount).toBe("10.000000");
  const expenseBody = {
    account_id: account.id, date: "2026-01-03", category: "Custody fee", amount: "2.000001",
    currency: "USD", fx_rate_hkd: "7.8", note: "",
  } satisfies components["schemas"]["ExpenseCreate"];
  const expense = await post<Expense>("expenses", expenseBody);
  expect(expense.hkd_amount).toBe("15.600008");
  const adjustment = await post<Cash>("cash-entries", {
    account_id: account.id, effective_date: "2026-01-03", kind: "ADJUSTMENT", currency: "HKD",
    amount: "0.000001", reason: "Synthetic reconciled micro adjustment",
  });
  expect(adjustment.external_flow).toBe(false);
  expect(adjustment.booked_hkd).toBe("0.000001");
  const current = await (await page.request.get(`/api/v1/accounts/${account.id}/cash-balances`)).json() as Balances;
  const usd = current.balances.find(row => row.currency === "USD")!;
  const hkd = current.balances.find(row => row.currency === "HKD")!;
  expect(usd.balance).toBe("1009.999999");
  expect(usd.historical_booked_hkd_net).toBe("7879.199992");
  expect(usd.unvalued_entries).toBe(0);
  expect(hkd.balance).toBe("0.000001");
  const opening = await (await page.request.get("/api/v1/cash-entries", {
    params: { account_id: account.id, source_type: "OPENING_BALANCE" },
  })).json() as { items: Cash[] };
  expect(opening.items).toHaveLength(1);
  expect(opening.items[0]!.editable).toBe(false);
  const protectedEntry = await page.request.delete(`/api/v1/cash-entries/${opening.items[0]!.id}`, {
    headers: headers(1), data: { reason: "Synthetic disallowed opening edit" },
  });
  expect(protectedEntry.status()).toBe(409);
  expect((await protectedEntry.json() as { code: string }).code).toBe("SOURCE_MANAGED");
  const reason = { reason: "Void synthetic financial movement" };
  const expenseVoid = await page.request.delete(`/api/v1/expenses/${expense.id}`, { headers: headers(1), data: reason });
  expect(expenseVoid.status()).toBe(200);
  for (const record of [revised, adjustment]) {
    const voidHeaders = headers(1);
    const voided = await page.request.delete(`/api/v1/cash-entries/${record.id}`, { headers: voidHeaders, data: reason });
    expect(voided.status()).toBe(200);
    const repeated = await page.request.delete(`/api/v1/cash-entries/${record.id}`, { headers: voidHeaders, data: reason });
    expect(repeated.status()).toBe(200);
    expect(await repeated.json()).toEqual(await voided.json());
  }
  const finalResponse = await page.request.get(`/api/v1/accounts/${account.id}/cash-balances`);
  expect(finalResponse.headers()["cache-control"]).toBe("no-store");
  const final = await finalResponse.json() as Balances;
  expect(final.balances.find(row => row.currency === "USD")!.balance).toBe("1000.000000");
  expect(final.balances.find(row => row.currency === "USD")!.historical_booked_hkd_net).toBe("7800.000000");
  expect(final.balances.find(row => row.currency === "HKD")!.balance).toBe("0.000000");
  const unchanged = await (await page.request.get(`/api/v1/accounts/${account.id}`)).json() as components["schemas"]["Account"];
  expect(unchanged.opening_cash).toBe("1000.000000");
  expect(unchanged.opening_cash_date).toBe("2026-01-01");
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  await page.getByRole("button", { name: "登出", exact: true }).click();
  await expect(page.getByRole("heading", { name: "登入持倉簿", exact: true })).toBeVisible();
});

test("paired cash and cost-preserving securities transfer HTTP revisions commit both sides or neither", async ({ page }) => {
  type CashTransfer = components["schemas"]["CashTransfer"];
  type StockTransfer = components["schemas"]["SecurityTransfer"];
  type Trade = components["schemas"]["TradeRecord"];
  type Balances = components["schemas"]["AccountCashBalances"];
  await page.goto("/settings/accounts");
  await signIn(page, REPLACEMENT_PASSWORD);
  const { headers, post } = await signedRequests(page);
  const portfolio = await post<components["schemas"]["Portfolio"]>("portfolios", { name: "E2E transfer portfolio" });
  const broker = await post<components["schemas"]["Broker"]>("brokers", { name: "E2E transfer broker" });
  const accounts = [];
  for (const name of ["E2E transfer source", "E2E transfer destination"]) {
    accounts.push(await post<components["schemas"]["Account"]>("accounts", {
      portfolio_id: portfolio.id, broker_id: broker.id, name, default_currency: "USD", opening_cash: "2000",
      opening_cash_date: "2026-01-01", opening_fx_rate_hkd: "7.8", cash_completeness: "COMPLETE",
    }));
  }
  const [source, target] = [accounts[0]!, accounts[1]!];
  const cashBody = {
    portfolio_id: portfolio.id, source_account_id: source.id, target_account_id: target.id,
    transfer_date: "2026-01-03", source_currency: "USD", target_currency: "USD",
    source_amount: "100", target_amount: "100", fx_rate_hkd: "7.8", note: "",
    reason: "Synthetic reviewed internal transfer",
  } satisfies components["schemas"]["CashTransferCreate"];
  const pair = await post<CashTransfer>("cash-transfers", cashBody);
  expect(pair.booking_delta_hkd).toBe("0.000000");
  expect(pair.source_booked_hkd).toBe("780.000000");
  const correction = await page.request.patch(`/api/v1/cash-transfers/${pair.id}`, {
    headers: headers(1), data: { ...cashBody, source_amount: "120", target_amount: "120", fx_rate_hkd: "7.9" },
  });
  expect(correction.status()).toBe(200);
  const correctedPair = await correction.json() as CashTransfer;
  expect(correctedPair.root_id).toBe(pair.id);
  expect(correctedPair.revision).toBe(2);
  expect(correctedPair.source_booked_hkd).toBe("948.000000");
  const oldPair = await (await page.request.get(`/api/v1/cash-transfers/${pair.id}`)).json() as CashTransfer;
  expect(oldPair.status).toBe("SUPERSEDED");
  expect(oldPair.source_booked_hkd).toBe("780.000000");
  const fx = await post<CashTransfer>("cash-transfers", {
    ...cashBody, target_account_id: source.id, source_amount: "1", target_amount: "7.8", target_currency: "HKD",
  });
  expect(fx.booking_delta_hkd).toBe("0.000000");
  const reason = { reason: "Void synthetic reviewed transfer" };
  const fxVoid = await page.request.delete(`/api/v1/cash-transfers/${fx.id}`, { headers: headers(1), data: reason });
  expect(fxVoid.status()).toBe(200);
  const stock = await post<components["schemas"]["Security"]>("securities", {
    exchange_code: "NASDAQ", display_ticker: "XFR", name: "E2E transfer security", quote_currency: "USD",
  });
  await post<Trade>("trades", {
    portfolio_id: portfolio.id, account_id: source.id, security_id: stock.id, side: "BUY", trade_date: "2026-01-02",
    quantity: "10", price: "100", fee: "1", currency: "USD", fx_rate_hkd: "7.8",
  });
  const balancesBefore = await (await page.request.get(`/api/v1/accounts/${source.id}/cash-balances`)).json() as Balances;
  const stockBody = {
    portfolio_id: portfolio.id, source_account_id: source.id, target_account_id: target.id,
    security_id: stock.id, transfer_date: "2026-01-03", quantity: "4", note: "",
  } satisfies components["schemas"]["SecurityTransferCreate"];
  const moved = await post<StockTransfer>("security-transfers", stockBody);
  expect(moved.cost_moved_hkd).toBe("3123.120000");
  expect(moved.cost_scope).toBe("BOOKED_AT_CREATION");
  const balancesAfter = await (await page.request.get(`/api/v1/accounts/${source.id}/cash-balances`)).json() as Balances;
  expect(balancesAfter).toEqual(balancesBefore);
  const sale = await post<Trade>("trades", {
    portfolio_id: portfolio.id, account_id: target.id, security_id: stock.id, side: "SELL", trade_date: "2026-01-04",
    quantity: "2", price: "120", fee: "1", currency: "USD", fx_rate_hkd: "7.8",
  });
  const orphan = await page.request.delete(`/api/v1/security-transfers/${moved.id}`, { headers: headers(1), data: reason });
  expect(orphan.status()).toBe(409);
  expect((await orphan.json() as { code: string }).code).toBe("INSUFFICIENT_SHARES");
  const revised = await page.request.patch(`/api/v1/security-transfers/${moved.id}`, {
    headers: headers(1), data: { ...stockBody, quantity: "5", reason: "Correct synthetic custodian movement" },
  });
  expect(revised.status()).toBe(200);
  const revisedStock = await revised.json() as StockTransfer;
  expect(revisedStock.root_id).toBe(moved.id);
  expect(revisedStock.created_sequence).toBe(moved.created_sequence);
  expect(revisedStock.cost_moved_hkd).toBe("3903.900000");
  const oldStock = await (await page.request.get(`/api/v1/security-transfers/${moved.id}`)).json() as StockTransfer;
  expect(oldStock.is_current).toBe(false);
  expect(oldStock.status).toBe("SUPERSEDED");
  expect(oldStock.cost_moved_hkd).toBe("3123.120000");
  const saleVoid = await page.request.delete(`/api/v1/trades/${sale.id}`, { headers: headers(1), data: reason });
  expect(saleVoid.status()).toBe(200);
  const stockVoidHeaders = headers(1);
  const stockVoid = await page.request.delete(`/api/v1/security-transfers/${revisedStock.id}`, { headers: stockVoidHeaders, data: reason });
  expect(stockVoid.status()).toBe(200);
  const stockRetry = await page.request.delete(`/api/v1/security-transfers/${revisedStock.id}`, { headers: stockVoidHeaders, data: reason });
  expect(stockRetry.status()).toBe(200);
  expect(await stockRetry.json()).toEqual(await stockVoid.json());
  const pairVoidHeaders = headers(1);
  const pairVoid = await page.request.delete(`/api/v1/cash-transfers/${correctedPair.id}`, { headers: pairVoidHeaders, data: reason });
  expect(pairVoid.status()).toBe(200);
  const pairRetry = await page.request.delete(`/api/v1/cash-transfers/${correctedPair.id}`, { headers: pairVoidHeaders, data: reason });
  expect(pairRetry.status()).toBe(200);
  expect(await pairRetry.json()).toEqual(await pairVoid.json());
  const currentSource = await (await page.request.get(`/api/v1/accounts/${source.id}/cash-balances`)).json() as Balances;
  const currentTarget = await (await page.request.get(`/api/v1/accounts/${target.id}/cash-balances`)).json() as Balances;
  expect(currentSource.balances.find(row => row.currency === "USD")!.balance).toBe("999.000000");
  expect(currentSource.balances.find(row => row.currency === "HKD")!.balance).toBe("0.000000");
  expect(currentTarget.balances.find(row => row.currency === "USD")!.balance).toBe("2000.000000");
  const history = await page.request.get("/api/v1/security-transfers", { params: { root_id: moved.id, include_history: true } });
  expect(history.headers()["cache-control"]).toBe("no-store");
  expect((await history.json() as { items: StockTransfer[] }).items).toHaveLength(2);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  await page.getByRole("button", { name: "登出", exact: true }).click();
  await expect(page.getByRole("heading", { name: "登入持倉簿", exact: true })).toBeVisible();
});

test("action terms and opening assertion HTTP corrections preserve evidence and truthful balances", async ({ page }) => {
  type Action = components["schemas"]["CorporateAction"];
  type Opening = components["schemas"]["OpeningCashRecord"];
  type Cash = components["schemas"]["CashEntry"];
  type Trade = components["schemas"]["TradeRecord"];
  type Balances = components["schemas"]["AccountCashBalances"];
  await page.goto("/settings/accounts");
  await signIn(page, REPLACEMENT_PASSWORD);
  const { headers, post } = await signedRequests(page);
  const portfolio = await post<components["schemas"]["Portfolio"]>("portfolios", { name: "E2E action opening portfolio" });
  const broker = await post<components["schemas"]["Broker"]>("brokers", { name: "E2E action opening broker" });
  const account = await post<components["schemas"]["Account"]>("accounts", {
    portfolio_id: portfolio.id, broker_id: broker.id, name: "E2E corrected opening account", default_currency: "USD",
    opening_cash: "1000", opening_cash_date: "2026-01-10", opening_fx_rate_hkd: "7.7", cash_completeness: "COMPLETE",
  });
  const security = await post<components["schemas"]["Security"]>("securities", {
    exchange_code: "NASDAQ", display_ticker: "ACTE", name: "E2E action security", quote_currency: "USD",
    identifier_effective_from: "2020-01-01",
  });
  const buyBody = {
    portfolio_id: portfolio.id, account_id: account.id, security_id: security.id, side: "BUY",
    trade_date: "2026-01-02", quantity: "10", price: "100", fee: "1", currency: "USD", fx_rate_hkd: "7.8",
  };
  await post<Trade>("trades", buyBody);
  async function journal() {
    const response = await page.request.get("/api/v1/cash-entries", { params: { account_id: account.id, limit: 100 } });
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
    return (await response.json() as { items: Cash[] }).items;
  }
  const beforeSplit = await journal();
  const body = {
    security_id: security.id, action_type: "SPLIT", effective_date: "2026-01-03", ratio_numerator: 3, ratio_denominator: 2,
    status: "CONFIRMED", reason: "Synthetic reviewed split evidence", note: "",
  } satisfies components["schemas"]["CorporateActionCreate"];
  const splitHeaders = headers();
  const created = await page.request.post("/api/v1/corporate-actions", { headers: splitHeaders, data: body });
  expect(created.status()).toBe(201);
  expect(created.headers()["cache-control"]).toBe("no-store");
  const original = await created.json() as Action;
  expect(original.effect_scope).toBe("QUANTITY_TERMS");
  expect(original.allocation_scope).toBe("TERMS_NOT_A_VALUATION");
  const largeRatio = await post<Action>("corporate-actions", {
    ...body, effective_date: "2050-01-03", status: "NEEDS_REVIEW", ratio_numerator: "9223372036854775807", ratio_denominator: "9223372036854775806",
  });
  expect(largeRatio.ratio_numerator).toBe("9223372036854775807");
  expect(await page.evaluate(async identifier => {
    const response = await fetch(`/api/v1/corporate-actions/${identifier}`, { cache: "no-store", credentials: "same-origin" });
    const data = await response.json() as { ratio_numerator: unknown; ratio_denominator: unknown };
    return [data.ratio_numerator, data.ratio_denominator];
  }, largeRatio.id)).toEqual(["9223372036854775807", "9223372036854775806"]);
  expect(await journal()).toEqual(beforeSplit);
  const repeated = await page.request.post("/api/v1/corporate-actions", { headers: splitHeaders, data: body });
  expect(repeated.status()).toBe(201);
  expect(await repeated.json()).toEqual(original);
  const sale = await post<Trade>("trades", { ...buyBody, side: "SELL", trade_date: "2026-01-04", quantity: "15", price: "120" });
  const reason = { reason: "Synthetic correction of reviewed evidence" };
  const beforeRejectedVoid = await journal();
  const orphan = await page.request.delete(`/api/v1/corporate-actions/${original.id}`, { headers: headers(1), data: reason });
  expect(orphan.status()).toBe(409);
  expect((await orphan.json() as { code: string }).code).toBe("INSUFFICIENT_SHARES");
  expect(await journal()).toEqual(beforeRejectedVoid);
  const revisedResponse = await page.request.patch(`/api/v1/corporate-actions/${original.id}`, {
    headers: headers(1), data: { ...body, ratio_numerator: 2, ratio_denominator: 1 },
  });
  expect(revisedResponse.status()).toBe(200);
  const revised = await revisedResponse.json() as Action;
  expect(revised.root_id).toBe(original.id);
  expect(revised.created_sequence).toBe(original.created_sequence);
  expect(revised.revision).toBe(2);
  const retained = await (await page.request.get(`/api/v1/corporate-actions/${original.id}`)).json() as Action;
  expect(retained.lifecycle_status).toBe("SUPERSEDED");
  expect(retained.ratio_numerator).toBe("3");
  expect(retained.ratio_denominator).toBe("2");
  expect((await page.request.delete(`/api/v1/trades/${sale.id}`, { headers: headers(1), data: reason })).status()).toBe(200);

  const beforeReview = await journal();
  const review = await post<Action>("corporate-actions", {
    ...body, action_type: "MERGER", effective_date: "2026-01-06", ratio_numerator: null, ratio_denominator: null, status: "NEEDS_REVIEW",
  });
  expect(review.effect_scope).toBe("REVIEW_ONLY");
  expect(review.review_required).toBe(true);
  expect(await journal()).toEqual(beforeReview);
  const blocked = await page.request.post("/api/v1/trades", { headers: headers(), data: { ...buyBody, trade_date: "2026-01-07", quantity: "1" } });
  expect(blocked.status()).toBe(409);
  expect((await blocked.json() as { code: string }).code).toBe("NEEDS_REVIEW");
  expect(await journal()).toEqual(beforeReview);
  expect((await page.request.delete(`/api/v1/corporate-actions/${review.id}`, { headers: headers(1), data: reason })).status()).toBe(200);
  const mappingsURL = `/api/v1/securities/${security.id}/identifiers`;
  const mappingsBefore = await (await page.request.get(mappingsURL)).json() as { items: components["schemas"]["Identifier"][] };
  const symbol = await post<Action>("corporate-actions", {
    ...body, action_type: "SYMBOL_CHANGE", effective_date: "2026-01-05", ratio_numerator: null, ratio_denominator: null, new_symbol: "ACTN",
  });
  expect(symbol.effect_scope).toBe("DISPLAY_ONLY");
  expect(symbol.previous_symbol).toBe("ACTE");
  expect(symbol.review_required).toBe(true);
  const renamed = await (await page.request.get(`/api/v1/securities/${security.id}`)).json() as components["schemas"]["Security"];
  expect(renamed.id).toBe(security.id);
  expect(renamed.display_ticker).toBe("ACTN");
  expect(await (await page.request.get(mappingsURL)).json()).toEqual(mappingsBefore);
  expect(await journal()).toEqual(beforeReview);

  const openingURL = `/api/v1/accounts/${account.id}/opening-cash`;
  const oldOpening = await (await page.request.get(openingURL)).json() as Opening;
  const correction = {
    opening_cash: "1500", opening_cash_date: "2026-01-15", opening_fx_rate_hkd: "7.9",
    reason: "Synthetic corrected opening statement", note: "",
  } satisfies components["schemas"]["OpeningCashUpdate"];
  const correctionHeaders = headers(1);
  const correctedResponse = await page.request.patch(openingURL, { headers: correctionHeaders, data: correction });
  expect(correctedResponse.status()).toBe(200);
  expect(correctedResponse.headers().etag).toBe('"2"');
  const corrected = await correctedResponse.json() as Opening;
  expect(corrected.predecessor_id).toBe(oldOpening.entry_id);
  expect(corrected.fx_rate_hkd).toBe("7.900000000000");
  expect(corrected.booked_hkd).toBe("11850.000000");
  const correctionRetry = await page.request.patch(openingURL, { headers: correctionHeaders, data: correction });
  expect(correctionRetry.status()).toBe(200);
  expect(await correctionRetry.json()).toEqual(corrected);
  const oldCash = (await journal()).find(row => row.id === oldOpening.entry_id)!;
  expect(oldCash.amount).toBe("1000.000000");
  expect(oldCash.fx_rate_hkd).toBe("7.700000000000");
  expect(oldCash.status).toBe("SUPERSEDED");
  const balance = await (await page.request.get(`/api/v1/accounts/${account.id}/cash-balances`)).json() as Balances;
  expect(balance.balances[0]!.balance).toBe("1500.000000");
  const cleared = await page.request.patch(openingURL, { headers: headers(2), data: { opening_cash: null, ...reason } });
  expect(cleared.status()).toBe(200);
  expect((await cleared.json() as Opening).quality).toBe("UNSET");
  const unknown = await (await page.request.get(`/api/v1/accounts/${account.id}/cash-balances`)).json() as Balances;
  expect(unknown.balances[0]!.recorded_balance).toBe("-1001.000000");
  expect(unknown.balances[0]!.balance).toBeNull();
  expect(unknown.balances[0]!.balance_quality).toBe("UNKNOWN");
  const zero = await page.request.patch(openingURL, {
    headers: headers(3), data: { ...correction, opening_cash: "0", opening_cash_date: "2026-01-20", opening_fx_rate_hkd: "8" },
  });
  expect(zero.status()).toBe(200);
  const knownZero = await zero.json() as Opening;
  expect(knownZero.opening_cash).toBe("0.000000");
  expect(knownZero.cash_completeness).toBe("UNKNOWN");
  const complete = await page.request.patch(`/api/v1/accounts/${account.id}`, { headers: headers(4), data: { cash_completeness: "COMPLETE" } });
  expect(complete.status()).toBe(200);
  const final = await (await page.request.get(`/api/v1/accounts/${account.id}/cash-balances`)).json() as Balances;
  expect(final.balances[0]!.balance).toBe("0.000000");
  expect(final.balances[0]!.historical_booked_hkd_net).toBe("200.200000");
  const rows = await journal();
  expect(rows).toHaveLength(17);
  expect(rows.filter(row => row.entry_role === "REVERSAL")).toHaveLength(7);
  expect(rows.every(row => row.external_flow === false)).toBe(true);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  await page.getByRole("button", { name: "登出", exact: true }).click();
  await expect(page.getByRole("heading", { name: "登入持倉簿", exact: true })).toBeVisible();
});

test("financial browser forms preserve exact facts, conflicts, reversals and opening assertions", async ({ page }) => {
  test.setTimeout(180_000);
  type Trade = components["schemas"]["TradeRecord"];
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error" && /Content Security Policy|violates.*directive|Refused to apply/i.test(message.text())) errors.push(message.text());
  });
  await page.goto("/settings/accounts");
  await signIn(page, REPLACEMENT_PASSWORD);
  const { headers, post } = await signedRequests(page);
  const portfolio = await post<components["schemas"]["Portfolio"]>("portfolios", { name: "E2E trade form portfolio" });
  const broker = await post<components["schemas"]["Broker"]>("brokers", { name: "E2E trade form broker" });
  const account = await post<components["schemas"]["Account"]>("accounts", {
    portfolio_id: portfolio.id, broker_id: broker.id, name: "E2E UI ledger account", default_currency: "USD",
    opening_cash: "2000", opening_cash_date: "2026-01-01", opening_fx_rate_hkd: "7.7", cash_completeness: "COMPLETE",
  });
  const stock = await post<components["schemas"]["Security"]>("securities", {
    exchange_code: "NASDAQ", display_ticker: "UIE", name: "E2E trade form stock", quote_currency: "USD",
  });
  await page.getByRole("link", { name: "交易紀錄", exact: true }).click();
  await expect(page.getByRole("heading", { name: "交易紀錄", exact: true })).toBeVisible();
  await page.getByText("篩選交易", { exact: true }).click();
  await page.getByLabel("篩選組合", { exact: true }).selectOption(portfolio.id);
  await expect(page.getByText("這個篩選範圍沒有紀錄；請調整日期、搜尋或清除篩選。", { exact: true })).toBeVisible();
  await page.getByText("篩選交易", { exact: true }).click();

  async function enterTrade(side: "BUY" | "SELL", day: string, quantity: string, price: string, fee: string, rate: string) {
    await page.getByRole("button", { name: "新增交易", exact: true }).click();
    const form = page.getByRole("form", { name: "新增交易", exact: true });
    await form.getByLabel("投資組合", { exact: true }).selectOption(portfolio.id);
    await form.getByLabel("交易帳戶", { exact: true }).selectOption(account.id);
    await form.getByLabel("交易證券", { exact: true }).selectOption(stock.id);
    await form.getByLabel("買賣方向").selectOption(side);
    await form.getByLabel("交易日期", { exact: true }).fill(day);
    await form.getByLabel("交易股數").fill(quantity);
    await form.getByLabel("每股成交價").fill(price);
    await form.getByLabel("交易費用").fill(fee);
    await form.getByLabel("歷史匯率（1 USD = HKD）").fill(rate);
    return form;
  }
  let form = await enterTrade("BUY", "2026-01-02", "10", "100", "1", "7.8");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const label of ["買賣方向", "交易股數", "交易稅費"]) {
      const bounds = await form.getByLabel(label).boundingBox();
      expect(bounds!.height).toBeGreaterThanOrEqual(44);
      expect(bounds!.height).toBeLessThanOrEqual(56);
    }
    await page.screenshot({ path: `.local/screenshots/trade-form-${width}.png`, fullPage: true });
  }
  await page.emulateMedia({ colorScheme: "dark" });
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("dark");
  await page.screenshot({ path: ".local/screenshots/trade-form-dark.png", fullPage: true });
  await page.emulateMedia({ colorScheme: "light" });
  await page.setViewportSize({ width: 360, height: 900 });
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const buyCard = () => page.getByRole("article", { name: /^2026-01-02 買入/ }).filter({ has: page.getByRole("button", { name: "更正交易", exact: true }) });
  await expect(buyCard()).toContainText("7807.800000");
  form = await enterTrade("SELL", "2026-01-03", "11", "120", "2", "7.8");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("持倉不足");
  await expect(form.getByLabel("交易股數")).toHaveValue("11");
  await form.getByLabel("交易股數").fill("4");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(page.getByRole("article", { name: /^2026-01-03 賣出/ })).toContainText("478.000000");
  const original = (await (await page.request.get("/api/v1/trades", { params: { portfolio_id: portfolio.id } })).json() as { items: Trade[] }).items.find(row => row.side === "BUY")!;
  await buyCard().getByRole("button", { name: "更正交易", exact: true }).click();
  form = page.getByRole("form", { name: "更正交易", exact: true });
  await form.getByLabel("交易股數").fill("12");
  await form.getByLabel("歷史匯率（1 USD = HKD）").fill("7.9");
  await form.getByLabel("交易更正依據").fill("Synthetic reviewed corrected broker statement");
  // A real second writer revises the same immutable root while the form retains its old ID/version.
  const otherWriter = await page.request.patch(`/api/v1/trades/${original.id}`, {
    headers: headers(1), data: {
      portfolio_id: portfolio.id, account_id: account.id, security_id: stock.id, side: "BUY", trade_date: "2026-01-02",
      quantity: "11", price: "100", fee: "1", currency: "USD", fx_rate_hkd: "7.8", reason: "Synthetic concurrent statement correction",
    },
  });
  expect(otherWriter.status()).toBe(200);
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("不會覆蓋新版");
  await expect(form.getByLabel("交易股數")).toHaveValue("12");
  await expect(form.getByLabel("歷史匯率（1 USD = HKD）")).toHaveValue("7.9");
  await form.getByRole("button", { name: "取消", exact: true }).click();
  await expect(buyCard()).toContainText("11.00000000");
  await buyCard().getByRole("button", { name: "更正交易", exact: true }).click();
  form = page.getByRole("form", { name: "更正交易", exact: true });
  await form.getByLabel("交易股數").fill("12");
  await form.getByLabel("歷史匯率（1 USD = HKD）").fill("7.9");
  await form.getByLabel("交易更正依據").fill("Synthetic reconciled replacement statement");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(buyCard()).toContainText("9487.900000");
  await buyCard().getByRole("button", { name: "查看此事件修訂鏈", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(3);
  await expect(page.getByText("已有新修訂", { exact: true })).toHaveCount(2);
  await buyCard().getByRole("button", { name: "作廢交易", exact: true }).click();
  await page.getByLabel("作廢交易依據").fill("Synthetic dependency rollback demonstration");
  await page.getByRole("button", { name: "作廢此交易", exact: true }).click();
  await expect(page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true })).toBeFocused();
  await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
  await expect(page.getByRole("alertdialog").getByRole("alert")).toContainText("持倉不足");
  await page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "取消作廢", exact: true }).click();
  await page.getByRole("button", { name: "顯示全部交易事件", exact: true }).click();
  for (const card of [page.getByRole("article", { name: /^2026-01-03 賣出/ }), buyCard()]) {
    await card.getByRole("button", { name: "作廢交易", exact: true }).click();
    await page.getByLabel("作廢交易依據").fill("Void reviewed synthetic broker record");
    await page.getByRole("button", { name: "作廢此交易", exact: true }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await expect(page.getByText("交易已作廢；原值、精確沖銷及審計仍保留。", { exact: true })).toBeVisible();
  }
  form = await enterTrade("SELL", "2050-01-02", "1", "50", "0", "");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(page.getByText("已儲存未來交易草稿；沒有現金或目前持倉影響。", { exact: true })).toBeVisible();
  await expect(page.getByText("未知，未確認 HKD 認列", { exact: true })).toBeVisible();
  const history = await (await page.request.get("/api/v1/trades", { params: { portfolio_id: portfolio.id, include_history: true } })).json() as { items: Trade[] };
  expect(history.items).toHaveLength(5);
  expect(history.items.filter(row => row.status === "ACTIVE")).toHaveLength(0);
  const cash = await (await page.request.get(`/api/v1/accounts/${account.id}/cash-balances`)).json() as components["schemas"]["AccountCashBalances"];
  expect(cash.balances[0]!.balance).toBe("2000.000000");
  expect(cash.balances[0]!.historical_booked_hkd_net).toBe("15400.000000");
  await page.setViewportSize({ width: 360, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: ".local/screenshots/trade-history-360.png", fullPage: true });
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  await test.step("cash, expenses, wallets and opening: unknown is not known zero", () => cashBrowserForms(page));
  await test.step("paired cash and stock transfers: preserve both legs, cost and dependencies", () => transferBrowserForms(page));
  await test.step("corporate-action forms: exact ratios, adopted terms and explicit review", () => actionBrowserForms(page));
  await test.step("responsive workspace: server tables, full-screen sheets, focus and pending-write protection", async () => workspaceBrowserForms(page, (await signedRequests(page)).post));
  await test.step("source-only job API: real unconfigured worker, safe progress and no financial changes", async () => sourceJobApi(page, (await signedRequests(page)).headers, stock.id, portfolio.id, account.id));
  await test.step("source schedule API: owned intent, actual daemon clock, CAS and restart dedup", async () => sourceScheduleApi(page, (await signedRequests(page)).headers, stock.id, portfolio.id, account.id));
  await test.step("data center: explicit source queue, cancellation and exact network-disabled evidence", async () => sourceBrowserForms(page, (await signedRequests(page)).post, portfolio.id, account.id));
  await test.step("source schedule UI: responsive frozen editors, actual 412 and immutable 25+2 receipts", async () => sourceScheduleBrowserForms(page, (await signedRequests(page)).headers, stock.id, portfolio.id, account.id));
  await test.step("managed policy API: current reauthentication, CAS, original replay and immutable 2+1 history", async () => providerProfileApi(page, (await signedRequests(page)).headers, portfolio.id, account.id));
  await test.step("managed settings UI: frozen policy edits, actual 412, pending protection and immutable 25+2 history", async () => providerSettingsBrowserForms(page, (await signedRequests(page)).headers, REPLACEMENT_PASSWORD, portfolio.id, account.id));
  await test.step("cited source API: current actor, exact quotations, disabled replay and unchanged financial facts", async () => citedSourceApi(page, (await signedRequests(page)).headers, portfolio.id, account.id));
  await test.step("cited source forms: exact terms, pending protection and original replay after disable/alias/read failures", async () => citedSourceBrowserForms(page, (await signedRequests(page)).headers, portfolio.id, account.id));
  expect(errors).toEqual([]);
  await page.getByRole("button", { name: "登出", exact: true }).click();
  await expect(page.getByRole("heading", { name: "登入持倉簿", exact: true })).toBeVisible();
});

async function cashBrowserForms(page: Page) {
  type Cash = components["schemas"]["CashEntry"];
  type Expense = components["schemas"]["Expense"];
  await page.setViewportSize({ width: 360, height: 900 });
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => {
    if (message.type() === "error" && /Content Security Policy|violates.*directive|Refused to apply/i.test(message.text())) errors.push(message.text());
  });
  // Continue the authenticated journey without repeated logins or weakened production limits.
  await page.goto("/settings/accounts");
  const { headers, post } = await signedRequests(page);
  const portfolio = await post<components["schemas"]["Portfolio"]>("portfolios", { name: "E2E cash form portfolio" });
  const broker = await post<components["schemas"]["Broker"]>("brokers", { name: "E2E cash form broker" });
  const account = await post<components["schemas"]["Account"]>("accounts", {
    portfolio_id: portfolio.id, broker_id: broker.id, name: "E2E cash form account", default_currency: "USD",
    opening_cash: "1000", opening_cash_date: "2026-01-01", opening_fx_rate_hkd: "7.8", cash_completeness: "COMPLETE",
  });
  const journalURL = `/cash?account_id=${account.id}`;
  const expenseURL = `/cash/expenses?account_id=${account.id}`;
  const walletURL = `/cash/accounts/${account.id}`;
  async function enterMovement(mode: "cash" | "expense", amount: string, currency: "USD" | "HKD", day: string, kindOrCategory: string, rate = "", explanation = "") {
    const title = mode === "cash" ? "新增現金紀錄" : "新增費用紀錄";
    await page.getByRole("button", { name: title, exact: true }).click();
    const form = page.getByRole("form", { name: title, exact: true });
    await form.getByLabel("入帳帳戶", { exact: true }).selectOption(account.id);
    await form.getByLabel("實際原幣", { exact: true }).selectOption(currency);
    await form.getByLabel("實際入帳日期").fill(day);
    await form.getByLabel("實際原幣金額").fill(amount);
    if (mode === "cash") await form.getByLabel("現金分類").selectOption(kindOrCategory);
    else await form.getByLabel("費用分類").fill(kindOrCategory);
    if (currency === "USD") await form.getByLabel("現金歷史匯率（1 USD = HKD）").fill(rate);
    await form.getByLabel("入帳／更正依據").fill(explanation);
    return form;
  }
  const actualUSD = () => page.getByLabel("USD 錢包", { exact: true }).getByText("可確認的實際餘額", { exact: true }).locator("..");
  const recordedUSD = () => page.getByLabel("USD 錢包", { exact: true }).getByText("已記錄原幣淨額（含沖銷）", { exact: true }).locator("..");
  async function confirmVoid(mode: "cash" | "expense") {
    await page.getByLabel("作廢入帳依據").fill("Void synthetic reviewed native movement");
    await page.getByRole("button", { name: mode === "cash" ? "確認作廢現金紀錄" : "確認作廢費用紀錄", exact: true }).click();
    await expect(page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true })).toBeFocused();
    await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
    await expect(page.getByText("已作廢並精確沖銷；原始紀錄與審計仍保留。", { exact: true })).toBeVisible();
  }

  await page.goto(journalURL);
  await expect(page.getByRole("heading", { name: "現金紀錄", exact: true })).toBeVisible();
  const initialOpening = page.getByRole("article", { name: "2026-01-01 USD 1000.000000", exact: true });
  await expect(initialOpening).toContainText("來源流程管理");
  await expect(initialOpening.getByRole("button", { name: "更正現金紀錄", exact: true })).toHaveCount(0);
  let form = await enterMovement("cash", "10.000001", "USD", "2026-01-02", "DEPOSIT");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `.local/screenshots/cash-form-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(page.getByText("已儲存原幣事實；歷史 HKD 仍未知，沒有採用預設匯率。", { exact: true })).toBeVisible();
  const deposit = page.getByRole("article", { name: "2026-01-02 USD 10.000001", exact: true });
  await expect(deposit).toContainText("未知，不是零");
  await deposit.getByRole("button", { name: "更正現金紀錄", exact: true }).click();
  form = page.getByRole("form", { name: "更正現金紀錄", exact: true });
  await form.getByLabel("實際原幣金額").fill("12.000001");
  await form.getByLabel("現金歷史匯率（1 USD = HKD）").fill("7.9");
  await form.getByLabel("入帳／更正依據").fill("Adopt reviewed historical deposit evidence");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(page.getByRole("article", { name: "2026-01-02 USD 12.000001", exact: true })).toContainText("94.800008");
  await expect(deposit).toContainText("已被新修訂取代");

  await page.goto(expenseURL);
  form = await enterMovement("expense", "2.000001", "USD", "2026-01-03", "Custody charge", "7.8");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const charge = page.getByRole("article", { name: "2026-01-03 USD 2.000001", exact: true });
  await expect(charge).toContainText("15.600008");
  await charge.getByRole("button", { name: "更正費用紀錄", exact: true }).click();
  form = page.getByRole("form", { name: "更正費用紀錄", exact: true });
  await expect(form.getByLabel("沿用／指定 HKD 金額（選填）")).toHaveValue("15.600008");
  await form.getByLabel("入帳備註").fill("Corrected note with original booking retained");
  await form.getByLabel("入帳／更正依據").fill("Retain the original historical booked value");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(charge).toContainText("15.600008");
  form = await enterMovement("expense", "-0.500001", "USD", "2026-01-03", "Custody refund", "7.8");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("退款");
  await form.getByLabel("入帳／更正依據").fill("Synthetic explicit operating fee refund");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: ".local/screenshots/expense-refund-dark.png", fullPage: true });
  await page.emulateMedia({ colorScheme: "light" });
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await page.getByRole("article", { name: "2026-01-03 USD -0.500001", exact: true }).getByRole("button", { name: "作廢費用紀錄", exact: true }).click();
  await confirmVoid("expense");

  await page.goto(journalURL);
  form = await enterMovement("cash", "-0.000001", "USD", "2026-01-04", "WITHDRAW");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await page.getByRole("article", { name: "2026-01-04 USD -0.000001", exact: true }).getByRole("button", { name: "作廢現金紀錄", exact: true }).click();
  await confirmVoid("cash");
  form = await enterMovement("cash", "0.000001", "HKD", "2026-01-03", "ADJUSTMENT", "", "Synthetic independent HKD micro adjustment");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);

  await page.goto(walletURL);
  await expect(actualUSD()).toContainText("USD 1010.000000");
  await page.getByRole("button", { name: "設定／更正期初", exact: true }).click();
  form = page.getByRole("form", { name: "設定／更正期初斷言", exact: true });
  await form.getByLabel("期初餘額（USD）").fill("1500");
  await form.getByLabel("期初生效日期").fill("2026-01-05");
  await form.getByLabel("期初歷史匯率（1 USD = HKD）").fill("7.9");
  await form.getByLabel("期初更正依據").fill("Synthetic relocated opening statement");
  // Account metadata and opening corrections intentionally share one CAS version namespace.
  const renamed = await page.request.patch(`/api/v1/accounts/${account.id}`, { headers: headers(1), data: { name: "E2E independently renamed cash wallet" } });
  expect(renamed.status()).toBe(200);
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("不會覆蓋新版");
  await expect(form.getByLabel("期初餘額（USD）")).toHaveValue("1500");
  await form.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "設定／更正期初", exact: true }).click();
  form = page.getByRole("form", { name: "設定／更正期初斷言", exact: true });
  await form.getByLabel("期初餘額（USD）").fill("1500");
  await form.getByLabel("期初生效日期").fill("2026-01-05");
  await form.getByLabel("期初歷史匯率（1 USD = HKD）").fill("7.9");
  await form.getByLabel("期初更正依據").fill("Synthetic reviewed replacement after conflict");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(actualUSD()).toContainText("USD 1500.000000");
  await page.getByRole("button", { name: "撤回期初斷言", exact: true }).click();
  await page.getByLabel("撤回期初依據").fill("Withdraw synthetic unsupported opening evidence");
  await page.getByRole("button", { name: "確認撤回期初斷言", exact: true }).click();
  await expect(page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true })).toBeFocused();
  await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(recordedUSD()).toContainText("USD 10.000000");
  await expect(actualUSD()).toContainText("未知，不是零");
  await page.getByRole("button", { name: "設定／更正期初", exact: true }).click();
  form = page.getByRole("form", { name: "設定／更正期初斷言", exact: true });
  await form.getByLabel("期初餘額（USD）").fill("0");
  await form.getByLabel("期初生效日期").fill("2026-01-10");
  await form.getByLabel("期初更正依據").fill("Synthetic explicit known zero statement");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("即使金額為零");
  await form.getByLabel("期初歷史匯率（1 USD = HKD）").fill("8");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(recordedUSD()).toContainText("USD 0.000000");
  await expect(actualUSD()).toContainText("未知，不是零");
  await page.getByRole("button", { name: "設定現金完整性", exact: true }).click();
  form = page.getByRole("form", { name: "設定現金完整性", exact: true });
  await form.getByLabel("帳戶現金完整性").selectOption("COMPLETE");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("必須明確確認");
  await form.getByLabel("我已核對所有原幣、現金歷史及期初來源").check();
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(actualUSD()).toContainText("USD 0.000000");
  await expect(page.getByLabel("USD 錢包", { exact: true })).toContainText("HKD -0.800000");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `.local/screenshots/wallet-opening-${width}.png`, fullPage: true });
  }
  const journal = await (await page.request.get("/api/v1/cash-entries", { params: { account_id: account.id, limit: 100 } })).json() as { items: Cash[] };
  expect(journal.items).toHaveLength(19);
  expect(journal.items.filter(row => row.entry_role === "REVERSAL")).toHaveLength(7);
  expect(journal.items.some(row => row.amount === "10.000001" && row.fx_rate_hkd === null && row.booked_hkd === null && row.status === "SUPERSEDED")).toBe(true);
  const expenses = await (await page.request.get("/api/v1/expenses", { params: { account_id: account.id, include_history: true } })).json() as { items: Expense[] };
  expect(expenses.items).toHaveLength(3);
  expect(expenses.items.filter(row => row.amount === "2.000001").every(row => row.hkd_amount === "15.600008")).toBe(true);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(errors).toEqual([]);
}

async function transferBrowserForms(page: Page) {
  type CashTransfer = components["schemas"]["CashTransfer"];
  type StockTransfer = components["schemas"]["SecurityTransfer"];
  type Balances = components["schemas"]["AccountCashBalances"];
  await page.setViewportSize({ width: 360, height: 900 });
  await page.goto("/settings/accounts");
  const { headers, post } = await signedRequests(page);
  const portfolio = await post<components["schemas"]["Portfolio"]>("portfolios", { name: "E2E transfer form portfolio" });
  const broker = await post<components["schemas"]["Broker"]>("brokers", { name: "E2E transfer form broker" });
  const accounts = [];
  for (const name of ["E2E transfer form source", "E2E transfer form target"]) accounts.push(await post<components["schemas"]["Account"]>("accounts", {
    portfolio_id: portfolio.id, broker_id: broker.id, name, default_currency: "USD", opening_cash: "2000", opening_cash_date: "2026-01-01", opening_fx_rate_hkd: "7.8", cash_completeness: "COMPLETE",
  }));
  const [source, target] = [accounts[0]!, accounts[1]!];
  const security = await post<components["schemas"]["Security"]>("securities", { exchange_code: "NASDAQ", display_ticker: "UXFR", name: "E2E transfer form security", quote_currency: "USD" });
  async function selectPortfolioFilter(mode: "cash" | "security") {
    const title = mode === "cash" ? "成對現金轉帳" : "股票轉倉";
    await expect(page.getByRole("heading", { name: title, level: 1, exact: true })).toBeVisible();
    await page.getByText(`篩選${title}`, { exact: true }).click();
    await page.getByLabel("轉倉篩選組合", { exact: true }).selectOption(portfolio.id);
    await page.getByText(`篩選${title}`, { exact: true }).click();
  }
  async function enterTransfer(mode: "cash" | "security", targetId = target.id) {
    const title = `新增${mode === "cash" ? "成對現金轉帳" : "股票轉倉"}`;
    await page.getByRole("button", { name: title, exact: true }).click();
    const form = page.getByRole("form", { name: title, exact: true });
    await form.getByLabel("轉倉組合", { exact: true }).selectOption(portfolio.id);
    await form.getByLabel("來源帳戶", { exact: true }).selectOption(source.id);
    await form.getByLabel("目標帳戶", { exact: true }).selectOption(targetId);
    await form.getByLabel("轉倉生效日期").fill("2026-01-03");
    return form;
  }
  async function voidTransfer(mode: "cash" | "security") {
    const title = mode === "cash" ? "成對現金轉帳" : "股票轉倉";
    await page.getByLabel("轉倉作廢依據").fill("Void reviewed synthetic transfer evidence");
    await page.getByRole("button", { name: `確認作廢${title}`, exact: true }).click();
    await expect(page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true })).toBeFocused();
    await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);
  }
  await page.goto("/cash/transfers");
  await selectPortfolioFilter("cash");
  let form = await enterTransfer("cash");
  await form.getByLabel("來源實際幣別").selectOption("USD");
  await form.getByLabel("目標實際幣別").selectOption("USD");
  await form.getByLabel("來源本金").fill("100.000001");
  await form.getByLabel("目標本金").fill("99");
  await form.getByLabel("轉倉依據").fill("Synthetic reviewed internal cash principal");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("同幣本金必須相等");
  await form.getByLabel("目標本金").fill("100.000001");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `.local/screenshots/paired-transfer-form-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const original = page.getByRole("article", { name: "2026-01-03 USD 100.000001 USD 100.000001", exact: true });
  await expect(original).toContainText("歷史 FX／HKD 未知");
  await original.getByRole("button", { name: "更正成對現金轉帳", exact: true }).click();
  form = page.getByRole("form", { name: "更正成對現金轉帳", exact: true });
  await form.getByLabel("來源本金").fill("120.000001");
  await form.getByLabel("目標本金").fill("120.000001");
  await form.getByLabel("轉帳歷史匯率（1 USD = HKD）").fill("7.9");
  await form.getByLabel("轉倉依據").fill("Adopt synthetic reviewed paired cash evidence");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const pair = page.getByRole("article", { name: "2026-01-03 USD 120.000001 USD 120.000001", exact: true });
  await expect(pair).toContainText("948.000008");
  await pair.getByRole("button", { name: "查看轉倉修訂鏈", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(2);
  await expect(original).toContainText("已被新修訂取代");
  await expect(original.getByRole("button", { name: "作廢成對現金轉帳", exact: true })).toHaveCount(0);
  await pair.getByRole("button", { name: "作廢成對現金轉帳", exact: true }).click();
  await voidTransfer("cash");
  await page.getByRole("button", { name: "顯示全部轉倉事件", exact: true }).click();
  form = await enterTransfer("cash", source.id);
  await form.getByLabel("來源實際幣別").selectOption("USD");
  await form.getByLabel("目標實際幣別").selectOption("HKD");
  await form.getByLabel("來源本金").fill("1");
  await form.getByLabel("目標本金").fill("7.81");
  await form.getByLabel("轉帳歷史匯率（1 USD = HKD）").fill("7.8");
  await form.getByLabel("轉倉依據").fill("Synthetic explicit same-account currency conversion");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const conversion = page.getByRole("article", { name: "2026-01-03 USD 1.000000 HKD 7.810000", exact: true });
  await expect(conversion).toContainText("歷史入帳差額需要覆核，不是已確認匯兌損益");
  await expect(conversion).toContainText("HKD 0.010000");
  await conversion.getByRole("button", { name: "作廢成對現金轉帳", exact: true }).click();
  await voidTransfer("cash");

  await post<components["schemas"]["TradeRecord"]>("trades", { portfolio_id: portfolio.id, account_id: source.id, security_id: security.id, side: "BUY", trade_date: "2026-01-02", quantity: "10", price: "100", fee: "1", currency: "USD", fx_rate_hkd: "7.8" });
  const before = await (await page.request.get(`/api/v1/accounts/${source.id}/cash-balances`)).json() as Balances;
  await page.goto("/transfers");
  await selectPortfolioFilter("security");
  form = await enterTransfer("security");
  await form.getByLabel("轉倉證券", { exact: true }).selectOption(security.id);
  await form.getByLabel("轉倉股數").fill("4");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: ".local/screenshots/security-transfer-form-dark.png", fullPage: true });
  await page.emulateMedia({ colorScheme: "light" });
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const originalStock = page.getByRole("article", { name: "2026-01-03 4.00000000 股轉倉", exact: true });
  await expect(originalStock).toContainText("3123.120000");
  const after = await (await page.request.get(`/api/v1/accounts/${source.id}/cash-balances`)).json() as Balances;
  expect(after).toEqual(before);
  const sale = await post<components["schemas"]["TradeRecord"]>("trades", { portfolio_id: portfolio.id, account_id: target.id, security_id: security.id, side: "SELL", trade_date: "2026-01-04", quantity: "2", price: "120", fee: "1", currency: "USD", fx_rate_hkd: "7.8" });
  await originalStock.getByRole("button", { name: "作廢股票轉倉", exact: true }).click();
  await page.getByLabel("轉倉作廢依據").fill("Synthetic orphaned destination sale test");
  await page.getByRole("button", { name: "確認作廢股票轉倉", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
  await expect(page.getByRole("alertdialog").getByRole("alert")).toContainText("持倉不足");
  await page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "取消操作", exact: true }).click();
  await originalStock.getByRole("button", { name: "更正股票轉倉", exact: true }).click();
  form = page.getByRole("form", { name: "更正股票轉倉", exact: true });
  await form.getByLabel("轉倉股數").fill("5");
  await form.getByLabel("轉倉依據").fill("Synthetic corrected custodian movement");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const revisedStock = page.getByRole("article", { name: "2026-01-03 5.00000000 股轉倉", exact: true });
  await expect(revisedStock).toContainText("3903.900000");
  await revisedStock.getByRole("button", { name: "查看轉倉修訂鏈", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(2);
  await expect(originalStock).toContainText("3123.120000");
  await expect(originalStock).toContainText("已被新修訂取代");
  const saleVoid = await page.request.delete(`/api/v1/trades/${sale.id}`, { headers: headers(1), data: { reason: "Void synthetic destination sale before reversing transfer" } });
  expect(saleVoid.status()).toBe(200);
  await revisedStock.getByRole("button", { name: "作廢股票轉倉", exact: true }).click();
  await voidTransfer("security");
  await page.setViewportSize({ width: 360, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: ".local/screenshots/security-transfer-history-360.png", fullPage: true });
  const cashHistory = await (await page.request.get("/api/v1/cash-transfers", { params: { portfolio_id: portfolio.id, include_history: true } })).json() as { items: CashTransfer[] };
  expect(cashHistory.items).toHaveLength(3);
  expect(cashHistory.items.some(row => row.source_amount === "100.000001" && row.fx_rate_hkd === null && row.source_booked_hkd === null)).toBe(true);
  const stockHistory = await (await page.request.get("/api/v1/security-transfers", { params: { portfolio_id: portfolio.id, include_history: true } })).json() as { items: StockTransfer[] };
  expect(stockHistory.items).toHaveLength(2);
  expect(stockHistory.items[0]!.root_id).toBe(stockHistory.items[1]!.root_id);
  expect(stockHistory.items[0]!.created_sequence).toBe(stockHistory.items[1]!.created_sequence);
  const currentSource = await (await page.request.get(`/api/v1/accounts/${source.id}/cash-balances`)).json() as Balances;
  const currentTarget = await (await page.request.get(`/api/v1/accounts/${target.id}/cash-balances`)).json() as Balances;
  expect(currentSource.balances.find(row => row.currency === "USD")!.balance).toBe("999.000000");
  expect(currentSource.balances.find(row => row.currency === "HKD")!.balance).toBe("0.000000");
  expect(currentTarget.balances[0]!.balance).toBe("2000.000000");
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
}

async function actionBrowserForms(page: Page) {
  type Action = components["schemas"]["CorporateAction"];
  await page.setViewportSize({ width: 360, height: 900 });
  await page.goto("/settings/accounts");
  const { headers, post } = await signedRequests(page);
  const portfolio = await post<components["schemas"]["Portfolio"]>("portfolios", { name: "E2E action form portfolio" });
  const broker = await post<components["schemas"]["Broker"]>("brokers", { name: "E2E action form broker" });
  const account = await post<components["schemas"]["Account"]>("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "E2E action form account", default_currency: "USD", opening_cash: "2000", opening_cash_date: "2026-01-01", opening_fx_rate_hkd: "7.8", cash_completeness: "COMPLETE" });
  const security = await post<components["schemas"]["Security"]>("securities", { exchange_code: "NASDAQ", display_ticker: "CAUI", name: "E2E action form security", quote_currency: "USD", identifier_effective_from: "2020-01-01" });
  await post<components["schemas"]["ProviderSymbol"]>(`securities/${security.id}/provider-symbols`, { provider_id: "yahoo", provider_symbol: "CAUI", effective_from: "2020-01-01" });
  const identifiersURL = `/api/v1/securities/${security.id}/identifiers`;
  const aliasesURL = `/api/v1/securities/${security.id}/provider-symbols`;
  const identifiersBefore: unknown = await (await page.request.get(identifiersURL)).json();
  const aliasesBefore: unknown = await (await page.request.get(aliasesURL)).json();
  const trade = { portfolio_id: portfolio.id, account_id: account.id, security_id: security.id, side: "BUY", trade_date: "2026-01-02", quantity: "10", price: "100", fee: "1", currency: "USD", fx_rate_hkd: "7.8" };
  await post<components["schemas"]["TradeRecord"]>("trades", trade);
  async function journal() { return await (await page.request.get("/api/v1/cash-entries", { params: { account_id: account.id, limit: 100 } })).json() as { items: components["schemas"]["CashEntry"][] }; }
  async function enterAction(kind: components["schemas"]["CorporateActionCreate"]["action_type"], day: string, status: "CONFIRMED" | "NEEDS_REVIEW") {
    await page.getByRole("button", { name: "新增企業行動", exact: true }).click();
    const form = page.getByRole("form", { name: "新增企業行動", exact: true });
    await form.getByLabel("企業行動證券", { exact: true }).selectOption(security.id);
    await form.getByLabel("企業行動類型").selectOption(kind);
    await form.getByLabel("企業行動生效日期").fill(day);
    await form.getByLabel("條款狀態").selectOption(status);
    await form.getByLabel("企業行動依據").fill("Synthetic reviewed corporate action notice");
    return form;
  }
  await page.goto("/corporate-actions");
  await expect(page.getByRole("heading", { name: "企業行動", exact: true, level: 1 })).toBeVisible();
  await page.getByText("篩選企業行動", { exact: true }).click();
  await page.getByLabel("企業行動篩選證券", { exact: true }).selectOption(security.id);
  await page.getByText("篩選企業行動", { exact: true }).click();
  const beforeSplit = await journal();
  let form = await enterAction("SPLIT", "2026-01-03", "CONFIRMED");
  await expect(form.getByLabel("比例分子")).toHaveValue("");
  await expect(form.getByLabel("比例分母")).toHaveValue("");
  await form.getByLabel("比例分子").fill("3");
  await form.getByLabel("比例分母").fill("2");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("請明確確認已核對條款");
  await form.getByLabel("我已核對條款，理解採納不等於完整估值或現金／成本分配").check();
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `.local/screenshots/action-form-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const original = page.getByRole("article", { name: "2026-01-03 SPLIT 3:2", exact: true });
  await expect(original).toContainText("已採納條款，不代表完整估值");
  expect(await journal()).toEqual(beforeSplit);
  const sale = await post<components["schemas"]["TradeRecord"]>("trades", { ...trade, side: "SELL", trade_date: "2026-01-04", quantity: "15", price: "120" });
  await original.getByRole("button", { name: "作廢企業行動", exact: true }).click();
  await page.getByLabel("企業行動作廢依據").fill("Synthetic dependent sale blocks split void");
  await page.getByRole("button", { name: "確認作廢企業行動", exact: true }).click();
  await expect(page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true })).toBeFocused();
  await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
  await expect(page.getByRole("alertdialog").getByRole("alert")).toContainText("持倉不足");
  await page.getByRole("alertdialog").getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "取消操作", exact: true }).click();
  await original.getByRole("button", { name: "更正企業行動", exact: true }).click();
  form = page.getByRole("form", { name: "更正企業行動", exact: true });
  await form.getByLabel("比例分子").fill("2");
  await form.getByLabel("比例分母").fill("1");
  await form.getByLabel("企業行動依據").fill("Synthetic corrected adopted two-for-one terms");
  await form.getByLabel("我已核對條款，理解採納不等於完整估值或現金／成本分配").check();
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const current = page.getByRole("article", { name: "2026-01-03 SPLIT 2:1", exact: true });
  await current.getByRole("button", { name: "查看企業行動修訂鏈", exact: true }).click();
  await expect(page.getByRole("article")).toHaveCount(2);
  await expect(original).toContainText("已被新修訂取代");
  await expect(original.getByRole("button", { name: "作廢企業行動", exact: true })).toHaveCount(0);
  const voidedSale = await page.request.delete(`/api/v1/trades/${sale.id}`, { headers: headers(1), data: { reason: "Void synthetic sale before allocation review" } });
  expect(voidedSale.status()).toBe(200);
  await page.getByRole("button", { name: "顯示全部行動事件", exact: true }).click();
  const beforeReview = await journal();
  form = await enterAction("MERGER", "2026-01-05", "NEEDS_REVIEW");
  await expect(form.getByRole("option", { name: "採納條款，不代表完整估值", exact: true })).toHaveCount(0);
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const merger = page.getByRole("article", { name: "2026-01-05 MERGER 未提供:未提供", exact: true });
  await expect(merger).toContainText("只保留覆核證據，未分配股數／成本／現金");
  expect(await journal()).toEqual(beforeReview);
  const blocked = await page.request.post("/api/v1/trades", { headers: headers(), data: { ...trade, trade_date: "2026-01-06", quantity: "1" } });
  expect(blocked.status()).toBe(409);
  expect((await blocked.json() as { code: string }).code).toBe("NEEDS_REVIEW");
  expect(await journal()).toEqual(beforeReview);
  await merger.getByRole("button", { name: "作廢企業行動", exact: true }).click();
  await page.getByLabel("企業行動作廢依據").fill("Withdraw synthetic unallocated merger evidence");
  await page.getByRole("button", { name: "確認作廢企業行動", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "確認", exact: true }).click();
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  form = await enterAction("SYMBOL_CHANGE", "2026-01-06", "CONFIRMED");
  await form.getByLabel("新顯示代號").fill("CA.UN");
  await form.getByLabel("證據網址（選填）").fill("https://example.test/dated-action-notice");
  await form.getByLabel("我已核對條款，理解採納不等於完整估值或現金／成本分配").check();
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const symbol = page.getByRole("article", { name: "2026-01-06 SYMBOL_CHANGE 未提供:未提供", exact: true });
  await expect(symbol).toContainText("只改目前顯示代號，保留穩定證券身分");
  await expect(symbol).toContainText("此穩定事件仍有未解決覆核");
  const renamed = await (await page.request.get(`/api/v1/securities/${security.id}`)).json() as components["schemas"]["Security"];
  expect(renamed.display_ticker).toBe("CA.UN");
  expect(renamed.id).toBe(security.id);
  expect(await (await page.request.get(identifiersURL)).json()).toEqual(identifiersBefore);
  expect(await (await page.request.get(aliasesURL)).json()).toEqual(aliasesBefore);
  expect(await journal()).toEqual(beforeReview);
  form = await enterAction("SYMBOL_CHANGE", "2050-01-01", "CONFIRMED");
  await form.getByLabel("新顯示代號").fill("FUT.CA");
  await form.getByLabel("我已核對條款，理解採納不等於完整估值或現金／成本分配").check();
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("未來的顯示代號只能保留待覆核");
  await expect(form.getByLabel("新顯示代號")).toHaveValue("FUT.CA");
  await form.getByLabel("條款狀態").selectOption("NEEDS_REVIEW");
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  form = await enterAction("SPLIT", "2050-01-03", "NEEDS_REVIEW");
  await form.getByLabel("比例分子").fill("9223372036854775807");
  await form.getByLabel("比例分母").fill("9223372036854775806");
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: ".local/screenshots/action-large-ratio-dark.png", fullPage: true });
  await page.emulateMedia({ colorScheme: "light" });
  await form.getByRole("button", { name: "儲存", exact: true }).click();
  await expect(form).toHaveCount(0);
  const large = page.getByRole("article", { name: "2050-01-03 SPLIT 9223372036854775807:9223372036854775806", exact: true });
  await expect(large).toContainText("9223372036854775807 : 9223372036854775806");
  await page.setViewportSize({ width: 360, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: ".local/screenshots/action-history-360.png", fullPage: true });
  const history = await (await page.request.get("/api/v1/corporate-actions", { params: { security_id: security.id, include_history: true } })).json() as { items: Action[] };
  expect(history.items).toHaveLength(6);
  expect(history.items.some(row => row.ratio_numerator === "9223372036854775807" && row.ratio_denominator === "9223372036854775806")).toBe(true);
  expect(await journal()).toEqual(beforeReview);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
}
