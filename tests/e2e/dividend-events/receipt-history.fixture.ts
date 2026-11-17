import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, stage, type Event } from "./helpers";

type Schemas = components["schemas"];

test("recorded receipt history stays private and preserves original event revisions", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-receipt-read-${randomUUID()}`;
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
  const portfolio = await create("portfolios", { name: "Synthetic receipt read portfolio" });
  const broker = await create("brokers", { name: "Synthetic receipt read broker" });
  const account = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic receipt read account", default_currency: "USD" });
  const security = await create("securities", { exchange_code: "NASDAQ", display_ticker: "RECEIPTE2E", name: "Synthetic receipt read security", quote_currency: "USD" });
  const created = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "1", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: "Synthetic receipt event reference", source_url: null, reason: "Synthetic event decision for receipt history review.",
  } });
  expect(created.status()).toBe(201); const first = await created.json() as Event;
  const revised = await page.request.patch(`/api/v1/dividends/events/${first.id}`, { headers: await headers(page, first.version), data: {
    security_id: security.id, revision_id: first.current_revision_id, resolve_rejection_id: null,
    event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null, pay_date: null,
    dps: "1.1", currency: "USD", adjustment_basis: "AS_DECLARED", citation: "Synthetic later receipt event reference",
    source_url: null, reason: "Synthetic successor before seeding retained receipt history.", confirm_unlock: false, confirm_reopen: false,
  } });
  expect(revised.status()).toBe(200); const event = await revised.json() as Event; expect(event.version).toBe(2);
  stage("SEED");
  execFileSync("uv", ["run", "python", "-I", "tests/e2e/dividend-events/seed_receipts.py", event.id, account.id, first.current_revision_id, event.current_revision_id], { stdio: "ignore" });
  const base = `/api/v1/dividends/events/${event.id}/accounts/${account.id}/receipts`;
  const direct = await page.request.get(base); expect(direct.status()).toBe(200); expect(direct.headers()["cache-control"]).toBe("no-store");
  const received = await direct.json() as Schemas["Page_RecordedReceipt_"];
  expect(received.items).toHaveLength(3); expect(received.items.map(item => item.revision_no)).toEqual([2, 2, 1]);
  expect(received.items[0]?.cash_scope).toBe("MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED");
  expect(received.items[1]?.recorded_amount).toBe("9223372036854.775807");
  expect(received.items[1]?.recorded_tax).toBeNull(); expect(received.items[1]?.recorded_fee).toBeNull();
  expect(received.items[2]?.recorded_tax).toBe("0.000000"); expect(received.items[2]?.recorded_fee).toBe("0.000000");
  expect(received.items[2]?.cash_scope).toBe("MATCHING_CASH_RECORDED_UNVERIFIED");
  const before = database(); expect(before.dividend_receipts).toHaveLength(3); expect(before.dividend_entitlements).toHaveLength(2);
  expect(before.cash_ledger_entries).toHaveLength(3); expect(before.dividend_projection_versions).toEqual([]);
  expect(before.fx_rates).toEqual([]); expect(before.upstream_attempts).toEqual([]); expect(before.jobs).toEqual([]);
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
    if (width >= 1024) await page.getByRole("row").filter({ hasText: "1.10000000" }).getByRole("button", { name: "查看原值與操作" }).click();
    else await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    const accountRead = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/accounts" && response.request().method() === "GET");
    const open = article.getByRole("button", { name: "查看帳戶收款紀錄" }); await open.focus(); await page.keyboard.press("Enter");
    const panel = article.getByRole("region", { name: "已記錄股息收款" });
    const choose = panel.getByRole("button", { name: "選擇收款紀錄帳戶 Synthetic receipt read account" }); await expect(choose).toBeEnabled();
    expect((await accountRead).headers()["cache-control"]).toBe("no-store");
    const historyRead = page.waitForResponse(response => new URL(response.url()).pathname === base && response.request().method() === "GET");
    await choose.click(); expect((await historyRead).headers()["cache-control"]).toBe("no-store");
    const history = panel.getByRole("region", { name: "帳戶收款紀錄歷史" });
    await expect(history).toContainText("原事件修訂 1"); await expect(history).toContainText("原事件修訂 2");
    await expect(history).toContainText("USD 6.000000"); await expect(history).toContainText("7.800000000000");
    await expect(history).toContainText("HKD 9223372036854.775807"); await expect(history).toContainText("港幣金額未知");
    await expect(history).toContainText("記錄稅額未知，不是零"); await expect(history).toContainText("記錄稅額 USD 0.000000");
    await expect(history).toContainText("已記錄現金關聯，尚未驗證"); await expect(history).toContainText("已記錄現金沖銷關聯，尚未驗證");
    await expect(history).toContainText("歷史稽核及付款均未驗證"); await expect(history).toContainText("不同幣別及歷史版本不在此相加");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  stage("COMPARE"); expect(database()).toEqual(before);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
