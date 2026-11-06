import { expect, type APIResponse, type Locator, type Page, type Route } from "@playwright/test";
import type { components } from "../../packages/contracts/src/schema";

type Schemas = components["schemas"];
type Post = <T>(path: string, data: object) => Promise<T>;

export async function sheetGeometry(page: Page, name: string, trap = false) {
  const sheet = page.getByRole("dialog", { name, exact: true });
  await expect(sheet).toBeVisible();
  const close = sheet.getByRole("button", { name: "關閉編輯", exact: true });
  await expect(close).toBeFocused();
  expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe("hidden");
  const bounds = (await sheet.boundingBox())!;
  const viewport = page.viewportSize()!;
  expect(bounds.y).toBe(0);
  expect(bounds.height).toBe(viewport.height);
  expect(bounds.x).toBeGreaterThanOrEqual(0);
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width);
  if (viewport.width >= 1024) {
    expect(bounds.x).toBeGreaterThan(0);
    expect(bounds.width).toBeLessThanOrEqual(768);
    // Outside clicks do not dismiss a financial editor or discard its unchanged intent.
    await page.mouse.click(10, 10);
    await expect(sheet).toBeVisible();
    await close.focus();
  } else {
    expect(bounds.x).toBe(0);
    expect(bounds.width).toBe(viewport.width);
  }
  if (trap) {
    await page.keyboard.press("Shift+Tab");
    await expect(sheet.getByRole("button", { name: "取消", exact: true })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(close).toBeFocused();
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  return sheet;
}

export async function cancelSheet(page: Page, sheet: Locator, opener: Locator, allowContentFallback = false) {
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
  if (allowContentFallback) {
    await expect.poll(async () => await opener.evaluate(node => node === document.activeElement) || await page.getByRole("main").evaluate(node => node === document.activeElement)).toBe(true);
  } else await expect(opener).toBeFocused();
  expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).not.toBe("hidden");
}

export async function workspaceBrowserForms(page: Page, post: Post) {
  const portfolio = await post<Schemas["Portfolio"]>("portfolios", { name: "E2E workspace portfolio" });
  const broker = await post<Schemas["Broker"]>("brokers", { name: "E2E workspace broker" });
  const accounts = [];
  for (const name of ["E2E workspace source", "E2E workspace target"]) accounts.push(await post<Schemas["Account"]>("accounts", {
    portfolio_id: portfolio.id, broker_id: broker.id, name, default_currency: "USD", opening_cash: "1000", opening_cash_date: "2026-01-01", opening_fx_rate_hkd: "7.8", cash_completeness: "COMPLETE",
  }));
  const [source, target] = [accounts[0]!, accounts[1]!];
  const security = await post<Schemas["Security"]>("securities", { exchange_code: "NASDAQ", display_ticker: "WSP", name: "E2E workspace security", quote_currency: "USD" });
  await post<Schemas["TradeRecord"]>("trades", { portfolio_id: portfolio.id, account_id: source.id, security_id: security.id, side: "BUY", trade_date: "2026-01-02", quantity: "10.00000001", price: "1.00000001", currency: "USD", fx_rate_hkd: "7.8", note: "Workspace precise trade" });
  await post<Schemas["CashEntry"]>("cash-entries", { account_id: source.id, effective_date: "2026-01-05", kind: "DEPOSIT", currency: "USD", amount: "12.000001", external_flow: true });
  await post<Schemas["CashEntry"]>("cash-entries", { account_id: source.id, effective_date: "2026-01-06", kind: "ADJUSTMENT", currency: "HKD", amount: "0.000001", reason: "Workspace independent HKD micro evidence" });
  await post<Schemas["Expense"]>("expenses", { account_id: source.id, date: "2026-01-03", category: "Workspace exact charge", amount: "2.000001", currency: "USD", fx_rate_hkd: "7.9" });
  await post<Schemas["CashTransfer"]>("cash-transfers", { portfolio_id: portfolio.id, source_account_id: source.id, target_account_id: target.id, transfer_date: "2026-01-04", source_currency: "USD", target_currency: "USD", source_amount: "3.000001", target_amount: "3.000001", reason: "Workspace paired principal without inferred FX" });
  await post<Schemas["SecurityTransfer"]>("security-transfers", { portfolio_id: portfolio.id, source_account_id: source.id, target_account_id: target.id, security_id: security.id, transfer_date: "2026-01-03", quantity: "1.00000001" });
  await post<Schemas["CorporateAction"]>("corporate-actions", { security_id: security.id, action_type: "SPLIT", effective_date: "2050-01-03", status: "NEEDS_REVIEW", ratio_numerator: "9223372036854775807", ratio_denominator: "9223372036854775806", reason: "Workspace exact future review terms, not valuation" });

  const cases = [
    { key: "trade", path: "/transactions", title: "交易紀錄", list: "交易", filter: "篩選交易", picker: "篩選組合", value: portfolio.id, expected: "10.00000001", field: "交易股數", input: "10.00000001", create: "新增交易", edit: "更正交易" },
    { key: "cash", path: `/cash?account_id=${source.id}`, title: "現金紀錄", list: "現金紀錄", expected: "USD 12.000001", field: "實際原幣金額", input: "12.000001", create: "新增現金紀錄", edit: "更正現金紀錄" },
    { key: "expense", path: `/cash/expenses?account_id=${source.id}`, title: "費用紀錄", list: "費用紀錄", expected: "USD 2.000001", field: "實際原幣金額", input: "2.000001", create: "新增費用紀錄", edit: "更正費用紀錄" },
    { key: "cash-transfer", path: "/cash/transfers", title: "成對現金轉帳", list: "成對現金轉帳", filter: "篩選成對現金轉帳", picker: "轉倉篩選組合", value: portfolio.id, expected: "USD 3.000001", field: "來源本金", input: "3.000001", create: "新增成對現金轉帳", edit: "更正成對現金轉帳" },
    { key: "stock-transfer", path: "/transfers", title: "股票轉倉", list: "股票轉倉", filter: "篩選股票轉倉", picker: "轉倉篩選組合", value: portfolio.id, expected: "1.00000001", field: "轉倉股數", input: "1.00000001", create: "新增股票轉倉", edit: "更正股票轉倉" },
    { key: "action", path: "/corporate-actions", title: "企業行動", list: "企業行動", filter: "篩選企業行動", picker: "企業行動篩選證券", value: security.id, expected: "9223372036854775807", field: "比例分母", input: "9223372036854775806", create: "新增企業行動", edit: "更正企業行動" },
  ];
  for (const item of cases) {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(item.path);
    await expect(page.getByRole("heading", { name: item.title, level: 1, exact: true })).toBeVisible();
    if (item.filter && item.picker && item.value) {
      await page.getByText(item.filter, { exact: true }).click();
      await page.getByLabel(item.picker, { exact: true }).selectOption(item.value);
      await page.getByText(item.filter, { exact: true }).click();
    }
    const table = page.getByRole("table");
    await expect(table).toBeVisible();
    await expect(table.getByRole("caption")).toContainText("非全部紀錄");
    await expect(table.getByRole("columnheader").first()).toHaveAttribute("aria-sort", "descending");
    const row = table.getByRole("row").filter({ hasText: item.expected }).first();
    await expect(row).toBeVisible();
    if (item.key === "cash") await expect(row).toContainText("未知，不是零");
    if (item.key === "action") await expect(row).toContainText("9223372036854775806");
    await page.screenshot({ path: `.local/screenshots/workspace-table-${item.key}-1440.png`, fullPage: true });
    const sidebar = page.getByRole("navigation", { name: "主要導覽", exact: true });
    await expect(sidebar.getByRole("link")).toHaveCount(7);
    await page.getByRole("button", { name: "收合導覽", exact: true }).click();
    await expect(page.getByRole("button", { name: "展開導覽", exact: true })).toHaveAttribute("aria-expanded", "false");
    await expect(sidebar.getByRole("link", { name: "交易紀錄", exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole("button", { name: "展開導覽", exact: true }).click();
    await row.getByRole("button", { name: "查看原值與操作", exact: true }).click();
    for (const width of [1440, 768, 360]) {
      await page.setViewportSize({ width, height: 900 });
      if (width < 1024) {
        await expect(table).toHaveCount(0);
        const bottom = page.getByRole("navigation", { name: "手機主要導覽", exact: true });
        await expect(bottom).toBeVisible();
        await expect(bottom.getByRole("link")).toHaveCount(5);
        const bounds = (await bottom.boundingBox())!;
        expect(bounds.y + bounds.height).toBe(900);
        for (const link of await bottom.getByRole("link").all()) {
          const touch = (await link.boundingBox())!;
          expect(touch.height).toBeGreaterThanOrEqual(44);
          expect(touch.width).toBeGreaterThanOrEqual(44);
        }
      }
      const create = page.getByRole("button", { name: item.create, exact: true });
      await create.click();
      let sheet = await sheetGeometry(page, item.create, item.key === "trade");
      await page.screenshot({ path: `.local/screenshots/workspace-sheet-${item.key}-${width}.png` });
      if (item.key === "trade" && width === 360) {
        await page.setViewportSize({ width: 360, height: 540 });
        const submit = sheet.getByRole("button", { name: "儲存", exact: true });
        await submit.scrollIntoViewIfNeeded();
        const touch = (await submit.boundingBox())!;
        expect(touch.y).toBeGreaterThanOrEqual(0);
        expect(touch.y + touch.height).toBeLessThanOrEqual(540);
        await page.setViewportSize({ width: 360, height: 900 });
      }
      await cancelSheet(page, sheet, create);
      const detail = page.getByRole("article").filter({ hasText: item.expected }).first();
      const edit = detail.getByRole("button", { name: item.edit, exact: true });
      await edit.click();
      sheet = await sheetGeometry(page, item.edit);
      await expect(sheet.getByLabel(item.field, { exact: true })).toHaveValue(item.input);
      if (item.key === "action" && width === 1440) {
        await page.emulateMedia({ colorScheme: "dark" });
        await page.screenshot({ path: ".local/screenshots/workspace-action-dark.png" });
        await page.emulateMedia({ colorScheme: "light" });
      }
      await cancelSheet(page, sheet, edit);
    }
  }

  await page.goto(`/cash/accounts/${source.id}`);
  for (const width of [1440, 360]) {
    await page.setViewportSize({ width, height: 900 });
    for (const name of ["設定／更正期初", "設定現金完整性"]) {
      const control = page.getByRole("button", { name, exact: true });
      await control.click();
      const sheet = await sheetGeometry(page, name, true);
      // The refetch may temporarily disable the opener; focus must still have a stable destination.
      await cancelSheet(page, sheet, control, true);
    }
  }

  // Route changes retain only view choices. Reloading starts a fresh private runtime.
  await page.goto("/transactions");
  await page.getByText("篩選交易", { exact: true }).click();
  await page.getByLabel("交易起日", { exact: true }).fill("2026-01-02");
  await page.getByLabel("包括已被取代的修訂").check();
  await page.getByLabel("搜尋交易").fill("Workspace");
  const navigation = page.getByRole("navigation", { name: "手機主要導覽", exact: true });
  await navigation.getByRole("link", { name: "現金帳", exact: true }).click();
  await expect(page.getByRole("heading", { name: "現金紀錄", level: 1, exact: true })).toBeVisible();
  await navigation.getByRole("link", { name: "交易紀錄", exact: true }).click();
  await expect(page.getByRole("main")).toBeFocused();
  await page.getByText("篩選交易", { exact: true }).click();
  await expect(page.getByLabel("交易起日", { exact: true })).toHaveValue("2026-01-02");
  await expect(page.getByLabel("包括已被取代的修訂")).toBeChecked();
  await expect(page.getByLabel("搜尋交易")).toHaveValue("Workspace");
  expect(new URL(page.url()).search).toBe("");
  await page.getByRole("button", { name: "清除此篩選：交易起日", exact: true }).click();
  await expect(page.getByLabel("交易起日", { exact: true })).toHaveValue("");
  await page.reload();
  await expect(page.getByLabel("搜尋交易")).toHaveValue("");
  await page.getByText("篩選交易", { exact: true }).click();
  await expect(page.getByLabel("包括已被取代的修訂")).not.toBeChecked();

  // Commit at the real server, but hold the response: closing cannot pretend to cancel that write.
  await page.goto(`/cash?account_id=${source.id}`);
  const journal = async () => await (await page.request.get("/api/v1/cash-entries", { params: { account_id: source.id, limit: 100 } })).json() as { items: Schemas["CashEntry"][]; next_cursor: string | null };
  const before = await journal();
  await page.getByRole("button", { name: "新增現金紀錄", exact: true }).click();
  const form = page.getByRole("form", { name: "新增現金紀錄", exact: true });
  await form.getByLabel("入帳帳戶", { exact: true }).selectOption(source.id);
  await form.getByLabel("實際原幣", { exact: true }).selectOption("USD");
  await form.getByLabel("現金分類").selectOption("DEPOSIT");
  await form.getByLabel("實際入帳日期").fill("2026-01-07");
  await form.getByLabel("實際原幣金額").fill("0.000003");
  let release!: () => void;
  const released = new Promise<void>(resolve => { release = resolve; });
  let committed!: (response: APIResponse) => void;
  let failed!: (error: unknown) => void;
  const response = new Promise<APIResponse>((resolve, reject) => { committed = resolve; failed = reject; });
  const hold = async (route: Route) => {
    if (route.request().method() !== "POST") return route.continue();
    try {
      const actual = await route.fetch(); committed(actual);
      await released; await route.fulfill({ response: actual });
    } catch (error) { failed(error); throw error; }
  };
  await page.route("**/api/v1/cash-entries", hold);
  try {
    await form.getByRole("button", { name: "儲存", exact: true }).click();
    const actual = await response;
    expect(actual.status()).toBe(201);
    expect(actual.headers()["cache-control"]).toBe("no-store");
    await expect(form).toHaveAttribute("aria-busy", "true");
    expect((await journal()).items).toHaveLength(before.items.length + 1);
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "關閉編輯", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "新增現金紀錄", exact: true })).toBeVisible();
    await expect(form.getByRole("button", { name: "取消", exact: true })).toBeDisabled();
    await expect(form.getByLabel("實際原幣金額")).toHaveValue("0.000003");
    await expect(page.getByText(/請等候回應後再關閉/)).toBeVisible();
    release();
    await expect(form).toHaveCount(0);
    await expect(page.getByRole("button", { name: "新增現金紀錄", exact: true })).toBeFocused();
  } finally { release(); await page.unroute("**/api/v1/cash-entries", hold); }
  const after = await journal();
  expect(after.items).toHaveLength(before.items.length + 1);
  expect(after.items.filter(row => row.amount === "0.000003")).toHaveLength(1);

  // A real 25-row keyset boundary stays server-owned; the UI never invents a total/page count.
  for (let index = 0; index < 26; index++) await post<Schemas["CashEntry"]>("cash-entries", {
    account_id: source.id, effective_date: "2026-01-08", kind: "ADJUSTMENT", currency: "HKD", amount: "0.000001", reason: "Workspace exact bounded pagination evidence", note: "Workspace server page boundary",
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.reload();
  const first = await (await page.request.get("/api/v1/cash-entries", { params: { account_id: source.id, limit: 25 } })).json() as { items: Schemas["CashEntry"][]; next_cursor: string };
  const physicalRows = page.locator("table tbody tr[data-record-id]");
  await expect(physicalRows).toHaveCount(25);
  expect(await physicalRows.evaluateAll(nodes => nodes.map(node => node.getAttribute("data-record-id")))).toEqual(first.items.map(row => row.id));
  await expect(page.getByText("第 1 頁；本頁 25 筆（非全部紀錄）", { exact: true })).toBeVisible();
  const second = await (await page.request.get("/api/v1/cash-entries", { params: { account_id: source.id, limit: 25, cursor: first.next_cursor } })).json() as { items: Schemas["CashEntry"][]; next_cursor: null };
  await page.getByRole("button", { name: "下一頁", exact: true }).click();
  await expect(physicalRows.first()).toHaveAttribute("data-record-id", second.items[0]!.id);
  expect(await physicalRows.evaluateAll(nodes => nodes.map(node => node.getAttribute("data-record-id")))).toEqual(second.items.map(row => row.id));
  await expect(page.getByRole("button", { name: "下一頁", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "上一頁", exact: true }).click();
  await expect(physicalRows.first()).toHaveAttribute("data-record-id", first.items[0]!.id);
  await page.screenshot({ path: ".local/screenshots/workspace-server-page-1440.png", fullPage: true });
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(async () => (await indexedDB.databases()).length + (await caches.keys()).length)).toBe(0);
}
