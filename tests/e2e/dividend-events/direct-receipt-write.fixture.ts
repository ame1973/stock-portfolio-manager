import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, stage, type Event } from "./helpers";

type Schemas = components["schemas"];
const path = "/api/v1/dividends/direct-receipts";

test("direct receipt forms record and reverse explicit cash without a legacy entitlement", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-direct-receipt-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click(); await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  stage("MASTER");
  const create = async (resource: string, data: object) => {
    const response = await page.request.post(`/api/v1/${resource}`, { headers: await headers(page), data }); expect(response.status()).toBe(201);
    return await response.json() as { id: string };
  };
  const portfolio = await create("portfolios", { name: "Synthetic direct receipt portfolio" });
  const broker = await create("brokers", { name: "Synthetic direct receipt broker" });
  const account = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic direct receipt account", default_currency: "USD" });
  const security = await create("securities", { exchange_code: "NASDAQ", display_ticker: "DIRECTREC", name: "Synthetic direct receipt security", quote_currency: "USD" });
  const created = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "1", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: "Synthetic explicit direct receipt event", source_url: null, reason: "Synthetic event decision without a financial grant.",
  } });
  expect(created.status()).toBe(201); const event = await created.json() as Event;
  const base = `/api/v1/dividends/events/${event.id}/accounts/${account.id}/recorded-receipts`;
  const inputResponse = await page.request.get(`${base}/inputs`);
  expect(inputResponse.status()).toBe(200); expect(inputResponse.headers()["cache-control"]).toBe("no-store");
  const inputs = await inputResponse.json() as Schemas["DirectReceiptInputs"];
  const body: Schemas["DirectReceiptCreate"] = {
    receipt_id: randomUUID(), event_id: inputs.event_id, revision_id: inputs.revision_id, account_id: inputs.account_id,
    portfolio_id: inputs.portfolio_id, broker_id: inputs.broker_id, account_version: inputs.account_version,
    portfolio_version: inputs.portfolio_version, broker_version: inputs.broker_version, expected_data_revision: inputs.portfolio_data_revision,
    received_date: "2026-11-04", received_amount: "6.125", currency: "USD", tax: null, fee: "0", fx_rate_hkd: "7.8",
    fx_source: "Synthetic historical FX", actual_hkd: null, actual_hkd_reason: null, note: "Synthetic private direct receipt note",
    reason: "Synthetic explicit recorded cash without entitlement.", confirmation: "RECORDED_CASH_ONLY", confirm_revision_fence: true,
  };
  const writeHeaders = await headers(page, event.version); const before = database();
  for (const table of ["dividend_entitlements", "dividend_receipts", "dividend_direct_receipts", "dividend_projection_versions", "fx_rates", "jobs", "upstream_attempts"]) expect(before[table]).toEqual([]);
  stage("COMMIT");
  const [left, right] = await Promise.all([
    page.request.post(path, { headers: writeHeaders, data: body }),
    page.request.post(path, { headers: writeHeaders, data: body }),
  ]);
  expect(left.status()).toBe(201); expect(right.status()).toBe(201);
  expect(left.headers()["cache-control"]).toBe("no-store"); expect(right.headers()["cache-control"]).toBe("no-store");
  const result = await left.json() as Schemas["DirectReceiptCreated"]; expect(await right.json()).toEqual(result);
  expect(result).toMatchObject({ id: body.receipt_id, event_id: event.id, account_id: account.id, revision_id: event.current_revision_id,
    revision_no: event.version, receipt_basis: "DIRECT_EVENT_ACCOUNT", received_amount: "6.125000", received_hkd: "47.775000", tax: null,
    fee: "0.000000", fx_rate_hkd: "7.800000000000", actual_hkd: null, actual_hkd_reason: null,
    scope: "HISTORICAL_DIRECT_RECEIPT_CREATION_RESPONSE_ONLY", payment_scope: "OWNER_RECORDED_NOT_VERIFIED", eligibility_scope: "NOT_ESTABLISHED" });
  expect(result).not.toHaveProperty("entitlement_id");
  const after = database();
  const changed = new Set(["dividend_direct_receipts", "dividend_direct_receipt_origins", "cash_ledger_entries", "projection_invalidations", "idempotency_keys", "audit_events", "portfolios"]);
  for (const table of ["dividend_direct_receipts", "dividend_direct_receipt_origins", "cash_ledger_entries", "projection_invalidations", "idempotency_keys"]) {
    expect(after[table]).toHaveLength(before[table]!.length + 1);
  }
  expect(after.audit_events).toHaveLength(before.audit_events!.length + 3);
  for (const table of Object.keys(before)) if (!changed.has(table)) expect(after[table]).toEqual(before[table]);
  stage("REPLAY");
  const repeated = await page.request.post(path, { headers: writeHeaders, data: { ...body, received_amount: "006.1250", fx_rate_hkd: "07.800" } });
  expect(repeated.status()).toBe(201); expect(await repeated.json()).toEqual(result); expect(database()).toEqual(after);
  const stale = await page.request.post(path, { headers: await headers(page, event.version), data: { ...body, receipt_id: randomUUID() } });
  expect(stale.status()).toBe(409); expect(stale.headers()["cache-control"]).toBe("no-store");
  expect((await stale.json() as { code: string }).code).toBe("DIVIDEND_DIRECT_RECEIPT_SCOPE_CHANGED"); expect(database()).toEqual(after);
  const fresh = await page.request.get(`${base}/inputs`); expect(fresh.status()).toBe(200);
  const latest = await fresh.json() as Schemas["DirectReceiptInputs"];
  const lostBody = { ...body, receipt_id: randomUUID(), expected_data_revision: latest.portfolio_data_revision };
  const lostHeaders = await headers(page, event.version); let lostResult: Schemas["DirectReceiptCreated"] | null = null;
  const routePath = "**/api/v1/dividends/direct-receipts";
  await page.route(routePath, async route => {
    const response = await route.fetch(); expect(response.status()).toBe(201);
    lostResult = await response.json() as Schemas["DirectReceiptCreated"]; await route.abort("failed");
  });
  stage("LOST");
  try {
    expect(await page.evaluate(async ({ intent, csrf, key, version }) => {
      try {
        await fetch("/api/v1/dividends/direct-receipts", { method: "POST", headers: { "Content-Type": "application/json",
          "X-CSRF-Token": csrf, "Idempotency-Key": key, "If-Match": `"${version}"` }, body: JSON.stringify(intent) });
        return false;
      } catch { return true; }
    }, { intent: lostBody, csrf: lostHeaders["X-CSRF-Token"], key: lostHeaders["Idempotency-Key"], version: event.version })).toBe(true);
  } finally { await page.unroute(routePath); }
  expect(lostResult).not.toBeNull(); const afterLost = database();
  expect(afterLost.dividend_direct_receipts).toHaveLength(2); expect(afterLost.cash_ledger_entries).toHaveLength(after.cash_ledger_entries!.length + 1);
  expect(afterLost.audit_events).toHaveLength(after.audit_events!.length + 3);
  for (const table of Object.keys(after)) if (!changed.has(table)) expect(afterLost[table]).toEqual(after[table]);
  const recovered = await page.request.post(path, { headers: lostHeaders, data: lostBody });
  expect(recovered.status()).toBe(201); expect(await recovered.json()).toEqual(lostResult); expect(database()).toEqual(afterLost);
  const rejection = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, event.version), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic fenced direct receipt event",
    evidence_url: null, reason: "Synthetic rejection after retained direct financial history.",
  } });
  expect(rejection.status()).toBe(409); expect((await rejection.json() as { code: string }).code).toBe("DIVIDEND_EVENT_PROJECTION_REQUIRED");
  expect(database()).toEqual(afterLost);
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
    if (width >= 1024) await page.locator(`tr[data-record-id="${event.id}"]`).getByRole("button", { name: "查看原值與操作" }).click();
    else await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    await article.getByRole("button", { name: "查看帳戶收款紀錄" }).click();
    const receipts = article.getByRole("region", { name: "已記錄股息收款" });
    const read = page.waitForResponse(response => new URL(response.url()).pathname === base && response.request().method() === "GET");
    const choose = receipts.getByRole("button", { name: "選擇收款紀錄帳戶 Synthetic direct receipt account" }); await choose.focus(); await page.keyboard.press("Enter");
    const response = await read; expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    const history = receipts.getByRole("region", { name: "帳戶收款紀錄歷史" });
    await expect(history.getByRole("listitem")).toHaveCount(2); await expect(history).toContainText("USD 6.125000");
    await expect(history).toContainText("直接關聯事件與帳戶"); await expect(history).toContainText("保留稽核記錄相符，付款仍未驗證");
    await expect(history.getByRole("button", { name: /核對作廢收款/ })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  stage("COMPARE"); expect(database()).toEqual(afterLost);
  for (const id of [result.id, lostBody.receipt_id]) {
    const response = await page.request.get(`${base}/${id}/void-inputs`);
    expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    const review = await response.json() as Schemas["DirectReceiptVoidInputs"];
    expect(review.scope).toBe("DIRECT_RECORDED_RECEIPT_VOID_INPUTS_ONLY");
    expect(review.origin_portfolio_id).toBe(portfolio.id);
    expect(review.current).toMatchObject({ event_id: event.id, account_id: account.id, portfolio_id: portfolio.id,
      revision_id: event.current_revision_id, revision_no: event.version, account_version: inputs.account_version,
      portfolio_data_revision: (BigInt(inputs.portfolio_data_revision) + 2n).toString(),
      receipt_basis: "DIRECT_EVENT_ACCOUNT", current_use_scope: "REVALIDATION_REQUIRED" });
    expect(review.receipt).toMatchObject({ id, event_id: event.id, account_id: account.id, entitlement_id: null,
      receipt_basis: "DIRECT_EVENT_ACCOUNT", revision_id: event.current_revision_id, revision_no: event.version,
      recorded_amount: "6.125000", recorded_tax: null, recorded_fee: "0.000000", received_hkd: "47.775000",
      fx_rate_hkd: "7.800000000000", status: "ACTIVE", version: "1", audit_scope: "RECORDED_AUDIT_MATCHED_ONLY",
      eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED" });
    expect(review.cash_entry_id).toBe(id === result.id ? result.cash_entry_id : lostResult!.cash_entry_id);
    const legacy = await page.request.get(`${base.replace("recorded-receipts", "receipts")}/${id}/void-inputs`);
    expect(legacy.status()).toBe(503); expect(legacy.headers()["cache-control"]).toBe("no-store");
  }
  const absent = await page.request.get(`${base}/${randomUUID()}/void-inputs`);
  expect(absent.status()).toBe(404); expect(absent.headers()["cache-control"]).toBe("no-store");
  expect(database()).toEqual(afterLost);
  const voidPath = (id: string) => `${path}/${id}/void`;
  const voidBody = async (id: string): Promise<Schemas["DirectReceiptVoidWrite"]> => {
    const response = await page.request.get(`${base}/${id}/void-inputs`);
    expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    const { current, receipt, cash_entry_id } = await response.json() as Schemas["DirectReceiptVoidInputs"];
    return { event_id: current.event_id, account_id: current.account_id,
      observed_revision_id: current.revision_id, observed_revision_no: current.revision_no,
      original_revision_id: receipt.revision_id, original_revision_no: receipt.revision_no,
      portfolio_id: current.portfolio_id, broker_id: current.broker_id, account_version: current.account_version,
      portfolio_version: current.portfolio_version, broker_version: current.broker_version,
      expected_data_revision: current.portfolio_data_revision, cash_entry_id,
      reason: "Synthetic direct receipt cash correction.", confirmation: "VOID_RECORDED_CASH" };
  };
  stage("COMMIT"); const firstVoid = await voidBody(result.id); const staleVoid = await voidBody(lostBody.receipt_id);
  const voidHeaders = await headers(page, 1);
  const [voidLeft, voidRight] = await Promise.all([
    page.request.post(voidPath(result.id), { headers: voidHeaders, data: firstVoid }),
    page.request.post(voidPath(result.id), { headers: voidHeaders, data: firstVoid }),
  ]);
  expect(voidLeft.status()).toBe(200); expect(voidRight.status()).toBe(200);
  expect(voidLeft.headers()["cache-control"]).toBe("no-store");
  const voided = await voidLeft.json() as Schemas["DirectReceiptVoided"];
  expect(await voidRight.json()).toEqual(voided);
  expect(voided).toMatchObject({ id: result.id, cash_entry_id: result.cash_entry_id, status: "VOID", version: "2",
    original_revision_id: event.current_revision_id, receipt_basis: "DIRECT_EVENT_ACCOUNT",
    scope: "HISTORICAL_DIRECT_RECEIPT_VOID_RESPONSE_ONLY", eligibility_scope: "NOT_ESTABLISHED" });
  const afterVoid = database(); const voidChanged = new Set(["dividend_direct_receipts", "cash_ledger_entries", "audit_events", "portfolios", "projection_invalidations", "idempotency_keys"]);
  expect(afterVoid.cash_ledger_entries).toHaveLength(afterLost.cash_ledger_entries!.length + 1);
  expect(afterVoid.audit_events).toHaveLength(afterLost.audit_events!.length + 3);
  for (const table of Object.keys(afterLost)) if (!voidChanged.has(table)) expect(afterVoid[table]).toEqual(afterLost[table]);
  const staleCorrection = await page.request.post(voidPath(lostBody.receipt_id), { headers: await headers(page, 1), data: staleVoid });
  expect(staleCorrection.status()).toBe(409); expect(staleCorrection.headers()["cache-control"]).toBe("no-store"); expect(database()).toEqual(afterVoid);
  stage("LOST"); const lostVoidBody = await voidBody(lostBody.receipt_id); const lostVoidHeaders = await headers(page, 1);
  const lostVoidPath = voidPath(lostBody.receipt_id); let lostVoidResult: Schemas["DirectReceiptVoided"] | null = null;
  await page.route(`**${lostVoidPath}`, async route => {
    const response = await route.fetch(); expect(response.status()).toBe(200);
    lostVoidResult = await response.json() as Schemas["DirectReceiptVoided"]; await route.abort("failed");
  });
  try {
    expect(await page.evaluate(async ({ url, intent, csrf, key }) => {
      try {
        await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf,
          "Idempotency-Key": key, "If-Match": '"1"' }, body: JSON.stringify(intent) }); return false;
      } catch { return true; }
    }, { url: lostVoidPath, intent: lostVoidBody, csrf: lostVoidHeaders["X-CSRF-Token"], key: lostVoidHeaders["Idempotency-Key"] })).toBe(true);
  } finally { await page.unroute(`**${lostVoidPath}`); }
  expect(lostVoidResult).not.toBeNull(); const afterLostVoid = database();
  expect(afterLostVoid.cash_ledger_entries).toHaveLength(afterVoid.cash_ledger_entries!.length + 1);
  expect(afterLostVoid.audit_events).toHaveLength(afterVoid.audit_events!.length + 3);
  for (const table of Object.keys(afterVoid)) if (!voidChanged.has(table)) expect(afterLostVoid[table]).toEqual(afterVoid[table]);
  stage("REPLAY"); const replayedVoid = await page.request.post(lostVoidPath, { headers: lostVoidHeaders, data: lostVoidBody });
  expect(replayedVoid.status()).toBe(200); expect(await replayedVoid.json()).toEqual(lostVoidResult); expect(database()).toEqual(afterLostVoid);
  expect(execFileSync("uv", ["run", "python", "-I", "-c", `
import os, sqlite3, sys
from pathlib import Path
root = Path(os.environ['SPM_E2E_CREDENTIAL_ROOT']).resolve(strict=True)
assert Path(os.environ['DATA_DIR']) == root / 'data'
with sqlite3.connect(f"file:{root / 'data' / 'portfolio.sqlite3'}?mode=ro", uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    for identifier in sys.argv[1:]:
        rows = db.execute("SELECT amount_micro,booked_hkd_micro,fx_rate_hkd_e12,entry_role FROM cash_ledger_entries WHERE source_type='DIVIDEND_DIRECT_RECEIPT' AND source_id=? ORDER BY entry_role", (identifier,)).fetchall()
        assert rows == [(6125000,47775000,7800000000000,'PRIMARY'),(-6125000,-47775000,7800000000000,'REVERSAL')]
print('EXACT_DIRECT_REVERSALS_PASS')
`, result.id, lostBody.receipt_id], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim()).toBe("EXACT_DIRECT_REVERSALS_PASS");
  expect(database()).toEqual(afterLostVoid);
  let formInputs: Schemas["DirectReceiptInputs"] | null = null;
  async function receiptPanel(name = "Synthetic direct receipt account") {
    await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
    if (page.viewportSize()!.width >= 1024) await page.locator(`tr[data-record-id="${event.id}"]`).getByRole("button", { name: "查看原值與操作" }).click();
    else await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    await article.getByRole("button", { name: "查看帳戶收款紀錄" }).click();
    const panel = article.getByRole("region", { name: "已記錄股息收款" });
    const choose = panel.getByRole("button", { name: `選擇收款紀錄帳戶 ${name}` });
    await choose.focus(); await page.keyboard.press("Enter"); return panel;
  }
  async function openDirectForm() {
    const panel = await receiptPanel();
    const read = page.waitForResponse(response => new URL(response.url()).pathname === `${base}/inputs` && response.request().method() === "GET");
    await panel.getByRole("button", { name: "準備直接記錄收款", exact: true }).click();
    const response = await read; expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    formInputs = await response.json() as Schemas["DirectReceiptInputs"];
    await panel.getByRole("button", { name: "核對後記錄直接收款" }).click();
    return page.getByRole("dialog", { name: "記錄直接股息收款" });
  }
  async function fillDirectForm() {
    const form = page.getByRole("form", { name: "記錄直接股息收款" });
    await form.getByLabel("實收日期").fill("2026-11-04"); await form.getByLabel("實收幣別").selectOption("HKD");
    await form.getByLabel("實收淨額").fill("6.125"); await form.getByLabel("實際稅額").selectOption("UNKNOWN");
    await form.getByLabel("實際費用").selectOption("PROVIDED"); await form.getByLabel("已記錄費用").fill("0");
    await form.getByRole("checkbox", { name: /另行指定有依據的實際港幣金額/ }).check();
    await form.getByLabel("實際港幣金額", { exact: true }).fill("6.125");
    await form.getByLabel("實際港幣金額依據").fill("Synthetic direct form identity evidence.");
    await form.getByLabel("收款依據").fill("Synthetic direct form recorded cash without entitlement.");
    await form.getByRole("checkbox", { name: /我明白這只是擁有人記錄的現金/ }).check();
    await form.getByRole("checkbox", { name: /我明白直接收款會持續阻擋/ }).check();
    return form;
  }
  stage("OPEN"); let sheet = await openDirectForm();
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  await expect(sheet.getByLabel("實收幣別")).toHaveValue(""); await expect(sheet.getByLabel("實收淨額")).toHaveValue("");
  await expect(sheet).toContainText("不需要或建立舊應收"); await expect(sheet).toContainText("目前尚無支援的解除流程");
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); const box = (await sheet.boundingBox())!;
    expect(box.width).toBeLessThanOrEqual(width); expect(box.height).toBeLessThanOrEqual(900);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  stage("PASSWORD"); const passwordForm = sheet.getByRole("form", { name: "重新驗證" });
  await passwordForm.getByLabel("目前密碼").fill(password); await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm.getByRole("status")).toContainText("已重新驗證");
  let form = await fillDirectForm(); const beforeForm = database();
  let releaseForm!: () => void; let committedForm!: () => void;
  const heldForm = new Promise<void>(resolve => { releaseForm = resolve; }); const savedForm = new Promise<void>(resolve => { committedForm = resolve; });
  let attempts = 0; let originalKey = ""; let replayKey = ""; let originalId = ""; let uiResult: Schemas["DirectReceiptCreated"] | null = null;
  await page.route(routePath, async route => {
    attempts++; const sent = JSON.parse(route.request().postData()!) as Schemas["DirectReceiptCreate"];
    expect(sent).not.toHaveProperty("entitlement_id"); expect(sent.confirm_revision_fence).toBe(true);
    expect(sent).toMatchObject({ event_id: event.id, revision_id: event.current_revision_id, account_id: account.id, currency: "HKD",
      tax: null, fee: "0", fx_rate_hkd: null, fx_source: null, actual_hkd: "6.125", actual_hkd_reason: "Synthetic direct form identity evidence.",
      expected_data_revision: formInputs!.portfolio_data_revision });
    const response = await route.fetch(); expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
    if (attempts === 1) {
      originalKey = route.request().headers()["idempotency-key"]!; originalId = sent.receipt_id;
      uiResult = await response.json() as Schemas["DirectReceiptCreated"]; committedForm(); await heldForm; await route.abort("failed");
    } else {
      replayKey = route.request().headers()["idempotency-key"]!; expect(sent.receipt_id).toBe(originalId);
      expect(await response.json()).toEqual(uiResult); await route.fulfill({ response });
    }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "記錄收款", exact: true }).click(); await savedForm;
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click(); await expect(sheet).toBeVisible();
  } finally { releaseForm(); }
  stage("LOST"); await expect(form.getByRole("alert")).toContainText("網絡連線中斷"); const afterForm = database();
  expect(afterForm.dividend_direct_receipts).toHaveLength(beforeForm.dividend_direct_receipts!.length + 1);
  expect(afterForm.cash_ledger_entries).toHaveLength(beforeForm.cash_ledger_entries!.length + 1);
  expect(afterForm.audit_events).toHaveLength(beforeForm.audit_events!.length + 3);
  for (const table of Object.keys(beforeForm)) if (!changed.has(table)) expect(afterForm[table]).toEqual(beforeForm[table]);
  expect(uiResult).toMatchObject({ currency: "HKD", fx_rate_hkd: "1.000000000000", fx_source: "IDENTITY", received_amount: "6.125000",
    received_hkd: "6.125000", actual_hkd: "6.125000", actual_hkd_reason: "Synthetic direct form identity evidence." });
  stage("REPLAY"); await form.getByLabel("實收淨額").fill("006.125000"); await form.getByRole("button", { name: "記錄收款", exact: true }).click();
  await expect(sheet).toHaveCount(0); await page.unroute(routePath);
  expect(attempts).toBe(2); expect(replayKey).toBe(originalKey); expect(database()).toEqual(afterForm);
  await expect(page.getByRole("status").filter({ hasText: "伺服器已回覆收款記錄" })).toBeVisible();
  stage("SECOND"); sheet = await openDirectForm(); form = await fillDirectForm();
  const renamed = await page.request.patch(`/api/v1/accounts/${account.id}`, { headers: await headers(page, 1), data: { name: "Synthetic renamed direct receipt account" } });
  expect(renamed.status()).toBe(200); const beforeRace = database();
  stage("COMPARE"); await form.getByRole("button", { name: "記錄收款", exact: true }).click();
  await expect(form.getByRole("alert")).toContainText("組合資料已變更"); await expect(form.getByLabel("實收淨額")).toHaveValue("6.125");
  expect(database()).toEqual(beforeRace); await sheet.getByRole("button", { name: "關閉編輯" }).click();
  let voidInputs: Schemas["DirectReceiptVoidInputs"] | null = null;
  async function openDirectVoid(name: string) {
    const panel = await receiptPanel(name);
    const read = page.waitForResponse(response => new URL(response.url()).pathname === `${base}/${originalId}/void-inputs` && response.request().method() === "GET");
    const select = panel.getByRole("button", { name: `核對作廢直接收款 ${originalId}` }); await select.focus(); await page.keyboard.press("Enter");
    const response = await read; expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    voidInputs = await response.json() as Schemas["DirectReceiptVoidInputs"];
    expect(voidInputs.cash_entry_id).toBe(uiResult!.cash_entry_id); expect(voidInputs.receipt.entitlement_id).toBeNull();
    await panel.getByRole("button", { name: "繼續核對並作廢直接收款" }).click();
    return page.getByRole("dialog", { name: "作廢直接股息收款" });
  }
  stage("OPEN"); let voidSheet = await openDirectVoid("Synthetic renamed direct receipt account");
  await expect(voidSheet.getByRole("button", { name: "關閉編輯" })).toBeFocused(); await expect(voidSheet.getByLabel("作廢依據")).toHaveValue("");
  await expect(voidSheet.getByRole("checkbox", { name: /我已核對原收款/ })).not.toBeChecked();
  await expect(voidSheet).toContainText("HKD 6.125000"); await expect(voidSheet).toContainText("作廢不會解除事件修訂");
  await expect(voidSheet).toContainText(uiResult!.cash_entry_id);
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); const box = (await voidSheet.boundingBox())!;
    expect(box.width).toBeLessThanOrEqual(width); expect(box.height).toBeLessThanOrEqual(900);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  stage("PASSWORD"); const correctionPassword = voidSheet.getByRole("form", { name: "重新驗證" });
  await correctionPassword.getByLabel("目前密碼").fill(password); await correctionPassword.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(correctionPassword.getByRole("status")).toContainText("已重新驗證");
  let correctionForm = voidSheet.getByRole("form", { name: "作廢直接股息收款" }); const correctionReason = "Synthetic direct form exact reversal.";
  await correctionForm.getByLabel("作廢依據").fill(correctionReason); await correctionForm.getByRole("checkbox", { name: /我已核對原收款/ }).check();
  const changedAccount = await page.request.patch(`/api/v1/accounts/${account.id}`, { headers: await headers(page, 2), data: { name: "Synthetic direct correction account" } });
  expect(changedAccount.status()).toBe(200); const beforeCorrectionRace = database();
  stage("COMPARE"); await correctionForm.getByRole("button", { name: "作廢收款並沖銷現金" }).click();
  await expect(correctionForm.getByRole("alert")).toContainText("事件、帳戶或相關組合已變更"); await expect(correctionForm.getByLabel("作廢依據")).toHaveValue(correctionReason);
  expect(database()).toEqual(beforeCorrectionRace); await voidSheet.getByRole("button", { name: "關閉編輯" }).click();
  stage("SECOND"); voidSheet = await openDirectVoid("Synthetic direct correction account"); correctionForm = voidSheet.getByRole("form", { name: "作廢直接股息收款" });
  await correctionForm.getByLabel("作廢依據").fill(correctionReason); await correctionForm.getByRole("checkbox", { name: /我已核對原收款/ }).check();
  const beforeUiVoid = database(); let releaseUiVoid!: () => void; let committedUiVoid!: () => void;
  const heldUiVoid = new Promise<void>(resolve => { releaseUiVoid = resolve; }); const savedUiVoid = new Promise<void>(resolve => { committedUiVoid = resolve; });
  const uiVoidPath = `**${voidPath(originalId)}`; let voidAttempts = 0; let voidKey = ""; let voidWire = "";
  let uiVoided: Schemas["DirectReceiptVoided"] | null = null;
  await page.route(uiVoidPath, async route => {
    voidAttempts++; const request = route.request(); const sent = JSON.parse(request.postData()!) as Schemas["DirectReceiptVoidWrite"];
    expect(sent).not.toHaveProperty("entitlement_id"); expect(request.headers()["if-match"]).toBe('"1"');
    expect(sent).toMatchObject({ event_id: event.id, observed_revision_id: event.current_revision_id, original_revision_id: uiResult!.revision_id,
      original_revision_no: uiResult!.revision_no, account_version: "3", expected_data_revision: voidInputs!.current.portfolio_data_revision,
      cash_entry_id: uiResult!.cash_entry_id, reason: correctionReason, confirmation: "VOID_RECORDED_CASH" });
    const response = await route.fetch(); expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    if (voidAttempts === 1) {
      voidKey = request.headers()["idempotency-key"]!; voidWire = request.postData()!; uiVoided = await response.json() as Schemas["DirectReceiptVoided"];
      committedUiVoid(); await heldUiVoid; await route.abort("failed");
    } else {
      expect(request.headers()["idempotency-key"]).toBe(voidKey); expect(request.postData()).toBe(voidWire);
      expect(await response.json()).toEqual(uiVoided); await route.fulfill({ response });
    }
  });
  stage("COMMIT_HELD");
  try {
    await correctionForm.getByRole("button", { name: "作廢收款並沖銷現金" }).click(); await savedUiVoid;
    await page.keyboard.press("Escape"); await voidSheet.getByRole("button", { name: "關閉編輯" }).click(); await expect(voidSheet).toBeVisible();
  } finally { releaseUiVoid(); }
  stage("LOST"); await expect(correctionForm.getByRole("alert")).toContainText("網絡連線中斷"); const afterUiVoid = database();
  expect(afterUiVoid.dividend_direct_receipts).toHaveLength(beforeUiVoid.dividend_direct_receipts!.length);
  expect(afterUiVoid.cash_ledger_entries).toHaveLength(beforeUiVoid.cash_ledger_entries!.length + 1); expect(afterUiVoid.audit_events).toHaveLength(beforeUiVoid.audit_events!.length + 3);
  for (const table of Object.keys(beforeUiVoid)) if (!voidChanged.has(table)) expect(afterUiVoid[table]).toEqual(beforeUiVoid[table]);
  stage("REPLAY"); await correctionForm.getByRole("button", { name: "作廢收款並沖銷現金" }).click(); await expect(voidSheet).toHaveCount(0);
  await page.unroute(uiVoidPath); expect(voidAttempts).toBe(2); expect(database()).toEqual(afterUiVoid);
  expect(execFileSync("uv", ["run", "python", "-I", "-c", `
import os, sqlite3, sys
from pathlib import Path
root = Path(os.environ['SPM_E2E_CREDENTIAL_ROOT']).resolve(strict=True)
assert Path(os.environ['DATA_DIR']) == root / 'data'
with sqlite3.connect(f"file:{root / 'data' / 'portfolio.sqlite3'}?mode=ro", uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    rows = db.execute("SELECT amount_micro,booked_hkd_micro,fx_rate_hkd_e12,entry_role FROM cash_ledger_entries WHERE source_type='DIVIDEND_DIRECT_RECEIPT' AND source_id=? ORDER BY entry_role", (sys.argv[1],)).fetchall()
    assert rows == [(6125000,6125000,1000000000000,'PRIMARY'),(-6125000,-6125000,1000000000000,'REVERSAL')]
    assert db.execute("SELECT status,version FROM dividend_direct_receipts WHERE id=?", (sys.argv[1],)).fetchone() == ('VOID',2)
print('EXACT_DIRECT_FORM_REVERSAL_PASS')
`, originalId], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim()).toBe("EXACT_DIRECT_FORM_REVERSAL_PASS");
  const finalPanel = await receiptPanel("Synthetic direct correction account"); await expect(finalPanel.getByRole("button", { name: `核對作廢直接收款 ${originalId}` })).toBeDisabled();
  expect(database()).toEqual(afterUiVoid);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
