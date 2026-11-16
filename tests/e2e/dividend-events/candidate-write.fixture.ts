import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, seedCandidateSource, stage } from "./helpers";

type Candidate = components["schemas"]["CandidateRecord"];
const protectedTables = ["dividend_events", "dividend_event_revisions", "dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "upstream_attempts", "jobs"];

test("candidate form pins source evidence, preserves retries and rejects stale revisions", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-candidate-write-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  stage("MASTER");
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: {
    exchange_code: "NASDAQ", display_ticker: "CANDWR", name: "Synthetic candidate writer security", quote_currency: "USD",
  } });
  expect(securityResponse.status()).toBe(201);
  const security = await securityResponse.json() as { id: string };
  stage("SEED");
  const { source, reason } = await seedCandidateSource(page, security.id);
  const before = database();
  for (const table of protectedTables) expect(before[table]).toEqual([]);
  stage("NAVIGATE");
  await page.setViewportSize({ width: 360, height: 900 }); await page.goto("/dividends/candidates");
  const create = page.getByRole("button", { name: "新增股息候選" });
  await expect(create).toBeEnabled(); await create.focus(); await page.keyboard.press("Enter");
  const sheet = page.getByRole("dialog", { name: "新增股息候選" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  const form = sheet.getByRole("form", { name: "新增股息候選" });
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const box = (await sheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  stage("FILL");
  await form.getByLabel("候選證券", { exact: true }).selectOption(security.id);
  await form.getByLabel("候選事件類型").selectOption("SPECIAL");
  await form.getByLabel("候選除淨日（選填）").fill("2026-11-01");
  await form.getByLabel("候選每股金額（選填）").fill("000.00000001");
  await expect(form.getByLabel("候選股息幣別")).toHaveValue("");
  await form.getByLabel("候選變更依據").fill("\u0085Synthetic candidate browser decision and retry.\u0085");
  await form.getByRole("button", { name: "新增來源關聯" }).click();
  await form.getByLabel("來源 1 觀測識別碼").fill(source.id);
  await form.getByLabel("來源 1 原始雜湊").fill(source.normalized_sha256);
  await form.getByLabel("來源 1 關聯依據").fill(reason);
  await form.getByRole("checkbox", { name: /我已核對本次候選條款與來源關聯/ }).check();
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const saved = new Promise<void>(resolve => { committed = resolve; });
  let firstBody = ""; let firstKey = ""; let replayBody = ""; let replayKey = ""; let first!: Candidate; let attempts = 0;
  const path = "**/api/v1/dividends/candidates";
  await page.route(path, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    attempts++;
    const body = route.request().postData()!; const key = route.request().headers()["idempotency-key"]!;
    const response = await route.fetch(); expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
    if (attempts === 1) { firstBody = body; firstKey = key; first = await response.json() as Candidate; committed(); await held; await route.abort("failed"); }
    else { replayBody = body; replayKey = key; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "儲存候選資料" }).click(); await saved;
    const afterFirst = database(); expect(afterFirst.dividend_candidate_revisions).toHaveLength(1);
    for (const table of protectedTables) expect(afterFirst[table]).toEqual(before[table]);
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(sheet).toBeVisible(); await expect(form.getByLabel("候選每股金額（選填）")).toHaveValue("000.00000001");
    await expect(form.getByRole("button", { name: "取消" })).toBeDisabled();
  } finally { release(); }
  stage("LOST");
  await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  expect(first.terms.dps).toBe("0.00000001"); expect(first.terms.currency).toBeNull();
  const successorResponse = await page.request.patch(`/api/v1/dividends/candidates/${first.id}`, { headers: await headers(page, 1), data: {
    state: "DISMISSED", terms: first.terms, matches: [], change_reason: "Synthetic later candidate dismissal before historical replay.",
  } });
  expect(successorResponse.status()).toBe(200);
  const successor = await successorResponse.json() as Candidate; expect(successor.version).toBe(2);
  const beforeReplay = database();
  stage("REPLAY");
  await form.getByRole("button", { name: "儲存候選資料" }).click(); await expect(sheet).toHaveCount(0); await page.unroute(path);
  expect(attempts).toBe(2); expect(replayKey).toBe(firstKey); expect(replayBody).toBe(firstBody);
  expect(JSON.parse(firstBody)).toMatchObject({ terms: { dps: "000.00000001", currency: null }, matches: [{ observation_id: source.id, source_sha256: source.normalized_sha256, reason }], change_reason: "Synthetic candidate browser decision and retry." });
  expect(database()).toEqual(beforeReplay);
  await expect(page.getByRole("status").filter({ hasText: "伺服器已回覆候選決定" })).toContainText("可能是原鍵歷史回覆");
  stage("SECOND");
  const card = page.getByRole("article", { name: `股息候選 ${first.id}` });
  await card.getByRole("button", { name: "查看候選資料" }).click();
  await expect(card).toContainText(successor.revision_id);
  await card.getByRole("button", { name: "修訂候選資料" }).click();
  const revisionSheet = page.getByRole("dialog", { name: "修訂股息候選" });
  const revisionForm = revisionSheet.getByRole("form", { name: "修訂股息候選" });
  await expect(revisionForm).toContainText(successor.revision_id);
  await revisionForm.getByLabel("候選狀態").selectOption("OPEN");
  await revisionForm.getByLabel("候選每股金額（選填）").fill("0");
  await revisionForm.getByLabel("候選變更依據").fill("Synthetic candidate reopening with explicit observed source.");
  await revisionForm.getByRole("button", { name: "新增來源關聯" }).click();
  await revisionForm.getByLabel("來源 1 觀測識別碼").fill(source.id);
  await revisionForm.getByLabel("來源 1 原始雜湊").fill(source.normalized_sha256);
  await revisionForm.getByLabel("來源 1 關聯依據").fill(reason);
  await revisionForm.getByRole("checkbox", { name: /我已核對本次候選條款與來源關聯/ }).check();
  const written = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/candidates/${first.id}` && response.request().method() === "PATCH");
  await revisionForm.getByRole("button", { name: "儲存候選修訂" }).click();
  const writtenResponse = await written; expect(writtenResponse.status()).toBe(200); expect(writtenResponse.request().headers()["if-match"]).toBe('"2"');
  const reopened = await writtenResponse.json() as Candidate;
  expect(reopened.version).toBe(3); expect(reopened.terms.dps).toBe("0.00000000"); expect(reopened.terms.currency).toBeNull();
  await expect(revisionSheet).toHaveCount(0);
  const afterReopened = database();
  for (const table of protectedTables) expect(afterReopened[table]).toEqual(before[table]);
  stage("CHANGED_EVIDENCE");
  await expect(card).toContainText(reopened.revision_id);
  await card.getByRole("button", { name: "修訂候選資料" }).click();
  const staleSheet = page.getByRole("dialog", { name: "修訂股息候選" });
  const staleForm = staleSheet.getByRole("form", { name: "修訂股息候選" });
  await expect(staleForm).toContainText(reopened.revision_id);
  await staleForm.getByLabel("候選變更依據").fill("Synthetic stale candidate revision should not overwrite a successor.");
  await staleForm.getByRole("checkbox", { name: /我已核對本次候選條款與來源關聯/ }).check();
  const competing = await page.request.patch(`/api/v1/dividends/candidates/${first.id}`, { headers: await headers(page, 3), data: {
    state: "DISMISSED", terms: reopened.terms, matches: [], change_reason: "Synthetic competing decision after the editor opened.",
  } });
  expect(competing.status()).toBe(200);
  const beforeDenied = database();
  const denied = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/candidates/${first.id}` && response.request().method() === "PATCH");
  await staleForm.getByRole("button", { name: "儲存候選修訂" }).click();
  expect((await denied).status()).toBe(412);
  await expect(staleForm.getByRole("alert")).toContainText("不會覆蓋新版資料");
  await expect(staleForm.getByLabel("候選變更依據")).toHaveValue("Synthetic stale candidate revision should not overwrite a successor.");
  expect(database()).toEqual(beforeDenied);
  stage("COMPARE");
  await staleSheet.getByRole("button", { name: "關閉編輯" }).click();
  await expect(staleSheet).toHaveCount(0);
  await expect(create).toBeEnabled(); await create.click();
  const equivalentSheet = page.getByRole("dialog", { name: "新增股息候選" });
  const equivalentForm = equivalentSheet.getByRole("form", { name: "新增股息候選" });
  await equivalentForm.getByLabel("候選證券", { exact: true }).selectOption(security.id);
  await equivalentForm.getByLabel("候選每股金額（選填）").fill("000.0100");
  await equivalentForm.getByLabel("候選變更依據").fill("Synthetic equivalent exact decimal retry reason.");
  await equivalentForm.getByRole("checkbox", { name: /本次明確不列來源關聯/ }).check();
  await equivalentForm.getByRole("checkbox", { name: /我已核對本次候選條款與來源關聯/ }).check();
  let releaseEquivalent!: () => void; let committedEquivalent!: () => void;
  const equivalentHeld = new Promise<void>(resolve => { releaseEquivalent = resolve; });
  const equivalentSaved = new Promise<void>(resolve => { committedEquivalent = resolve; });
  let originalKey = ""; let equivalentKey = ""; let originalDecimal = ""; let equivalentDecimal = ""; let equivalentAttempts = 0;
  await page.route(path, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    equivalentAttempts++;
    const body = JSON.parse(route.request().postData()!) as components["schemas"]["CandidateWrite"];
    const key = route.request().headers()["idempotency-key"]!;
    const response = await route.fetch(); expect(response.status()).toBe(201);
    if (equivalentAttempts === 1) { originalKey = key; originalDecimal = body.terms.dps!; committedEquivalent(); await equivalentHeld; await route.abort("failed"); }
    else { equivalentKey = key; equivalentDecimal = body.terms.dps!; await route.fulfill({ response }); }
  });
  try { await equivalentForm.getByRole("button", { name: "儲存候選資料" }).click(); await equivalentSaved; }
  finally { releaseEquivalent(); }
  await expect(equivalentForm.getByRole("alert")).toContainText("網絡連線中斷");
  const beforeEquivalentReplay = database();
  await equivalentForm.getByLabel("候選每股金額（選填）").fill("0.01");
  await equivalentForm.getByRole("button", { name: "儲存候選資料" }).click();
  await expect(equivalentSheet).toHaveCount(0); await page.unroute(path);
  expect(equivalentAttempts).toBe(2); expect(equivalentKey).toBe(originalKey);
  expect(originalDecimal).toBe("000.0100"); expect(equivalentDecimal).toBe("0.01");
  expect(database()).toEqual(beforeEquivalentReplay);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
