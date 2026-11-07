import { expect, type Page } from "@playwright/test";
import type { components } from "../../packages/contracts/src/schema";

type Schemas = components["schemas"];
type Profile = Schemas["ProviderProfileRecord"];
export async function providerSettingsBrowserForms(page: Page, headers: (version?: number) => Record<string, string>, password: string, portfolioId: string, accountId: string) {
  async function read<T>(path: string): Promise<T> { const response = await page.request.get(`/api/v1/${path}`); expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store"); return await response.json() as T; }
  const snapshot = async () => ({ portfolio: await read<Schemas["Portfolio"]>(`portfolios/${portfolioId}`), cash: await read<Schemas["AccountCashBalances"]>(`accounts/${accountId}/cash-balances`), trades: await read<Schemas["Page_TradeRecord_"]>(`trades?portfolio_id=${portfolioId}&include_history=true`), jobs: await read<Schemas["Page_SourceJob_"]>("jobs") });
  const before = await snapshot();
  let credentialRequests = 0;
  const countImports = (request: { url: () => string; method: () => string }) => { if (request.url().endsWith("/credential") && request.method() === "POST") credentialRequests++; };
  page.on("request", countImports);
  await page.goto("/settings/providers"); await expect(page.getByRole("heading", { name: "來源授權與金鑰", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "建立停用來源政策", exact: true }).click();
  const sheet = page.getByRole("dialog"); await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); await expect(sheet).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const bounds = await sheet.boundingBox(); expect(Math.abs(bounds!.width - (width < 1024 ? width : 768))).toBeLessThan(2);
    const field = await sheet.getByLabel("目前密碼").boundingBox(); expect(field!.height).toBeGreaterThanOrEqual(44); expect(field!.height).toBeLessThanOrEqual(56);
    // Password and credential controls are empty in every explicit screenshot.
    await expect(sheet.getByLabel("目前密碼")).toHaveValue(""); await page.screenshot({ path: `.local/screenshots/provider-policy-sheet-${width}.png`, fullPage: true });
  }
  await sheet.getByLabel("目前密碼").fill(password); await sheet.getByRole("button", { name: "確認目前密碼", exact: true }).click(); await expect(sheet.getByText(/已重新驗證/)).toBeVisible(); await expect(sheet.getByLabel("目前密碼")).toHaveValue("");
  let form = sheet.getByRole("form", { name: "建立停用來源政策" }); await form.getByLabel("政策來源").selectOption("eodhd"); await form.getByLabel("政策變更依據").fill("Explicit disabled EODHD policy from the real settings UI, not a test key.");
  await form.getByRole("button", { name: "儲存停用政策", exact: true }).click(); await expect(sheet).toHaveCount(0);
  expect(await read<Profile>("provider-profiles/eodhd")).toMatchObject({ version: 1, credential_reference_present: false, policy: { enabled: false, requests_per_minute: null } });
  await page.goto("/settings/providers/eodhd"); await page.getByRole("button", { name: "匯入或輪替私密金鑰" }).click();
  await expect(page.getByLabel("新私密金鑰")).toHaveValue(""); await page.screenshot({ path: ".local/screenshots/provider-empty-credential-sheet.png", fullPage: true });
  await page.getByLabel("新私密金鑰").evaluate(element => { const data = new DataTransfer(); data.setData("text/plain", "synthetic-invalid-paste\n"); element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true })); });
  await expect(page.getByRole("alert")).toHaveText(/已清空先前輸入/); await expect(page.getByLabel("新私密金鑰")).toHaveValue(""); await page.getByRole("button", { name: "取消", exact: true }).click();

  await page.goto("/settings/providers/yahoo"); const observed = await read<Profile>("provider-profiles/yahoo"); expect(observed.version).toBe(3);
  await page.getByRole("button", { name: "修訂、啟用或停用政策" }).click(); form = page.getByRole("form", { name: "修訂來源政策" });
  await form.getByLabel("政策變更依據").fill("Keep this observed policy input after a real second-writer conflict.");
  const other = await page.request.patch("/api/v1/provider-profiles/yahoo", { headers: headers(3), data: { policy: observed.policy, clear_credential: false, reason: "A genuine concurrent synthetic policy writer, not a response mock." } }); expect(other.status()).toBe(200);
  await form.getByRole("button", { name: "儲存政策修訂" }).click(); await expect(form.getByRole("alert")).toHaveText(/不會覆蓋新版/); await expect(form.getByLabel("政策變更依據")).toHaveValue("Keep this observed policy input after a real second-writer conflict.");
  await form.getByRole("button", { name: "取消", exact: true }).click(); await page.getByRole("button", { name: "重新載入目前政策與歷史" }).click();
  const current = page.getByRole("region", { name: "目前保存的個人政策" }); await expect(current.getByRole("heading", { name: "Yahoo：版本 4", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "修訂、啟用或停用政策" }).click(); form = page.getByRole("form", { name: "修訂來源政策" });
  await form.getByLabel("資料權利參考").fill("Reviewed synthetic UI reference, not verified licensing"); await form.getByLabel("資料權利依據").fill("Explicit synthetic browser policy review with zero upstream work.");
  await form.getByLabel("每分鐘軟體要求上限").fill("2"); await form.getByLabel("滾動 24 小時軟體要求上限").fill("20"); await form.getByLabel(/我已核對/).check(); await form.getByLabel(/啟用此個人來源政策/).check(); await form.getByLabel(/我明白這是來源存取授權/).check(); await form.getByLabel("政策變更依據").fill("Explicit future source access decision; no automatic job or successful probe.");
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; }); const saved = new Promise<void>(resolve => { committed = resolve; });
  await page.route("**/api/v1/provider-profiles/yahoo", async route => {
    if (route.request().method() !== "PATCH") { await route.continue(); return; }
    const response = await route.fetch(); expect(response.status()).toBe(200); committed(); await held; await route.fulfill({ response });
  });
  try {
    await form.getByRole("button", { name: "儲存政策修訂" }).click(); await saved;
    expect(await read<Profile>("provider-profiles/yahoo")).toMatchObject({ version: 5, policy: { enabled: true } });
    await page.keyboard.press("Escape"); await page.getByRole("button", { name: "關閉編輯" }).click(); await expect(page.getByRole("dialog")).toBeVisible(); await expect(form.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
  } finally { release(); }
  await expect(page.getByRole("dialog")).toHaveCount(0); await page.unroute("**/api/v1/provider-profiles/yahoo");
  await page.getByRole("button", { name: "修訂、啟用或停用政策" }).click(); form = page.getByRole("form", { name: "修訂來源政策" }); await form.getByLabel(/啟用此個人來源政策/).uncheck(); await form.getByLabel("政策變更依據").fill("Explicitly disable future source access without cancelling or deleting anything."); await form.getByRole("button", { name: "儲存政策修訂" }).click(); await expect(page.getByRole("dialog")).toHaveCount(0);
  let latest = await read<Profile>("provider-profiles/yahoo"); expect(latest).toMatchObject({ version: 6, policy: { enabled: false } });
  // These are actual owner policy revisions, not provider runs or mocked list responses.
  while (latest.version < 27) {
    const next = await page.request.patch("/api/v1/provider-profiles/yahoo", { headers: headers(latest.version), data: { policy: latest.policy, clear_credential: false, reason: `Reviewed synthetic history row ${latest.version + 1}; no provider access is enabled.` } });
    expect(next.status()).toBe(200); latest = await next.json() as Profile;
  }
  const history = await read<Schemas["Page_ProviderProfileRecord_"]>("provider-profiles/yahoo/revisions?limit=25"); expect(history.items).toHaveLength(25);
  const older = await read<Schemas["Page_ProviderProfileRecord_"]>(`provider-profiles/yahoo/revisions?limit=25&cursor=${encodeURIComponent(history.next_cursor!)}`); expect(older.items).toHaveLength(2);
  await page.getByRole("button", { name: "重新載入目前政策與歷史" }).click(); await expect(current.getByRole("heading", { name: "Yahoo：版本 27", exact: true })).toBeVisible();
  const region = page.getByRole("region", { name: "來源政策歷史列表" });
  const table = region.getByRole("region", { name: "來源政策歷史伺服器分頁表格" });
  const expected = [history.items.map(row => row.id), older.items.map(row => row.id)];
  for (let index = 0; index < 2; index++) {
    if (index) await region.getByRole("button", { name: "下一頁", exact: true }).click();
    await expect(table.locator("tr[data-record-id]")).toHaveCount(expected[index]!.length);
    expect(await table.locator("tr[data-record-id]").evaluateAll(rows => rows.map(row => row.getAttribute("data-record-id")))).toEqual(expected[index]);
  }
  await page.screenshot({ path: ".local/screenshots/provider-policy-history-1440.png", fullPage: true });
  await page.setViewportSize({ width: 360, height: 900 }); await expect(table).toHaveCount(0); await expect(region.locator("article[aria-label]")).toHaveCount(2);
  expect(await region.locator("article[aria-label]").evaluateAll(rows => rows.map(row => row.getAttribute("aria-label")))).toEqual(expected[1]);
  await page.emulateMedia({ colorScheme: "dark" }); await page.screenshot({ path: ".local/screenshots/provider-policy-history-dark-360.png", fullPage: true }); await page.emulateMedia({ colorScheme: "light" });
  await region.getByRole("searchbox", { name: "搜尋來源政策歷史" }).fill("Reviewed synthetic history row 27"); await expect(region.locator("article[aria-label]")).toHaveCount(1);
  expect(page.url()).toMatch(/\/settings\/providers\/yahoo$/); expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(credentialRequests).toBe(0); page.off("request", countImports); expect(await snapshot()).toEqual(before);
}
