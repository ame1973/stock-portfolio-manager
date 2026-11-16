import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, readEvent, stage, type Event } from "./helpers";

const financialTables = ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"];

test("isolated withdrawal preserves history and refuses an unobserved rejection", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-withdrawal-browser-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  const reauth = await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } });
  expect(reauth.status()).toBe(200);

  stage("MASTER");
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: { exchange_code: "NASDAQ", display_ticker: "WDRE2E", name: "Synthetic withdrawal security", quote_currency: "USD" } });
  expect(securityResponse.status()).toBe(201);
  const security = await securityResponse.json() as { id: string };
  const create = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "92233720368.54775807", currency: "USD",
    adjustment_basis: "AS_DECLARED", citation: "Synthetic event reference", reason: "Synthetic event terms for a withdrawal decision.",
  } });
  expect(create.status()).toBe(201);
  const initial = await create.json() as Event;
  const acceptance = await page.request.post(`/api/v1/dividends/${initial.id}/verify`, { headers: await headers(page, 1), data: {
    revision_id: initial.current_revision_id, decision: "ACCEPT_REVISION", citation: "Synthetic acceptance reference", reason: "Synthetic owner acceptance before withdrawal.",
  } });
  expect(acceptance.status()).toBe(200);
  const event = await acceptance.json() as Event;
  expect(event.version).toBe(2); expect(event.manual_locked).toBe(true);
  const beforeRejection = await readEvent(page, event.id);
  const rejection = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, 2), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic rejection reference",
    evidence_url: "https://example.test/reject?private=synthetic-secret#page", reason: "Synthetic rejection requires explicit withdrawal resolution.",
  } });
  expect(rejection.status()).toBe(200);
  expect(await readEvent(page, event.id)).toEqual(beforeRejection);
  const rejectionResponse = await page.request.get(`/api/v1/dividends/events/${event.id}/rejection`);
  expect(rejectionResponse.headers()["cache-control"]).toBe("no-store");
  const rejected = await rejectionResponse.json() as components["schemas"]["RejectionState"];
  expect(rejected.state).toBe("REJECTED");

  stage("NAVIGATE");
  await page.setViewportSize({ width: 360, height: 900 });
  await page.goto("/dividends");
  const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
  await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const panel = article.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(panel).toContainText("此版本已記錄拒絕");
  const opener = panel.getByRole("button", { name: "撤回此事件" });
  await opener.focus(); await page.keyboard.press("Enter");
  const sheet = page.getByRole("dialog", { name: "撤回股息事件" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  const passwordForm = sheet.getByRole("form", { name: "重新驗證" });
  await passwordForm.getByLabel("目前密碼").fill(password);
  await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  const form = sheet.getByRole("form", { name: "撤回股息事件" });
  await expect(form).toContainText(rejected.rejection!.id);
  await expect(form).toContainText("Synthetic rejection requires explicit withdrawal resolution.");
  await expect(form).toContainText("除淨日 2026-11-03；每股 USD 92233720368.54775807");
  await expect(form).toContainText("不會沖銷實收");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const box = (await sheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await form.getByLabel("撤回引用參考").fill("Synthetic browser withdrawal reference");
  await form.getByLabel("撤回引用網址（選填）").fill("https://example.test/withdraw?private=synthetic-secret#page");
  await form.getByLabel("撤回決定依據").fill("Synthetic withdrawal explicitly resolves the observed rejection.");
  await form.getByRole("checkbox", { name: /我確認撤回所示股息事件/ }).check();
  const beforeValidation = database();
  await form.getByRole("button", { name: "記錄撤回決定" }).click();
  await expect(form.getByRole("alert")).toContainText("請確認明確處理所示拒絕");
  expect(database()).toEqual(beforeValidation);
  await form.getByRole("checkbox", { name: /我確認以本次撤回決定明確處理所示拒絕/ }).check();

  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const saved = new Promise<void>(resolve => { committed = resolve; });
  let sentBody = ""; let sentKey = ""; let retryBody = ""; let retryKey = ""; let match = ""; let attempts = 0;
  let savedWithdrawal!: Event;
  const path = `**/api/v1/dividends/events/${event.id}/withdraw`;
  await page.route(path, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    attempts++;
    const body = route.request().postData()!; const requestHeaders = route.request().headers();
    const response = await route.fetch(); expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    if (attempts === 1) { sentBody = body; sentKey = requestHeaders["idempotency-key"]!; match = requestHeaders["if-match"]!; committed(); await held; await route.abort("failed"); }
    else { retryBody = body; retryKey = requestHeaders["idempotency-key"]!; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "記錄撤回決定" }).click(); await saved;
    const current = await readEvent(page, event.id);
    expect(current.etag).toBe('"3"'); expect(current.data.workflow_status).toBe("WITHDRAWN"); expect(current.data.manual_locked).toBe(true);
    expect(current.data.dps).toBe(event.dps); expect(current.data.document_verification).toBe("NOT_ESTABLISHED");
    savedWithdrawal = current.data;
    const afterWithdrawal = database();
    expect(afterWithdrawal.dividend_rejection_resolutions).toHaveLength(beforeValidation.dividend_rejection_resolutions!.length + 1);
    for (const table of financialTables) expect(afterWithdrawal[table]).toEqual(beforeValidation[table]);
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(sheet).toBeVisible();
    await expect(form.getByLabel("撤回引用參考")).toHaveValue("Synthetic browser withdrawal reference");
  } finally { release(); }
  stage("LOST");
  await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  await passwordForm.getByLabel("目前密碼").fill(password);
  await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  const reopened = await page.request.patch(`/api/v1/dividends/events/${event.id}`, { headers: await headers(page, 3), data: {
    security_id: security.id, revision_id: savedWithdrawal.current_revision_id, resolve_rejection_id: null,
    event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null, pay_date: null,
    dps: "1.00000001", currency: "USD", adjustment_basis: "UNKNOWN", citation: "Synthetic reopened event reference", source_url: null,
    reason: "Synthetic explicit reopening after the withdrawal response was lost.", confirm_unlock: true, confirm_reopen: true,
  } });
  expect(reopened.status()).toBe(200);
  const beforeReplay = database();
  for (const table of financialTables) expect(beforeReplay[table]).toEqual(beforeValidation[table]);
  stage("REPLAY");
  await form.getByRole("button", { name: "記錄撤回決定" }).click();
  await expect(sheet).toHaveCount(0);
  await page.unroute(path);
  expect(attempts).toBe(2); expect(retryKey).toBe(sentKey); expect(retryBody).toBe(sentBody); expect(match).toBe('"2"');
  expect(JSON.parse(sentBody)).toMatchObject({ revision_id: event.current_revision_id, resolve_rejection_id: rejected.rejection!.id,
    confirmation: "WITHDRAW_EVENT", citation: "Synthetic browser withdrawal reference" });
  expect(database()).toEqual(beforeReplay);
  const latest = await readEvent(page, event.id);
  expect(latest.etag).toBe('"4"'); expect(latest.data.workflow_status).toBe("INCOMPLETE"); expect(latest.data.adjustment_basis).toBe("UNKNOWN");
  await expect(page.getByRole("status").filter({ hasText: "伺服器已回覆撤回決定" })).toContainText("可能是原鍵回覆");

  stage("CHANGED_EVIDENCE");
  await page.goto("/dividends");
  const newerArticle = page.getByRole("article", { name: `股息事件 ${event.id}` });
  await newerArticle.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const newerPanel = newerArticle.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(newerPanel).toContainText("此版本未記錄拒絕");
  await expect(newerPanel.getByRole("button", { name: "人工接受此版本" })).toHaveCount(0);
  await newerPanel.getByRole("button", { name: "撤回此事件" }).click();
  const raceSheet = page.getByRole("dialog", { name: "撤回股息事件" });
  const racePassword = raceSheet.getByRole("form", { name: "重新驗證" });
  await racePassword.getByLabel("目前密碼").fill(password);
  await racePassword.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(racePassword).toContainText("已重新驗證");
  const raceForm = raceSheet.getByRole("form", { name: "撤回股息事件" });
  await raceForm.getByLabel("撤回引用參考").fill("Synthetic stale withdrawal reference");
  await raceForm.getByLabel("撤回決定依據").fill("Synthetic withdrawal cannot silently resolve a later rejection.");
  await raceForm.getByRole("checkbox", { name: /我確認撤回所示股息事件/ }).check();
  await expect(raceForm.getByRole("checkbox", { name: /明確處理所示拒絕/ })).toHaveCount(0);
  const competing = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, 4), data: {
    revision_id: latest.data.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic competing rejection",
    reason: "Synthetic rejection created after a clear withdrawal read.",
  } });
  expect(competing.status()).toBe(200);
  const beforeDenied = database();
  const denied = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/events/${event.id}/withdraw` && response.request().method() === "POST");
  await raceForm.getByRole("button", { name: "記錄撤回決定" }).click();
  expect((await denied).status()).toBe(409);
  await expect(raceForm.getByRole("alert")).toContainText("已有拒絕");
  await expect(raceForm.getByLabel("撤回引用參考")).toHaveValue("Synthetic stale withdrawal reference");
  expect(database()).toEqual(beforeDenied);
  expect((await readEvent(page, event.id)).etag).toBe('"4"');
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
