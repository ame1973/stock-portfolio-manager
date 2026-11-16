import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, readEvent, stage, type Event } from "./helpers";

const financialTables = ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"];

test("isolated manual override preserves history and pins the observed rejection", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-override-browser-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  const reauth = await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } });
  expect(reauth.status()).toBe(200);

  stage("MASTER");
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: { exchange_code: "NASDAQ", display_ticker: "OVRE2E", name: "Synthetic override security", quote_currency: "USD" } });
  expect(securityResponse.status()).toBe(201);
  const security = await securityResponse.json() as { id: string };
  const create = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "92233720368.54775807", currency: "USD",
    adjustment_basis: "AS_DECLARED", citation: "Synthetic event reference", reason: "Synthetic event terms for an override decision.",
  } });
  expect(create.status()).toBe(201);
  const initial = await create.json() as Event;
  const acceptance = await page.request.post(`/api/v1/dividends/${initial.id}/verify`, { headers: await headers(page, 1), data: {
    revision_id: initial.current_revision_id, decision: "ACCEPT_REVISION", citation: "Synthetic acceptance reference", reason: "Synthetic owner acceptance before withdrawal.",
  } });
  expect(acceptance.status()).toBe(200);
  const accepted = await acceptance.json() as Event;
  const withdrawal = await page.request.post(`/api/v1/dividends/events/${initial.id}/withdraw`, { headers: await headers(page, 2), data: {
    revision_id: accepted.current_revision_id, confirmation: "WITHDRAW_EVENT", citation: "Synthetic withdrawal reference", reason: "Synthetic withdrawal before explicit override.",
  } });
  expect(withdrawal.status()).toBe(200);
  const event = await withdrawal.json() as Event;
  expect(event.version).toBe(3); expect(event.workflow_status).toBe("WITHDRAWN"); expect(event.manual_locked).toBe(true);
  const beforeRejection = await readEvent(page, event.id);
  const rejection = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, 3), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic rejection reference",
    evidence_url: "https://example.test/reject?private=synthetic-secret#page", reason: "Synthetic rejection requires explicit override resolution.",
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
  const opener = panel.getByRole("button", { name: "人工覆寫此版本" });
  await opener.focus(); await page.keyboard.press("Enter");
  const sheet = page.getByRole("dialog", { name: "人工覆寫股息事件" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  const passwordForm = sheet.getByRole("form", { name: "重新驗證" });
  await passwordForm.getByLabel("目前密碼").fill(password);
  await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  const form = sheet.getByRole("form", { name: "人工覆寫股息事件" });
  await expect(form).toContainText(rejected.rejection!.id);
  await expect(form).toContainText("Synthetic rejection requires explicit override resolution.");
  await expect(form.getByLabel("事件每股金額")).toHaveValue("92233720368.54775807");
  await expect(form.getByLabel("事件引用參考")).toHaveValue("");
  await expect(form.getByLabel("事件引用網址（選填）")).toHaveValue("");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const box = (await sheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await form.getByLabel("事件每股金額").fill("1.00000001");
  await form.getByLabel("事件引用參考").fill("Synthetic browser override reference");
  await form.getByLabel("事件引用網址（選填）").fill("https://example.test/override?private=synthetic-secret#page");
  await form.getByLabel("事件決定依據").fill("Synthetic override explicitly replaces the observed terms.");
  await form.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }).check();
  const beforeValidation = database();
  await form.getByRole("button", { name: "儲存人工覆寫" }).click();
  await expect(form.getByText("請確認解除人工鎖定。")).toBeVisible();
  await expect(form.getByText("請確認重新開啟已撤回事件。")).toBeVisible();
  await expect(form.getByText("請確認明確處理所示拒絕。")).toBeVisible();
  await expect(form.getByText("請確認新版本使用本次填寫的引用。")).toBeVisible();
  await expect(form.getByText("請確認人工覆寫所示事件。")).toBeVisible();
  expect(database()).toEqual(beforeValidation);
  await form.getByRole("checkbox", { name: /我確認解除這個版本的人工鎖定/ }).check();
  await form.getByRole("checkbox", { name: /我確認重新開啟已撤回的事件/ }).check();
  await form.getByRole("checkbox", { name: /我確認以此覆寫明確處理所示拒絕/ }).check();
  await form.getByRole("checkbox", { name: /我明白新版本只使用本次填寫的引用/ }).check();
  await form.getByRole("checkbox", { name: /我確認以人工覆寫方式建立新版本/ }).check();

  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const saved = new Promise<void>(resolve => { committed = resolve; });
  let sentBody = ""; let sentKey = ""; let retryBody = ""; let retryKey = ""; let match = ""; let attempts = 0;
  let savedOverride!: Event;
  const path = `**/api/v1/dividends/${event.id}/verify`;
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
    await form.getByRole("button", { name: "儲存人工覆寫" }).click(); await saved;
    const current = await readEvent(page, event.id);
    expect(current.etag).toBe('"4"'); expect(current.data.workflow_status).toBe("PROVISIONAL"); expect(current.data.manual_locked).toBe(false);
    expect(current.data.manual_override).toBe(true); expect(current.data.dps).toBe("1.00000001"); expect(current.data.document_verification).toBe("NOT_ESTABLISHED");
    savedOverride = current.data;
    const afterOverride = database();
    expect(afterOverride.dividend_rejection_resolutions).toHaveLength(beforeValidation.dividend_rejection_resolutions!.length + 1);
    for (const table of financialTables) expect(afterOverride[table]).toEqual(beforeValidation[table]);
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(sheet).toBeVisible(); await expect(form.getByLabel("事件每股金額")).toHaveValue("1.00000001");
  } finally { release(); }
  stage("LOST");
  await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  await passwordForm.getByLabel("目前密碼").fill(password);
  await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  const successor = await page.request.patch(`/api/v1/dividends/events/${event.id}`, { headers: await headers(page, 4), data: {
    security_id: security.id, revision_id: savedOverride.current_revision_id, resolve_rejection_id: null,
    event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null, pay_date: null,
    dps: "1.00000002", currency: "USD", adjustment_basis: "AS_DECLARED", citation: "Synthetic later revision reference", source_url: null,
    reason: "Synthetic later revision after the override response was lost.", confirm_unlock: false, confirm_reopen: false,
  } });
  expect(successor.status()).toBe(200);
  const beforeReplay = database();
  for (const table of financialTables) expect(beforeReplay[table]).toEqual(beforeValidation[table]);
  stage("REPLAY");
  await form.getByRole("button", { name: "儲存人工覆寫" }).click();
  await expect(sheet).toHaveCount(0); await page.unroute(path);
  expect(attempts).toBe(2); expect(retryKey).toBe(sentKey); expect(retryBody).toBe(sentBody); expect(match).toBe('"3"');
  const body = JSON.parse(sentBody) as Record<string, unknown>;
  expect(body).toMatchObject({ revision_id: event.current_revision_id, resolve_rejection_id: rejected.rejection!.id,
    decision: "MANUAL_OVERRIDE", evidence_url: "https://example.test/override?private=synthetic-secret#page", confirm_unlock: true, confirm_reopen: true });
  expect(body).not.toHaveProperty("source_url");
  expect(database()).toEqual(beforeReplay);
  const latest = await readEvent(page, event.id);
  expect(latest.etag).toBe('"5"'); expect(latest.data.manual_override).toBe(false); expect(latest.data.dps).toBe("1.00000002");
  await expect(page.getByRole("status").filter({ hasText: "伺服器已回覆人工覆寫決定" })).toContainText("可能是原鍵回覆");

  stage("CHANGED_EVIDENCE");
  await page.goto("/dividends");
  const newerArticle = page.getByRole("article", { name: `股息事件 ${event.id}` });
  await newerArticle.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const newerPanel = newerArticle.getByRole("region", { name: "獨立拒絕狀態" });
  await expect(newerPanel).toContainText("此版本未記錄拒絕");
  await newerPanel.getByRole("button", { name: "人工覆寫此版本" }).click();
  const raceSheet = page.getByRole("dialog", { name: "人工覆寫股息事件" });
  const racePassword = raceSheet.getByRole("form", { name: "重新驗證" });
  await racePassword.getByLabel("目前密碼").fill(password);
  await racePassword.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(racePassword).toContainText("已重新驗證");
  const raceForm = raceSheet.getByRole("form", { name: "人工覆寫股息事件" });
  await raceForm.getByLabel("事件引用參考").fill("Synthetic stale override reference");
  await raceForm.getByLabel("事件決定依據").fill("Synthetic override cannot silently resolve a later rejection.");
  await raceForm.getByRole("checkbox", { name: /我明白新版本只使用本次填寫的引用/ }).check();
  await raceForm.getByRole("checkbox", { name: /我確認以人工覆寫方式建立新版本/ }).check();
  await raceForm.getByRole("checkbox", { name: /我明白這只記錄人工決定/ }).check();
  await expect(raceForm.getByRole("checkbox", { name: /明確處理所示拒絕/ })).toHaveCount(0);
  const competing = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, 5), data: {
    revision_id: latest.data.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic competing rejection",
    reason: "Synthetic rejection created after a clear override read.",
  } });
  expect(competing.status()).toBe(200);
  const beforeDenied = database();
  const denied = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/${event.id}/verify` && response.request().method() === "POST");
  await raceForm.getByRole("button", { name: "儲存人工覆寫" }).click();
  expect((await denied).status()).toBe(409);
  await expect(raceForm.getByRole("alert")).toContainText("已有拒絕");
  await expect(raceForm.getByLabel("事件引用參考")).toHaveValue("Synthetic stale override reference");
  expect(database()).toEqual(beforeDenied); expect((await readEvent(page, event.id)).etag).toBe('"5"');
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
