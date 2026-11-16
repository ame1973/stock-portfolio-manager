import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, type Page } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";

type Event = components["schemas"]["DividendEventRecord"];
type Tables = Record<string, string[]>;
const origin = process.env.SPM_E2E_BASE_URL!;
function stage(value: string) { writeFileSync(join(process.env.SPM_E2E_CREDENTIAL_ROOT!, "stage"), value); }
function database(): Tables {
  // Read only the exclusive synthetic fixture database; rows remain in process memory.
  return JSON.parse(execFileSync("uv", ["run", "python", "-I", "-c", `
import json, os, sqlite3
from pathlib import Path
root = Path(os.environ['SPM_E2E_CREDENTIAL_ROOT']).resolve(strict=True)
assert Path(os.environ['DATA_DIR']) == root / 'data'
with sqlite3.connect(f"file:{root / 'data' / 'portfolio.sqlite3'}?mode=ro", uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0026_dividend_projection_fee',)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version' ORDER BY name")]
    assert len(tables) == 63
    print(json.dumps({name: [json.dumps(row, separators=(',', ':')) for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid')] for name in tables}))
`], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] })) as Tables;
}
async function headers(page: Page, version?: number) {
  const csrf = await (await page.request.get("/api/v1/auth/csrf")).json() as { csrf_token: string };
  return { Origin: origin, "X-CSRF-Token": csrf.csrf_token, "Idempotency-Key": randomUUID(), ...(version ? { "If-Match": `"${version}"` } : {}) };
}
async function readEvent(page: Page, eventId: string) {
  const response = await page.request.get(`/api/v1/dividends/events/${eventId}`);
  expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
  return { data: await response.json() as Event, etag: response.headers().etag };
}

test("isolated event UI separates retained decisions, rejection and protected creation", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-dividend-browser-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  const reauth = await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } });
  expect(reauth.status()).toBe(200);
  stage("MASTER");
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: { exchange_code: "NASDAQ", display_ticker: "DIVE2E", name: "Synthetic dividend event security", quote_currency: "USD" } });
  expect(securityResponse.status()).toBe(201);
  const security = await securityResponse.json() as { id: string };
  const create = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "92233720368.54775807", currency: "USD",
    adjustment_basis: "AS_DECLARED", citation: "Synthetic event reference", source_url: "https://example.test/event?private=synthetic-secret#page",
    reason: "Synthetic event terms, not a verified source or financial entitlement.",
  } });
  expect(create.status()).toBe(201); expect(create.headers()["cache-control"]).toBe("no-store");
  const event = await create.json() as Event;
  expect(event.reference).toEqual({ citation: "Synthetic event reference", url: null, url_withheld: true });
  const beforeRejection = await readEvent(page, event.id);
  stage("NAVIGATE");
  await page.setViewportSize({ width: 360, height: 900 });
  const eventList = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/dividends/events" && response.request().method() === "GET");
  await page.goto("/dividends");
  expect((await eventList).headers()["cache-control"]).toBe("no-store");
  await expect(page.getByRole("heading", { name: "股息事件", exact: true })).toBeVisible();
  const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
  await expect(article).toContainText("USD 92233720368.54775807");
  const opener = article.getByRole("button", { name: "查看事件與拒絕狀態", exact: true });
  const rejectionRead = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/events/${event.id}/rejection`);
  await opener.focus(); await page.keyboard.press("Enter");
  expect((await rejectionRead).headers()["cache-control"]).toBe("no-store");
  const state = article.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(state).toContainText("此版本未記錄拒絕");
  await expect(state).toContainText("不是接受決定或財務資格");
  await expect(article).toContainText("引用包含查詢或片段，連結未提供");
  expect(await page.locator("body").innerText()).not.toContain("synthetic-secret");
  stage("COMMIT");
  const rejected = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, event.version), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic rejection reference",
    evidence_url: "https://example.test/rejection?private=synthetic-secret#page", reason: "Synthetic rejection requiring separate state revalidation.",
  } });
  expect(rejected.status()).toBe(200); expect(rejected.headers()["cache-control"]).toBe("no-store");
  const afterRejection = await readEvent(page, event.id);
  expect(afterRejection).toEqual(beforeRejection);
  const rejectionResponse = await page.request.get(`/api/v1/dividends/events/${event.id}/rejection`);
  expect(rejectionResponse.status()).toBe(200); expect(rejectionResponse.headers()["cache-control"]).toBe("no-store");
  const rejectionState = await rejectionResponse.json() as components["schemas"]["RejectionState"];
  expect(rejectionState.state).toBe("REJECTED");
  const rejectionId = rejectionState.rejection!.id;
  const snapshot = database();
  expect(snapshot.dividend_event_rejections).toHaveLength(1);
  for (const table of ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "upstream_attempts", "jobs"]) expect(snapshot[table]).toEqual([]);
  await state.getByRole("button", { name: "重新載入拒絕狀態" }).click();
  await expect(state).toContainText("此版本已記錄拒絕");
  await expect(state).toContainText("Synthetic rejection reference");
  await expect(state).toContainText("引用包含查詢或片段，連結未提供");
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/dividends");
    if (width >= 1024) {
      await expect(page.getByRole("navigation", { name: "主要導覽" }).getByRole("link")).toHaveCount(8);
      const row = page.getByRole("row").filter({ hasText: "92233720368.54775807" });
      await row.getByRole("button", { name: "查看原值與操作" }).click();
    } else {
      const bottom = page.getByRole("navigation", { name: "手機主要導覽" });
      await expect(bottom.getByRole("link")).toHaveCount(5);
      await expect(bottom.getByRole("link", { name: "股息事件" })).toHaveAttribute("aria-current", "page");
      await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    }
    await expect(article.getByRole("region", { name: "獨立拒絕狀態" })).toContainText("此版本已記錄拒絕");
    const history = article.getByRole("button", { name: "查看事件修訂歷史" });
    await history.focus(); await page.keyboard.press("Enter");
    await expect(article.getByRole("region", { name: "事件修訂歷史" })).toContainText("歷史版本 1");
    const decisions = article.getByRole("button", { name: "查看拒絕歷史" });
    await decisions.focus(); await page.keyboard.press("Enter");
    await expect(article.getByRole("region", { name: "拒絕歷史" })).toContainText("未記錄後繼解決版本");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator("body").innerText()).not.toContain("synthetic-secret");
  }
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 360, height: 900 });
  await page.getByRole("navigation", { name: "手機主要導覽" }).getByRole("link", { name: "交易紀錄" }).click();
  await expect(page.getByRole("heading", { name: "交易紀錄", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "核對企業行動" }).click();
  await expect(page.getByRole("heading", { name: "企業行動", exact: true })).toBeVisible();
  stage("COMPARE");
  expect(database()).toEqual(snapshot);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);

  stage("OPEN");
  await page.getByRole("navigation", { name: "手機主要導覽" }).getByRole("link", { name: "股息事件" }).click();
  const createButton = page.getByRole("button", { name: "新增人工股息事件", exact: true });
  await createButton.focus(); await page.keyboard.press("Enter");
  const sheet = page.getByRole("dialog", { name: "新增人工股息事件" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  const passwordForm = sheet.getByRole("form", { name: "重新驗證" });
  await passwordForm.getByLabel("目前密碼").fill(password);
  await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  await expect(passwordForm.getByLabel("目前密碼")).toHaveValue("");
  const form = sheet.getByRole("form", { name: "新增人工股息事件" });
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const bounds = (await sheet.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0); expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    for (const label of ["事件除淨日", "事件每股金額", "事件決定依據"]) {
      const input = (await form.getByLabel(label).boundingBox())!;
      expect(input.height).toBeGreaterThanOrEqual(44); expect(input.height).toBeLessThanOrEqual(56);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await form.getByLabel("人工事件證券", { exact: true }).selectOption(security.id);
  await form.getByLabel("事件類型").selectOption("INTERIM");
  await form.getByLabel("事件除淨日").fill("2026-11-04");
  await form.getByLabel("事件每股金額").fill("0000.00000001");
  await form.getByLabel("事件股息幣別").selectOption("HKD");
  await form.getByLabel("事件金額口徑").selectOption("AS_DECLARED");
  await form.getByLabel("事件引用參考").fill("\u0085Synthetic manual event reference\u0085");
  await form.getByLabel("事件引用網址（選填）").fill("https://example.test/manual?private=synthetic-secret#part");
  await form.getByLabel("事件決定依據").fill("\u0085Synthetic manual event and unchanged retry evidence.\u0085");
  await form.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }).check();
  stage("COMMIT_HELD");
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const saved = new Promise<void>(resolve => { committed = resolve; });
  let firstBody = ""; let firstKey = ""; let replayBody = ""; let replayKey = ""; let attempts = 0;
  await page.route("**/api/v1/dividends/events", async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    attempts++;
    const body = route.request().postData()!; const key = route.request().headers()["idempotency-key"]!;
    const response = await route.fetch(); expect(response.status()).toBe(201);
    if (attempts === 1) { firstBody = body; firstKey = key; committed(); await held; await route.abort("failed"); }
    else { replayBody = body; replayKey = key; await route.fulfill({ response }); }
  });
  try {
    await form.getByRole("button", { name: "儲存人工事件" }).click(); await saved;
    const afterWrite = database();
    expect(afterWrite.dividend_events).toHaveLength(2);
    for (const table of ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"]) expect(afterWrite[table]).toEqual(snapshot[table]);
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(sheet).toBeVisible();
    await expect(form.getByLabel("事件每股金額")).toHaveValue("0000.00000001");
    await expect(form.getByRole("button", { name: "取消" })).toBeDisabled();
  } finally { release(); }
  stage("LOST");
  await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  await passwordForm.getByLabel("目前密碼").fill(password);
  await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  const beforeReplay = database();
  stage("REPLAY");
  await form.getByRole("button", { name: "儲存人工事件" }).click();
  await expect(sheet).toHaveCount(0);
  await expect(createButton).toBeFocused();
  await page.unroute("**/api/v1/dividends/events");
  expect(attempts).toBe(2); expect(replayKey).toBe(firstKey); expect(replayBody).toBe(firstBody);
  expect(JSON.parse(firstBody)).toEqual({ security_id: security.id, event_type: "INTERIM", ex_date: "2026-11-04", announce_date: null, record_date: null, pay_date: null,
    dps: "0000.00000001", currency: "HKD", adjustment_basis: "AS_DECLARED", citation: "Synthetic manual event reference",
    source_url: "https://example.test/manual?private=synthetic-secret#part", reason: "Synthetic manual event and unchanged retry evidence." });
  expect(database()).toEqual(beforeReplay);
  await expect(page.getByRole("status").filter({ hasText: "可能是原鍵回覆" })).toBeVisible();
  await expect(page.getByText("HKD 0.00000001", { exact: false })).toBeVisible();
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);

  stage("SECOND");
  await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const rejectedPanel = article.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(rejectedPanel).toContainText("此版本已記錄拒絕");
  const revisionOpener = rejectedPanel.getByRole("button", { name: "修訂人工股息事件" });
  await revisionOpener.focus(); await page.keyboard.press("Enter");
  const revisionSheet = page.getByRole("dialog", { name: "修訂人工股息事件" });
  await expect(revisionSheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  const revisionPassword = revisionSheet.getByRole("form", { name: "重新驗證" });
  await revisionPassword.getByLabel("目前密碼").fill(password);
  await revisionPassword.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(revisionPassword).toContainText("已重新驗證");
  const revisionForm = revisionSheet.getByRole("form", { name: "修訂人工股息事件" });
  await expect(revisionForm).toContainText(rejectionId);
  await expect(revisionForm.getByLabel("事件每股金額")).toHaveValue("92233720368.54775807");
  await expect(revisionForm.getByLabel("事件引用參考")).toHaveValue("");
  await expect(revisionForm.getByLabel("事件引用網址（選填）")).toHaveValue("");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const box = (await revisionSheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await revisionForm.getByLabel("事件每股金額").fill("1.00000001");
  await revisionForm.getByLabel("事件引用參考").fill("Synthetic revised event reference");
  await revisionForm.getByLabel("事件決定依據").fill("Synthetic revision explicitly resolves the observed rejection.");
  await revisionForm.getByRole("checkbox", { name: /我確認以此修訂明確處理所示拒絕/ }).check();
  await revisionForm.getByRole("checkbox", { name: /我明白新版本只使用本次填寫的引用/ }).check();
  await revisionForm.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }).check();
  const beforeRevision = database();
  let releaseRevision!: () => void; let revisionCommitted!: () => void;
  const heldRevision = new Promise<void>(resolve => { releaseRevision = resolve; });
  const savedRevision = new Promise<void>(resolve => { revisionCommitted = resolve; });
  let revisionBody = ""; let revisionKey = ""; let retryBody = ""; let retryKey = ""; let revisionAttempts = 0; let match = "";
  let committedRevision!: Event;
  const path = `**/api/v1/dividends/events/${event.id}`;
  await page.route(path, async route => {
    if (route.request().method() !== "PATCH") { await route.continue(); return; }
    revisionAttempts++;
    const body = route.request().postData()!; const requestHeaders = route.request().headers();
    const response = await route.fetch(); expect(response.status()).toBe(200);
    if (revisionAttempts === 1) { revisionBody = body; revisionKey = requestHeaders["idempotency-key"]!; match = requestHeaders["if-match"]!; revisionCommitted(); await heldRevision; await route.abort("failed"); }
    else { retryBody = body; retryKey = requestHeaders["idempotency-key"]!; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await revisionForm.getByRole("button", { name: "儲存人工修訂" }).click(); await savedRevision;
    const current = await readEvent(page, event.id);
    expect(current.etag).toBe('"2"'); expect(current.data.dps).toBe("1.00000001");
    committedRevision = current.data;
    const afterRevision = database();
    expect(afterRevision.dividend_rejection_resolutions).toHaveLength(1);
    expect(afterRevision.dividend_event_rejections).toEqual(beforeRevision.dividend_event_rejections);
    for (const table of ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"]) expect(afterRevision[table]).toEqual(beforeRevision[table]);
    await page.keyboard.press("Escape"); await revisionSheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(revisionSheet).toBeVisible();
    await expect(revisionForm.getByLabel("事件每股金額")).toHaveValue("1.00000001");
  } finally { releaseRevision(); }
  stage("LOST");
  await expect(revisionForm.getByRole("alert")).toContainText("網絡連線中斷");
  await revisionPassword.getByLabel("目前密碼").fill(password);
  await revisionPassword.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(revisionPassword).toContainText("已重新驗證");
  expect(committedRevision).toBeDefined();
  const successor = await page.request.patch(`/api/v1/dividends/events/${event.id}`, { headers: await headers(page, 2), data: {
    security_id: security.id, revision_id: committedRevision.current_revision_id, resolve_rejection_id: null,
    event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null, pay_date: null,
    dps: "1.00000002", currency: "USD", adjustment_basis: "AS_DECLARED", citation: "Synthetic later successor reference",
    source_url: null, reason: "Synthetic later decision after the original response was lost.", confirm_unlock: false, confirm_reopen: false,
  } });
  expect(successor.status()).toBe(200); expect(successor.headers().etag).toBe('"3"');
  const beforeRevisionReplay = database();
  for (const table of ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"]) expect(beforeRevisionReplay[table]).toEqual(beforeRevision[table]);
  stage("REPLAY");
  await revisionForm.getByRole("button", { name: "儲存人工修訂" }).click();
  await expect(revisionSheet).toHaveCount(0);
  await page.unroute(path);
  expect(revisionAttempts).toBe(2); expect(retryKey).toBe(revisionKey); expect(retryBody).toBe(revisionBody); expect(match).toBe('"1"');
  expect(JSON.parse(revisionBody)).toMatchObject({ security_id: security.id, revision_id: event.current_revision_id, resolve_rejection_id: rejectionId,
    confirm_unlock: false, confirm_reopen: false, dps: "1.00000001", currency: "USD", source_url: null });
  expect(database()).toEqual(beforeRevisionReplay);
  const latest = await readEvent(page, event.id);
  expect(latest.etag).toBe('"3"'); expect(latest.data.dps).toBe("1.00000002");
  await expect(page.getByRole("status").filter({ hasText: "可能是原鍵回覆" })).toBeVisible();
  const refreshed = await page.request.get(`/api/v1/dividends/events/${event.id}/rejection`);
  expect(refreshed.status()).toBe(200); expect((await refreshed.json() as components["schemas"]["RejectionState"]).state).toBe("NOT_REJECTED");
  const historyResponse = await page.request.get(`/api/v1/dividends/events/${event.id}/rejections`);
  const retained = await historyResponse.json() as components["schemas"]["Page_RejectionHistoryItem_"];
  expect(retained.items[0]?.resolution?.successor_revision_no).toBe(2);

  stage("CHANGED_EVIDENCE");
  const eventListResponse = await page.request.get("/api/v1/dividends/events?limit=25");
  const listed = await eventListResponse.json() as components["schemas"]["Page_DividendEventRecord_"];
  const competing = listed.items.find(item => item.dps === "0.00000001" && item.currency === "HKD")!;
  expect(competing.version).toBe(1);
  await page.goto("/dividends");
  const competingArticle = page.getByRole("article", { name: `股息事件 ${competing.id}` });
  await competingArticle.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const competingState = competingArticle.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(competingState).toContainText("此版本未記錄拒絕");
  await competingState.getByRole("button", { name: "修訂人工股息事件" }).click();
  const staleSheet = page.getByRole("dialog", { name: "修訂人工股息事件" });
  // Reuse the real recent session established by the revision replay.
  const staleForm = staleSheet.getByRole("form", { name: "修訂人工股息事件" });
  await staleForm.getByLabel("事件引用參考").fill("Synthetic stale revision reference");
  await staleForm.getByLabel("事件決定依據").fill("Synthetic revision must not silently resolve a new rejection.");
  await staleForm.getByRole("checkbox", { name: /我明白新版本只使用本次填寫的引用/ }).check();
  await staleForm.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }).check();
  await expect(staleForm.getByRole("checkbox", { name: /我確認以此修訂明確處理所示拒絕/ })).toHaveCount(0);
  const competingRejection = await page.request.post(`/api/v1/dividends/${competing.id}/verify`, { headers: await headers(page, competing.version), data: {
    revision_id: competing.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic competing rejection reference",
    reason: "Synthetic concurrent rejection requires a fresh explicit pin.",
  } });
  expect(competingRejection.status()).toBe(200);
  const beforeDenied = database();
  const denied = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/events/${competing.id}` && response.request().method() === "PATCH");
  await staleForm.getByRole("button", { name: "儲存人工修訂" }).click();
  expect((await denied).status()).toBe(409);
  await expect(staleForm.getByRole("alert")).toContainText("已有拒絕");
  await expect(staleForm.getByLabel("事件引用參考")).toHaveValue("Synthetic stale revision reference");
  expect(database()).toEqual(beforeDenied);
  expect((await readEvent(page, competing.id)).etag).toBe('"1"');
  await staleSheet.getByRole("button", { name: "關閉編輯" }).click();

  stage("SECOND");
  const createRejectionEvent = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "REGULAR", ex_date: "2026-11-05", dps: "0.25000000", currency: "USD",
    adjustment_basis: "AS_DECLARED", citation: "Synthetic event for rejection", reason: "Synthetic event for explicit browser rejection.",
  } });
  expect(createRejectionEvent.status()).toBe(201);
  const rejectEvent = await createRejectionEvent.json() as Event;
  const originalRejectEvent = await readEvent(page, rejectEvent.id);
  await page.goto("/dividends");
  const rejectArticle = page.getByRole("article", { name: `股息事件 ${rejectEvent.id}` });
  await rejectArticle.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const rejectPanel = rejectArticle.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(rejectPanel).toContainText("此版本未記錄拒絕");
  const rejectOpener = rejectPanel.getByRole("button", { name: "拒絕此事件版本" });
  await rejectOpener.focus(); await page.keyboard.press("Enter");
  const rejectSheet = page.getByRole("dialog", { name: "拒絕股息事件版本" });
  await expect(rejectSheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  const rejectPassword = rejectSheet.getByRole("form", { name: "重新驗證" });
  await rejectPassword.getByLabel("目前密碼").fill(password);
  await rejectPassword.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(rejectPassword).toContainText("已重新驗證");
  const rejectForm = rejectSheet.getByRole("form", { name: "拒絕股息事件版本" });
  await expect(rejectForm).toContainText(rejectEvent.current_revision_id);
  await expect(rejectForm).toContainText("除淨日 2026-11-05；每股 USD 0.25000000");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const box = (await rejectSheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await rejectForm.getByLabel("拒絕引用參考").fill("Synthetic browser rejection reference");
  await rejectForm.getByLabel("拒絕引用網址（選填）").fill("https://example.test/reject?private=synthetic-secret#page");
  await rejectForm.getByLabel("拒絕決定依據").fill("Synthetic browser rejection preserves the original event.");
  await rejectForm.getByRole("checkbox", { name: /我確認拒絕所示事件版本/ }).check();
  const beforeBrowserReject = database();
  let releaseReject!: () => void; let rejectCommitted!: () => void;
  const heldReject = new Promise<void>(resolve => { releaseReject = resolve; });
  const savedReject = new Promise<void>(resolve => { rejectCommitted = resolve; });
  let rejectBody = ""; let rejectKey = ""; let retryRejectBody = ""; let retryRejectKey = ""; let rejectMatch = ""; let rejectAttempts = 0;
  const rejectPath = `**/api/v1/dividends/${rejectEvent.id}/verify`;
  await page.route(rejectPath, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    rejectAttempts++;
    const body = route.request().postData()!; const requestHeaders = route.request().headers();
    const response = await route.fetch(); expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    if (rejectAttempts === 1) { rejectBody = body; rejectKey = requestHeaders["idempotency-key"]!; rejectMatch = requestHeaders["if-match"]!; rejectCommitted(); await heldReject; await route.abort("failed"); }
    else { retryRejectBody = body; retryRejectKey = requestHeaders["idempotency-key"]!; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await rejectForm.getByRole("button", { name: "記錄拒絕決定" }).click(); await savedReject;
    expect(await readEvent(page, rejectEvent.id)).toEqual(originalRejectEvent);
    const afterBrowserReject = database();
    expect(afterBrowserReject.dividend_event_rejections).toHaveLength(beforeBrowserReject.dividend_event_rejections!.length + 1);
    for (const table of ["dividend_events", "dividend_event_revisions", "dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"]) expect(afterBrowserReject[table]).toEqual(beforeBrowserReject[table]);
    await page.keyboard.press("Escape"); await rejectSheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(rejectSheet).toBeVisible();
    await expect(rejectForm.getByLabel("拒絕引用參考")).toHaveValue("Synthetic browser rejection reference");
  } finally { releaseReject(); }
  stage("LOST");
  await expect(rejectForm.getByRole("alert")).toContainText("網絡連線中斷");
  await rejectPassword.getByLabel("目前密碼").fill(password);
  await rejectPassword.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(rejectPassword).toContainText("已重新驗證");
  const browserRejectionResponse = await page.request.get(`/api/v1/dividends/events/${rejectEvent.id}/rejection`);
  const browserRejection = await browserRejectionResponse.json() as components["schemas"]["RejectionState"];
  expect(browserRejection.state).toBe("REJECTED");
  expect(browserRejection.rejection!.reference).toEqual({ citation: "Synthetic browser rejection reference", url: null, url_withheld: true });
  const browserResolution = await page.request.patch(`/api/v1/dividends/events/${rejectEvent.id}`, { headers: await headers(page, 1), data: {
    security_id: security.id, revision_id: rejectEvent.current_revision_id, resolve_rejection_id: browserRejection.rejection!.id,
    event_type: "REGULAR", ex_date: "2026-11-05", announce_date: null, record_date: null, pay_date: null,
    dps: "0.25000000", currency: "USD", adjustment_basis: "AS_DECLARED", citation: "Synthetic resolution reference", source_url: null,
    reason: "Synthetic later resolution after a lost rejection response.", confirm_unlock: false, confirm_reopen: false,
  } });
  expect(browserResolution.status()).toBe(200);
  const beforeRejectReplay = database();
  for (const table of ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"]) expect(beforeRejectReplay[table]).toEqual(beforeBrowserReject[table]);
  stage("REPLAY");
  await rejectForm.getByRole("button", { name: "記錄拒絕決定" }).click();
  await expect(rejectSheet).toHaveCount(0);
  await page.unroute(rejectPath);
  expect(rejectAttempts).toBe(2); expect(retryRejectKey).toBe(rejectKey); expect(retryRejectBody).toBe(rejectBody); expect(rejectMatch).toBe('"1"');
  expect(JSON.parse(rejectBody)).toMatchObject({ revision_id: rejectEvent.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic browser rejection reference" });
  expect(database()).toEqual(beforeRejectReplay);
  expect((await readEvent(page, rejectEvent.id)).etag).toBe('"2"');
  const afterRejectReplay = await page.request.get(`/api/v1/dividends/events/${rejectEvent.id}/rejection`);
  expect((await afterRejectReplay.json() as components["schemas"]["RejectionState"]).state).toBe("NOT_REJECTED");
  await expect(page.getByRole("status").filter({ hasText: "伺服器已回覆拒絕決定" })).toContainText("可能是原鍵回覆");

  stage("CHANGED_EVIDENCE");
  await page.goto("/dividends");
  const newerArticle = page.getByRole("article", { name: `股息事件 ${rejectEvent.id}` });
  await newerArticle.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const newerPanel = newerArticle.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(newerPanel).toContainText("此版本未記錄拒絕");
  await newerPanel.getByRole("button", { name: "拒絕此事件版本" }).click();
  const raceSheet = page.getByRole("dialog", { name: "拒絕股息事件版本" });
  // Reuse the real recent session established by the rejection replay.
  const raceForm = raceSheet.getByRole("form", { name: "拒絕股息事件版本" });
  await raceForm.getByLabel("拒絕引用參考").fill("Synthetic stale rejection reference");
  await raceForm.getByLabel("拒絕決定依據").fill("Synthetic rejection must not replace a competing decision.");
  await raceForm.getByRole("checkbox", { name: /我確認拒絕所示事件版本/ }).check();
  const successorEvent = await browserResolution.json() as Event;
  const competingDecision = await page.request.post(`/api/v1/dividends/${rejectEvent.id}/verify`, { headers: await headers(page, 2), data: {
    revision_id: successorEvent.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic competing rejection", reason: "Synthetic concurrent rejection wins before the browser decision.",
  } });
  expect(competingDecision.status()).toBe(200);
  const beforeRaceDenied = database();
  const raceDenied = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/${rejectEvent.id}/verify` && response.request().method() === "POST");
  await raceForm.getByRole("button", { name: "記錄拒絕決定" }).click();
  expect((await raceDenied).status()).toBe(409);
  await expect(raceForm.getByRole("alert")).toContainText("已有拒絕");
  await expect(raceForm.getByLabel("拒絕引用參考")).toHaveValue("Synthetic stale rejection reference");
  expect(database()).toEqual(beforeRaceDenied);
  expect((await readEvent(page, rejectEvent.id)).etag).toBe('"2"');
  await raceSheet.getByRole("button", { name: "關閉編輯" }).click();
  await expect(newerPanel).toContainText("此版本已記錄拒絕");
  const currentRejectionResponse = await page.request.get(`/api/v1/dividends/events/${rejectEvent.id}/rejection`);
  const currentRejection = await currentRejectionResponse.json() as components["schemas"]["RejectionState"];
  expect(currentRejection.state).toBe("REJECTED");
  const acceptOpener = newerPanel.getByRole("button", { name: "人工接受此版本" });
  await acceptOpener.focus(); await page.keyboard.press("Enter");
  const acceptSheet = page.getByRole("dialog", { name: "人工接受股息事件版本" });
  await expect(acceptSheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  const acceptPassword = acceptSheet.getByRole("form", { name: "重新驗證" });
  await acceptPassword.getByLabel("目前密碼").fill(password);
  await acceptPassword.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(acceptPassword).toContainText("已重新驗證");
  const acceptForm = acceptSheet.getByRole("form", { name: "人工接受股息事件版本" });
  await expect(acceptForm).toContainText(currentRejection.rejection!.id);
  await expect(acceptForm).toContainText("Synthetic concurrent rejection wins before the browser decision.");
  await expect(acceptForm).toContainText("Synthetic competing rejection");
  await expect(acceptForm).toContainText("一般股息");
  await expect(acceptForm).toContainText("宣告：未知；記錄：未知；付款：未知");
  await expect(acceptForm).toContainText("除淨日 2026-11-05；每股 USD 0.25000000");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const box = (await acceptSheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await acceptForm.getByLabel("接受引用參考").fill("Synthetic browser acceptance reference");
  await acceptForm.getByLabel("接受引用網址（選填）").fill("https://example.test/accept?private=synthetic-secret#page");
  await acceptForm.getByLabel("接受決定依據").fill("Synthetic acceptance explicitly resolves the observed rejection.");
  await acceptForm.getByRole("checkbox", { name: /我確認以人工決定接受所示事件條款/ }).check();
  const beforeAcceptanceValidation = database();
  await acceptForm.getByRole("button", { name: "記錄人工接受" }).click();
  await expect(acceptForm.getByRole("alert")).toContainText("請確認明確處理所示拒絕");
  expect(database()).toEqual(beforeAcceptanceValidation);
  await acceptForm.getByRole("checkbox", { name: /我確認以本次接受決定明確處理所示拒絕/ }).check();
  let releaseAccept!: () => void; let acceptCommitted!: () => void;
  const heldAccept = new Promise<void>(resolve => { releaseAccept = resolve; });
  const savedAccept = new Promise<void>(resolve => { acceptCommitted = resolve; });
  let acceptBody = ""; let acceptKey = ""; let retryAcceptBody = ""; let retryAcceptKey = ""; let acceptMatch = ""; let acceptAttempts = 0;
  let committedAcceptance!: Event;
  const acceptPath = `**/api/v1/dividends/${rejectEvent.id}/verify`;
  await page.route(acceptPath, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    acceptAttempts++;
    const body = route.request().postData()!; const requestHeaders = route.request().headers();
    const response = await route.fetch(); expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    if (acceptAttempts === 1) { acceptBody = body; acceptKey = requestHeaders["idempotency-key"]!; acceptMatch = requestHeaders["if-match"]!; acceptCommitted(); await heldAccept; await route.abort("failed"); }
    else { retryAcceptBody = body; retryAcceptKey = requestHeaders["idempotency-key"]!; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await acceptForm.getByRole("button", { name: "記錄人工接受" }).click(); await savedAccept;
    const current = await readEvent(page, rejectEvent.id);
    expect(current.etag).toBe('"3"'); expect(current.data.workflow_status).toBe("VERIFIED"); expect(current.data.manual_locked).toBe(true);
    expect(current.data.dps).toBe("0.25000000"); expect(current.data.document_verification).toBe("NOT_ESTABLISHED");
    committedAcceptance = current.data;
    const afterAcceptance = database();
    expect(afterAcceptance.dividend_rejection_resolutions).toHaveLength(beforeRaceDenied.dividend_rejection_resolutions!.length + 1);
    for (const table of ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"]) expect(afterAcceptance[table]).toEqual(beforeRaceDenied[table]);
    await page.keyboard.press("Escape"); await acceptSheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(acceptSheet).toBeVisible();
    await expect(acceptForm.getByLabel("接受引用參考")).toHaveValue("Synthetic browser acceptance reference");
  } finally { releaseAccept(); }
  stage("ACCEPT_LOST");
  await expect(acceptForm.getByRole("alert")).toContainText("網絡連線中斷");
  await acceptPassword.getByLabel("目前密碼").fill(password);
  const acceptanceReauthentication = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/auth/reauth" && response.request().method() === "POST");
  await acceptPassword.getByRole("button", { name: "確認目前密碼" }).click();
  const acceptanceReauthenticationResult = await acceptanceReauthentication;
  stage(acceptanceReauthenticationResult.status() === 429 ? "ACCEPT_RATE_LIMITED" : "ACCEPT_REAUTH");
  expect(acceptanceReauthenticationResult.status()).toBe(200);
  await expect(acceptPassword).toContainText("已重新驗證");
  const afterAcceptanceRevision = await page.request.patch(`/api/v1/dividends/events/${rejectEvent.id}`, { headers: await headers(page, 3), data: {
    security_id: security.id, revision_id: committedAcceptance.current_revision_id, resolve_rejection_id: null,
    event_type: "REGULAR", ex_date: "2026-11-05", announce_date: null, record_date: null, pay_date: null,
    dps: "0.26000000", currency: "USD", adjustment_basis: "AS_DECLARED", citation: "Synthetic later revision reference", source_url: null,
    reason: "Synthetic later revision after the acceptance response was lost.", confirm_unlock: true, confirm_reopen: false,
  } });
  expect(afterAcceptanceRevision.status()).toBe(200);
  const beforeAcceptReplay = database();
  for (const table of ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"]) expect(beforeAcceptReplay[table]).toEqual(beforeRaceDenied[table]);
  stage("REPLAY");
  await acceptForm.getByRole("button", { name: "記錄人工接受" }).click();
  await expect(acceptSheet).toHaveCount(0);
  await page.unroute(acceptPath);
  expect(acceptAttempts).toBe(2); expect(retryAcceptKey).toBe(acceptKey); expect(retryAcceptBody).toBe(acceptBody); expect(acceptMatch).toBe('"2"');
  expect(JSON.parse(acceptBody)).toMatchObject({ revision_id: successorEvent.current_revision_id, resolve_rejection_id: currentRejection.rejection!.id,
    decision: "ACCEPT_REVISION", citation: "Synthetic browser acceptance reference" });
  expect(database()).toEqual(beforeAcceptReplay);
  const afterAcceptReplay = await readEvent(page, rejectEvent.id);
  expect(afterAcceptReplay.etag).toBe('"4"'); expect(afterAcceptReplay.data.workflow_status).toBe("PROVISIONAL");
  await expect(page.getByRole("status").filter({ hasText: "伺服器已回覆人工接受決定" })).toContainText("可能是原鍵回覆");

  stage("CHANGED_EVIDENCE");
  await page.goto("/dividends");
  const acceptanceRaceArticle = page.getByRole("article", { name: `股息事件 ${rejectEvent.id}` });
  await acceptanceRaceArticle.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const acceptanceRacePanel = acceptanceRaceArticle.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(acceptanceRacePanel).toContainText("此版本未記錄拒絕");
  await acceptanceRacePanel.getByRole("button", { name: "人工接受此版本" }).click();
  const acceptanceRaceSheet = page.getByRole("dialog", { name: "人工接受股息事件版本" });
  const acceptanceRacePassword = acceptanceRaceSheet.getByRole("form", { name: "重新驗證" });
  await acceptanceRacePassword.getByLabel("目前密碼").fill(password);
  await acceptanceRacePassword.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(acceptanceRacePassword).toContainText("已重新驗證");
  const acceptanceRaceForm = acceptanceRaceSheet.getByRole("form", { name: "人工接受股息事件版本" });
  await acceptanceRaceForm.getByLabel("接受引用參考").fill("Synthetic stale acceptance reference");
  await acceptanceRaceForm.getByLabel("接受決定依據").fill("Synthetic acceptance cannot silently resolve a later rejection.");
  await acceptanceRaceForm.getByRole("checkbox", { name: /我確認以人工決定接受所示事件條款/ }).check();
  await expect(acceptanceRaceForm.getByRole("checkbox", { name: /明確處理所示拒絕/ })).toHaveCount(0);
  const rejectionBeforeAcceptance = await page.request.post(`/api/v1/dividends/${rejectEvent.id}/verify`, { headers: await headers(page, 4), data: {
    revision_id: afterAcceptReplay.data.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic later competing rejection",
    reason: "Synthetic rejection created after a clear acceptance read.",
  } });
  expect(rejectionBeforeAcceptance.status()).toBe(200);
  const beforeAcceptanceDenied = database();
  const acceptanceDenied = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/${rejectEvent.id}/verify` && response.request().method() === "POST");
  await acceptanceRaceForm.getByRole("button", { name: "記錄人工接受" }).click();
  expect((await acceptanceDenied).status()).toBe(409);
  await expect(acceptanceRaceForm.getByRole("alert")).toContainText("已有拒絕");
  await expect(acceptanceRaceForm.getByLabel("接受引用參考")).toHaveValue("Synthetic stale acceptance reference");
  expect(database()).toEqual(beforeAcceptanceDenied);
  expect((await readEvent(page, rejectEvent.id)).etag).toBe('"4"');
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
