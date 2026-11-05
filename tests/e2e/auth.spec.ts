import { execFileSync } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";

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
