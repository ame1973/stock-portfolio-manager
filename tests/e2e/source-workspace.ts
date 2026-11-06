import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, type APIResponse, type Page, type Route } from "@playwright/test";
import type { components } from "../../packages/contracts/src/schema";
import { cancelSheet, sheetGeometry } from "./workspace";

type Schemas = components["schemas"];
type Post = <T>(path: string, data: object) => Promise<T>;
export async function sourceBrowserForms(page: Page, post: Post, portfolioId: string, accountId: string) {
  const stock = await post<Schemas["Security"]>("securities", { exchange_code: "NASDAQ", display_ticker: "SUI", name: "E2E source UI evidence", quote_currency: "USD", identifier_effective_from: "2020-01-01" });
  await post<Schemas["ProviderSymbol"]>(`securities/${stock.id}/provider-symbols`, { provider_id: "eodhd", provider_symbol: "SUI.US", effective_from: "2020-01-01" });
  async function read<T>(path: string, params: Record<string, string | number> = {}): Promise<T> {
    const response = await page.request.get(`/api/v1/${path}`, { params });
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
    return await response.json() as T;
  }
  const snapshot = async () => ({
    portfolio: await read<Schemas["Portfolio"]>(`portfolios/${portfolioId}`),
    cash: await read<Schemas["AccountCashBalances"]>(`accounts/${accountId}/cash-balances`),
    trades: await read<{ items: Schemas["TradeRecord"][] }>("trades", { portfolio_id: portfolioId, include_history: "true" }),
  });
  const before = await snapshot();
  await page.setViewportSize({ width: 360, height: 900 });
  await page.goto("/settings/portfolios");
  await page.getByRole("link", { name: "資料中心與來源擷取", exact: true }).click();
  await expect(page.getByRole("heading", { name: "來源設定與候選能力", exact: true })).toBeVisible();
  await expect(page.getByText(/已載入 8 個候選來源/)).toBeVisible();
  await expect(page.getByRole("button", { name: /匯入密鑰|新增排程|測試連線/ })).toHaveCount(0);
  await page.getByRole("button", { name: "查看EODHD診斷", exact: true }).click();
  await expect(page.getByText(/尚無已記錄來源頁面/)).toBeVisible();
  await page.screenshot({ path: ".local/screenshots/source-providers-360.png" });
  const tabs = page.getByRole("navigation", { name: "資料中心分類", exact: true });
  await tabs.getByRole("link", { name: "證券診斷", exact: true }).click();
  await page.getByLabel("診斷證券", { exact: true }).selectOption(stock.id);
  await expect(page.getByText(/未知 \/ 未知（NOT_ESTABLISHED）/).first()).toBeVisible();
  await tabs.getByRole("link", { name: "股息觀測", exact: true }).click();
  await expect(page.getByText(/尚無可顯示的 v1 來源觀測/)).toBeVisible();
  await tabs.getByRole("link", { name: "拆股因子", exact: true }).click();
  await expect(page.getByText(/尚無可顯示的 v1 來源觀測/)).toBeVisible();
  await tabs.getByRole("link", { name: "擷取工作", exact: true }).click();
  const create = page.getByRole("button", { name: "建立來源擷取工作", exact: true });
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await create.click();
    const sheet = await sheetGeometry(page, "建立來源擷取工作", width === 360);
    const form = sheet.getByRole("form");
    await expect(form.getByLabel("擷取起日", { exact: true })).toHaveValue("");
    await expect(form.getByRole("checkbox", { name: /^EODHD/ })).not.toBeChecked();
    await expect(form.getByLabel("擷取股息觀測")).toBeChecked();
    await expect(form.getByLabel("擷取拆股因子")).not.toBeChecked();
    for (const name of ["擷取起日", "擷取迄日", "擷取證券"]) {
      const bounds = (await form.getByLabel(name, { exact: true }).boundingBox())!;
      expect(bounds.height).toBeGreaterThanOrEqual(44);
      expect(bounds.height).toBeLessThanOrEqual(56);
    }
    await page.screenshot({ path: `.local/screenshots/source-sync-sheet-${width}.png` });
    await cancelSheet(page, sheet, create);
  }
  async function fill() {
    await create.click();
    const form = page.getByRole("form", { name: "建立來源擷取工作", exact: true });
    await form.getByLabel("擷取證券", { exact: true }).selectOption(stock.id);
    await form.getByRole("button", { name: "加入證券", exact: true }).click();
    await form.getByLabel("擷取起日").fill("2026-01-01");
    await form.getByLabel("擷取迄日").fill("2026-01-31");
    await form.getByRole("checkbox", { name: /^EODHD/ }).check();
    await form.getByRole("checkbox", { name: /我明白這只擷取來源觀測/ }).check();
    return form;
  }
  const form = await fill();
  let release!: () => void;
  const released = new Promise<void>(resolve => { release = resolve; });
  let committed!: (response: APIResponse) => void;
  let failed!: (error: unknown) => void;
  const response = new Promise<APIResponse>((resolve, reject) => { committed = resolve; failed = reject; });
  const hold = async (route: Route) => {
    if (route.request().method() !== "POST") return route.continue();
    try {
      expect(route.request().postDataJSON()).toEqual({ security_ids: [stock.id], provider_ids: ["eodhd"], from_date: "2026-01-01", to_date: "2026-01-31", operations: ["DIVIDENDS"], force: false });
      const actual = await route.fetch(); committed(actual);
      await released; await route.fulfill({ response: actual });
    } catch (error) { failed(error); throw error; }
  };
  await page.route("**/api/v1/dividends/sync", hold);
  try {
    await form.getByRole("button", { name: "建立擷取工作", exact: true }).click();
    const actual = await response;
    expect(actual.status()).toBe(202);
    expect(actual.headers()["cache-control"]).toBe("no-store");
    await expect(form).toHaveAttribute("aria-busy", "true");
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "關閉編輯", exact: true }).click();
    await expect(form).toBeVisible();
    await expect(form.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
    await expect(form.getByLabel("擷取起日")).toHaveValue("2026-01-01");
    release();
    await expect(form).toHaveCount(0);
    await expect(create).toBeFocused();
  } finally { release(); await page.unroute("**/api/v1/dividends/sync", hold); }
  await expect(page.getByText(/已接受來源擷取工作（HTTP 202）/)).toBeVisible();
  await page.getByRole("link", { name: /^查看已接受工作 / }).click();
  await expect(page.getByRole("heading", { name: "未能完成來源擷取", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "要求取消此工作", exact: true })).toBeDisabled();
  await expect(page.getByRole("table").getByRole("columnheader").first()).toHaveAttribute("aria-sort", "ascending");
  await expect(page.getByRole("table")).toContainText("未設定／未就緒；沒有自動啟用來源");
  await page.screenshot({ path: ".local/screenshots/source-unconfigured-job-1440.png", fullPage: true });

  const project = process.env.SPM_E2E_COMPOSE_PROJECT!;
  const compose = (args: string[]) => execFileSync("docker", ["compose", "-p", project, ...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 60_000 });
  compose(["stop", "worker"]);
  try {
    // A stopped real worker makes a queued cancellation deterministic without weakening leases/limits.
    await page.getByRole("link", { name: "返回來源工作列表", exact: true }).click();
    const cancelForm = await fill();
    await cancelForm.getByRole("button", { name: "建立擷取工作", exact: true }).click();
    await expect(cancelForm).toHaveCount(0);
    await page.getByRole("link", { name: /^查看已接受工作 / }).click();
    await page.getByRole("button", { name: "要求取消此工作", exact: true }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog.getByRole("button", { name: "取消", exact: true })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("button", { name: "要求取消此工作", exact: true })).toBeFocused();
    await page.getByRole("button", { name: "要求取消此工作", exact: true }).click();
    await dialog.getByRole("button", { name: "確認", exact: true }).click();
    await expect(page.getByText(/取消回覆：已停止未完成工作，保留既有來源/)).toBeVisible();
    await expect(page.getByRole("button", { name: "要求取消此工作", exact: true })).toBeDisabled();
    await tabs.getByRole("link", { name: "來源設定", exact: true }).click();
    const output = execFileSync("docker", [
      "run", "--rm", "--network", "none", "--read-only", "--memory", "512m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp",
      "--env", "SPM_SYNTHETIC_E2E=source-ui", "--env", "DATA_DIR=/data", "--env", `PUBLIC_ORIGIN=${process.env.SPM_E2E_BASE_URL!}`,
      "--mount", `type=volume,src=${project}_data,dst=/data`, "--mount", `type=bind,src=${resolve("tests/e2e/source_evidence.py")},dst=/tmp/source_evidence.py,readonly`,
      "spm-api:local", "python", "-I", "/tmp/source_evidence.py", stock.id,
    ], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 30_000 });
    expect(output.trim()).toBe("Synthetic adapter evidence: 3 mocked requests, 27 dividends, 2 provider-only splits; financial facts unchanged.");
  } finally { compose(["start", "worker"]); }

  // Non-empty views use actual normalized/persisted adapter fixtures, never mocked browser API responses.
  await tabs.getByRole("link", { name: "股息觀測", exact: true }).click();
  await page.getByText("篩選股息來源觀測", { exact: true }).click();
  await page.getByLabel("觀測證券", { exact: true }).selectOption(stock.id);
  await page.getByText("篩選股息來源觀測", { exact: true }).click();
  const first = await read<Schemas["ObservationPage_DividendObservationRecord_"]>("dividends/observations", { security_id: stock.id, limit: 25 });
  const second = await read<Schemas["ObservationPage_DividendObservationRecord_"]>("dividends/observations", { security_id: stock.id, limit: 25, cursor: first.next_cursor! });
  const physicalRows = page.locator("table tbody tr[data-record-id]");
  await expect(physicalRows).toHaveCount(25);
  expect(await physicalRows.evaluateAll(nodes => nodes.map(node => node.getAttribute("data-record-id")))).toEqual(first.items.map(row => row.id));
  await page.getByRole("button", { name: "下一頁", exact: true }).click();
  await expect(physicalRows).toHaveCount(2);
  expect(await physicalRows.evaluateAll(nodes => nodes.map(node => node.getAttribute("data-record-id")))).toEqual(second.items.map(row => row.id));
  await expect(page.getByRole("button", { name: "下一頁", exact: true })).toBeDisabled();
  const maximum = [...first.items, ...second.items].find(row => row.dps === "92233720368.54775807")!;
  expect(maximum.currency).toBeNull();
  expect(maximum.source_currency).toBe("EUR");
  expect(maximum.sightings_count).toBe(2);
  if (first.items.some(row => row.id === maximum.id)) await page.getByRole("button", { name: "上一頁", exact: true }).click();
  const maxRow = page.locator(`tr[data-record-id="${maximum.id}"]`);
  await expect(maxRow).toContainText("92233720368.54775807 幣別未知");
  await expect(page.getByRole("table").getByRole("caption")).toContainText("首次擷取時間／ID 倒序（非事件日期）");
  await maxRow.getByRole("button", { name: "查看原值與操作", exact: true }).click();
  await expect(page.getByText(maximum.normalized_sha256, { exact: true })).toBeVisible();
  await page.screenshot({ path: ".local/screenshots/source-dividends-1440.png", fullPage: true });
  for (const width of [768, 360]) {
    await page.setViewportSize({ width, height: 900 });
    // Wait for the responsive branch, not the soon-to-unmount desktop expansion.
    await expect(page.getByRole("table")).toHaveCount(0);
    const record = page.getByRole("article", { name: `SUI.US ${maximum.id}`, exact: true });
    await record.scrollIntoViewIfNeeded();
    await expect(record).toContainText("92233720368.54775807 幣別未知");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `.local/screenshots/source-dividends-${width}.png` });
  }
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: ".local/screenshots/source-dividends-dark.png" });
  await page.emulateMedia({ colorScheme: "light" });
  await tabs.getByRole("link", { name: "拆股因子", exact: true }).click();
  await expect(page.getByText("精確來源因子：9223372036854775807 : 9223372036854775806（未採納股數）", { exact: true })).toBeVisible();
  await expect(page.getByText("精確來源因子：1 : 3（未採納股數）", { exact: true })).toBeVisible();
  await page.screenshot({ path: ".local/screenshots/source-splits-360.png", fullPage: true });
  await tabs.getByRole("link", { name: "證券診斷", exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.getByLabel("診斷證券", { exact: true })).toHaveValue(stock.id);
  await page.getByRole("button", { name: "重新載入證券診斷", exact: true }).click();
  const coverageRow = page.locator(`tr[data-record-id="eodhd:${stock.id}"]`);
  await expect(coverageRow).toContainText("27");
  await expect(coverageRow).toContainText("未設定／不可用");
  await coverageRow.getByRole("button", { name: "查看原值與操作", exact: true }).click();
  await expect(page.getByText(/未知 \/ 未知（NOT_ESTABLISHED）/)).toBeVisible();
  await expect(page.getByText(/供應商帳單剩餘配額/)).toHaveCount(0);
  await page.screenshot({ path: ".local/screenshots/source-coverage-1440.png", fullPage: true });
  await tabs.getByRole("link", { name: "股息觀測", exact: true }).click();
  await page.getByText("篩選股息來源觀測", { exact: true }).click();
  await page.getByLabel("除淨起日", { exact: true }).fill("2026-01-20");
  await page.getByLabel("搜尋股息來源觀測").fill("SUI.US");
  await tabs.getByRole("link", { name: "拆股因子", exact: true }).click();
  await page.getByText("篩選provider-only 拆股因子", { exact: true }).click();
  await expect(page.getByLabel("生效起日")).toHaveValue("");
  await tabs.getByRole("link", { name: "股息觀測", exact: true }).click();
  await page.getByText("篩選股息來源觀測", { exact: true }).click();
  await expect(page.getByLabel("除淨起日", { exact: true })).toHaveValue("2026-01-20");
  await expect(page.getByLabel("搜尋股息來源觀測")).toHaveValue("SUI.US");
  expect(new URL(page.url()).search).toBe("");
  await page.getByRole("button", { name: "清除此篩選：除淨起日", exact: true }).click();
  await expect(page.getByLabel("除淨起日", { exact: true })).toHaveValue("");
  await page.reload();
  await expect(page.getByLabel("搜尋股息來源觀測")).toHaveValue("");
  const providers = await read<{ items: Schemas["ProviderInfo"][] }>("providers");
  expect(providers.items.every(row => !row.configured && row.live_validation === "NOT_TESTED")).toBe(true);
  expect(await snapshot()).toEqual(before);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(async () => (await indexedDB.databases()).length + (await caches.keys()).length)).toBe(0);
}
