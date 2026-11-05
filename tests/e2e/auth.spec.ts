import { execFileSync } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import type { components } from "../../packages/contracts/src/schema";

const PASSWORD = "synthetic-browser-passphrase-2026";
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
    await otherPage.reload();
    await expect(otherPage.getByRole("heading", { name: "登入持倉簿", exact: true })).toBeVisible();
    await signIn(otherPage);
    const otherSession = (await other.cookies()).find(cookie => cookie.name === "spm_session")!;
    await page.getByLabel("目前密碼", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "確認目前密碼", exact: true }).click();
    await expect(page.getByText(/已重新驗證/)).toBeVisible();
    const replacement = "synthetic-updated-browser-passphrase-2026";
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
  await signIn(page, "synthetic-updated-browser-passphrase-2026");
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
  await signIn(page, "synthetic-updated-browser-passphrase-2026");
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
