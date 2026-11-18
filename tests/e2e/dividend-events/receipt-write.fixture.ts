import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, stage, type Event } from "./helpers";

type Schemas = components["schemas"];

test("protected receipt API and form preserve exact scope and uncertain retries", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-receipt-write-${randomUUID()}`;
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
  expect(revised.status()).toBe(200); const event = await revised.json() as Event;
  stage("SEED");
  execFileSync("uv", ["run", "python", "-I", "tests/e2e/dividend-events/seed_receipts.py", event.id, account.id, first.current_revision_id, event.current_revision_id], { stdio: "ignore" });
  const base = `/api/v1/dividends/events/${event.id}/accounts/${account.id}/receipts`;
  const inputsResponse = await page.request.get(`${base}/inputs`);
  expect(inputsResponse.status()).toBe(200); expect(inputsResponse.headers()["cache-control"]).toBe("no-store");
  const inputs = await inputsResponse.json() as Schemas["ReceiptInputs"];
  expect(inputs.scope).toBe("RECORDED_RECEIPT_INPUTS_ONLY"); expect(inputs.eligibility_scope).toBe("NOT_ESTABLISHED");
  const entitlement = inputs.entitlements.items.find(item => item.revision_id === first.current_revision_id)!;
  expect(entitlement.scope).toBe("LEGACY_ENTITLEMENT_NOT_VERIFIED");
  const body: Schemas["ReceiptCreate"] = {
    receipt_id: randomUUID(), event_id: inputs.event_id, revision_id: inputs.revision_id,
    entitlement_id: entitlement.id, account_id: inputs.account_id, portfolio_id: inputs.portfolio_id, broker_id: inputs.broker_id,
    account_version: inputs.account_version, portfolio_version: inputs.portfolio_version, broker_version: inputs.broker_version,
    expected_data_revision: inputs.portfolio_data_revision, received_date: "2026-11-04", received_amount: "6.125", currency: "USD",
    tax: null, fee: "0", fx_rate_hkd: "7.8", fx_source: "Synthetic historical FX", actual_hkd: null, actual_hkd_reason: null,
    note: "Synthetic private receipt note", reason: "Synthetic recorded receipt from an explicit source.", confirmation: "RECORDED_CASH_ONLY",
  };
  const writeHeaders = await headers(page, event.version); const before = database();
  stage("COMMIT");
  const [left, right] = await Promise.all([
    page.request.post("/api/v1/dividends/receipts", { headers: writeHeaders, data: body }),
    page.request.post("/api/v1/dividends/receipts", { headers: writeHeaders, data: body }),
  ]);
  expect(left.status()).toBe(201); expect(right.status()).toBe(201);
  expect(left.headers()["cache-control"]).toBe("no-store"); expect(right.headers()["cache-control"]).toBe("no-store");
  const result = await left.json() as Schemas["ReceiptCreated"];
  expect(await right.json()).toEqual(result);
  expect(result).toMatchObject({ id: body.receipt_id, original_revision_id: first.current_revision_id,
    observed_revision_id: event.current_revision_id, received_amount: "6.125000", received_hkd: "47.775000", tax: null,
    fee: "0.000000", fx_rate_hkd: "7.800000000000", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED" });
  const after = database();
  for (const table of ["dividend_receipts", "cash_ledger_entries", "dividend_receipt_origins", "projection_invalidations", "idempotency_keys"]) {
    expect(after[table]).toHaveLength(before[table]!.length + 1);
  }
  expect(after.audit_events).toHaveLength(before.audit_events!.length + 3);
  const changed = new Set(["dividend_receipts", "cash_ledger_entries", "dividend_receipt_origins", "projection_invalidations", "idempotency_keys", "audit_events", "portfolios"]);
  for (const table of Object.keys(before)) if (!changed.has(table)) expect(after[table]).toEqual(before[table]);
  stage("REAUTH");
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  const beforeReplay = database();
  stage("REPLAY"); const replay = await page.request.post("/api/v1/dividends/receipts", { headers: writeHeaders, data: body });
  expect(replay.status()).toBe(201); expect(await replay.json()).toEqual(result); expect(database()).toEqual(beforeReplay);
  const newHeaders = await headers(page, event.version); const beforeStale = database();
  const stale = await page.request.post("/api/v1/dividends/receipts", { headers: newHeaders, data: { ...body, receipt_id: randomUUID() } });
  expect(stale.status()).toBe(409); expect(stale.headers()["cache-control"]).toBe("no-store");
  expect((await stale.json() as { code: string }).code).toBe("DIVIDEND_RECEIPT_SCOPE_CHANGED"); expect(database()).toEqual(beforeStale);
  const refreshed = await page.request.get(`${base}/inputs`); expect(refreshed.status()).toBe(200);
  const latest = await refreshed.json() as Schemas["ReceiptInputs"];
  const lostBody = { ...body, receipt_id: randomUUID(), expected_data_revision: latest.portfolio_data_revision };
  const lostHeaders = await headers(page, event.version); let lostResult: Schemas["ReceiptCreated"] | null = null;
  const routePath = "**/api/v1/dividends/receipts";
  await page.route(routePath, async route => {
    const response = await route.fetch(); expect(response.status()).toBe(201);
    lostResult = await response.json() as Schemas["ReceiptCreated"]; await route.abort("failed");
  });
  stage("LOST"); const beforeLost = database();
  try {
    const lost = await page.evaluate(async ({ intent, csrf, key, version }) => {
      try {
        await fetch("/api/v1/dividends/receipts", { method: "POST", headers: { "Content-Type": "application/json",
          "X-CSRF-Token": csrf, "Idempotency-Key": key, "If-Match": `"${version}"` }, body: JSON.stringify(intent) });
        return false;
      } catch { return true; }
    }, { intent: lostBody, csrf: lostHeaders["X-CSRF-Token"], key: lostHeaders["Idempotency-Key"], version: event.version });
    expect(lost).toBe(true);
  } finally { await page.unroute(routePath); }
  expect(lostResult).not.toBeNull(); const afterLost = database();
  for (const table of ["dividend_receipts", "cash_ledger_entries", "dividend_receipt_origins", "projection_invalidations", "idempotency_keys"]) {
    expect(afterLost[table]).toHaveLength(beforeLost[table]!.length + 1);
  }
  expect(afterLost.audit_events).toHaveLength(beforeLost.audit_events!.length + 3);
  for (const table of Object.keys(beforeLost)) if (!changed.has(table)) expect(afterLost[table]).toEqual(beforeLost[table]);
  const recovered = await page.request.post("/api/v1/dividends/receipts", { headers: lostHeaders, data: lostBody });
  expect(recovered.status()).toBe(201); expect(await recovered.json()).toEqual(lostResult); expect(database()).toEqual(afterLost);

  async function openForm() {
    await page.setViewportSize({ width: 360, height: 900 }); await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
    await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    await article.getByRole("button", { name: "查看帳戶收款紀錄" }).click();
    const panel = article.getByRole("region", { name: "已記錄股息收款" });
    await panel.getByRole("button", { name: "選擇收款紀錄帳戶 Synthetic receipt read account" }).click();
    await panel.getByRole("button", { name: "準備記錄收款" }).click();
    const preparation = panel.getByRole("region", { name: "核對收款依據" });
    const selected = preparation.getByRole("button", { name: `選擇應收 ${entitlement.id} 並記錄收款` });
    await expect(selected).toBeEnabled(); await selected.click();
    const sheet = page.getByRole("dialog", { name: "記錄股息收款" });
    await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
    return { sheet, form: sheet.getByRole("form", { name: "記錄股息收款" }) };
  }
  async function fillForm(form: ReturnType<typeof page.getByRole>) {
    await form.getByLabel("實收日期").fill("2026-11-04"); await form.getByLabel("實收幣別").selectOption("USD");
    await form.getByLabel("實收淨額").fill("6.125"); await form.getByLabel("實際稅額").selectOption("UNKNOWN");
    await form.getByLabel("實際費用").selectOption("PROVIDED"); await form.getByLabel("已記錄費用").fill("0");
    await form.getByLabel("港幣歷史匯率").selectOption("PROVIDED"); await form.getByLabel("USD 兌 HKD 匯率").fill("7.8");
    await form.getByLabel("港幣匯率來源").fill("Synthetic historical FX"); await form.getByLabel("收款依據").fill("Synthetic receipt form evidence.");
    await form.getByRole("checkbox", { name: /我明白這只是擁有人記錄的現金/ }).check();
  }

  stage("OPEN"); let { sheet, form } = await openForm();
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); const box = (await sheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.emulateMedia({ colorScheme: "dark" }); expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.emulateMedia({ colorScheme: "light" });
  stage("PASSWORD"); const passwordForm = sheet.getByRole("form", { name: "重新驗證" });
  await passwordForm.getByLabel("目前密碼").fill(password); await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm.getByRole("status")).toContainText("已重新驗證"); await fillForm(form);
  let release!: () => void; let committed!: () => void; const held = new Promise<void>(resolve => { release = resolve; });
  const saved = new Promise<void>(resolve => { committed = resolve; }); let uiAttempts = 0; let uiKey = ""; let retryKey = ""; let uiId = ""; let retryId = "";
  await page.route(routePath, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    uiAttempts++; const response = await route.fetch(); expect(response.status()).toBe(201);
    const sent = JSON.parse(route.request().postData()!) as Schemas["ReceiptCreate"];
    if (uiAttempts === 1) {
      uiKey = route.request().headers()["idempotency-key"]!; uiId = sent.receipt_id;
      expect(sent).toMatchObject({ entitlement_id: entitlement.id, tax: null, fee: "0", received_amount: "6.125", confirmation: "RECORDED_CASH_ONLY" });
      committed(); await held; await route.abort("failed");
    } else { retryKey = route.request().headers()["idempotency-key"]!; retryId = sent.receipt_id; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "記錄收款" }).click(); await saved;
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click(); await expect(sheet).toBeVisible();
  } finally { release(); }
  stage("LOST"); await expect(form.getByRole("alert")).toContainText("網絡連線中斷"); const beforeUiReplay = database();
  await form.getByLabel("實收淨額").fill("006.125000"); stage("REPLAY"); await form.getByRole("button", { name: "記錄收款" }).click();
  await expect(sheet).toHaveCount(0); await page.unroute(routePath); expect(uiAttempts).toBe(2); expect(retryKey).toBe(uiKey); expect(retryId).toBe(uiId);
  expect(database()).toEqual(beforeUiReplay); await expect(page.getByRole("status").filter({ hasText: "伺服器已回覆收款記錄" })).toBeVisible();

  stage("SECOND"); ({ sheet, form } = await openForm()); await fillForm(form);
  let changedAttempts = 0; let originalKey = ""; let changedKey = ""; let originalId = ""; let changedId = "";
  await page.route(routePath, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    changedAttempts++; const response = await route.fetch(); const sent = JSON.parse(route.request().postData()!) as Schemas["ReceiptCreate"];
    if (changedAttempts === 1) { expect(response.status()).toBe(201); originalKey = route.request().headers()["idempotency-key"]!; originalId = sent.receipt_id; await route.abort("failed"); }
    else { expect(response.status()).toBe(409); expect((await response.json() as { code: string }).code).toBe("DIVIDEND_RECEIPT_CONFLICT");
      changedKey = route.request().headers()["idempotency-key"]!; changedId = sent.receipt_id; await route.fulfill({ response }); }
  });
  await form.getByRole("button", { name: "記錄收款" }).click(); await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  const beforeChanged = database(); await form.getByLabel("實收淨額").fill("6.126"); await form.getByRole("button", { name: "記錄收款" }).click();
  await expect(form.getByRole("alert")).toContainText("此收款識別碼已使用或與原意圖衝突");
  await page.unroute(routePath); expect(changedAttempts).toBe(2); expect(changedKey).not.toBe(originalKey); expect(changedId).toBe(originalId);
  expect(database()).toEqual(beforeChanged); await sheet.getByRole("button", { name: "關閉編輯" }).click();

  stage("NAVIGATE"); ({ sheet, form } = await openForm()); stage("FILL"); await fillForm(form);
  stage("PASSWORD"); await form.getByLabel("實收幣別").selectOption("HKD");
  stage("REAUTH"); await form.getByRole("checkbox", { name: /另行指定有依據的實際港幣金額/ }).check();
  stage("REPLAY"); await form.getByLabel("實際港幣金額", { exact: true }).fill("6.125001");
  await form.getByLabel("實際港幣金額依據").fill("Synthetic identity override evidence.");
  stage("COMPARE"); const beforeHkd = database(); await form.getByRole("button", { name: "記錄收款" }).click();
  await expect(form.getByText("港幣實收與實際港幣金額必須相同。")).toBeVisible(); expect(database()).toEqual(beforeHkd);
  let hkdAttempts = 0; let hkdId = "";
  await page.route(routePath, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    hkdAttempts++; const sent = JSON.parse(route.request().postData()!) as Schemas["ReceiptCreate"]; hkdId = sent.receipt_id;
    expect(sent).toMatchObject({ currency: "HKD", received_amount: "6.125", fx_rate_hkd: null, fx_source: null,
      actual_hkd: "006.125000", actual_hkd_reason: "Synthetic identity override evidence." });
    stage("COMMIT"); const response = await route.fetch(); expect(response.status()).toBe(201);
    expect(await response.json()).toMatchObject({ currency: "HKD", received_hkd: "6.125000", fx_source: "IDENTITY", actual_hkd_override: true });
    stage("REPLAY"); await route.fulfill({ response });
  });
  stage("REAUTH"); await form.getByLabel("實際港幣金額", { exact: true }).fill("006.125000"); await form.getByRole("button", { name: "記錄收款" }).click();
  await expect(sheet).toHaveCount(0); await page.unroute(routePath); expect(hkdAttempts).toBe(1);
  stage("FINAL"); const afterHkd = database();
  for (const table of ["dividend_receipts", "cash_ledger_entries", "dividend_receipt_origins", "projection_invalidations", "idempotency_keys"]) {
    expect(afterHkd[table]).toHaveLength(beforeHkd[table]!.length + 1);
  }
  expect(afterHkd.audit_events).toHaveLength(beforeHkd.audit_events!.length + 3);
  const origin = afterHkd.dividend_receipt_origins!.map(row => JSON.parse(row) as unknown[]).find(row => row[0] === hkdId);
  expect(origin?.[13]).toBe("IDENTITY"); expect(origin?.[14]).toBe("Synthetic identity override evidence.");
  for (const table of Object.keys(beforeHkd)) if (!changed.has(table)) expect(afterHkd[table]).toEqual(beforeHkd[table]);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
