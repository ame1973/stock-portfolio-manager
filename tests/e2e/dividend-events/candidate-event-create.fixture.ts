import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, seedCandidateSource, stage } from "./helpers";

type Candidate = components["schemas"]["CandidateRecord"];
type Event = components["schemas"]["DividendEventRecord"];
const financialTables = ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "expenses", "security_transfers", "fx_rates", "upstream_attempts", "jobs"];

test("distinct candidate event preserves exact source pins, reauthentication and historical replay", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-candidate-event-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  stage("MASTER");
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: {
    exchange_code: "NASDAQ", display_ticker: "CANDEV", name: "Synthetic distinct event security", quote_currency: "USD",
  } });
  expect(securityResponse.status()).toBe(201);
  const security = await securityResponse.json() as { id: string };
  stage("SEED");
  const { source, reason } = await seedCandidateSource(page, security.id);
  const body = { state: "OPEN", terms: { security_id: security.id, ex_date: "2026-11-01", event_type: "SPECIAL",
    dps: "92233720368.54775807", currency: "USD", adjustment_basis: "AS_DECLARED" },
    matches: [{ observation_id: source.id, source_sha256: source.normalized_sha256, basis_assertion_id: null, reason }],
    change_reason: "Synthetic candidate with a conflicting source amount for review." };
  const created = await page.request.post("/api/v1/dividends/candidates", { headers: await headers(page), data: body });
  expect(created.status()).toBe(201);
  const target = await created.json() as Candidate;
  const before = database();
  for (const table of financialTables) expect(before[table]).toEqual([]);
  stage("NAVIGATE");
  await page.setViewportSize({ width: 360, height: 900 }); await page.goto("/dividends/candidates");
  const card = page.getByRole("article", { name: `股息候選 ${target.id}` });
  await card.getByRole("button", { name: "查看候選資料" }).click();
  const opener = card.getByRole("button", { name: "建立獨立股息事件" });
  await expect(opener).toBeEnabled(); await opener.focus(); await page.keyboard.press("Enter");
  const sheet = page.getByRole("dialog", { name: "由候選建立獨立股息事件" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  const passwordForm = sheet.getByRole("form", { name: "重新驗證" });
  await passwordForm.getByLabel("目前密碼").fill(password); await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  const form = sheet.getByRole("form", { name: "由候選建立獨立股息事件" });
  await expect(form).toContainText(target.revision_id); await expect(form).toContainText("每股 USD 92233720368.54775807");
  await expect(form).toContainText("不是目前或過往事件關聯查詢");
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    const bounds = (await sheet.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0); expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  stage("FILL");
  await form.getByLabel("獨立事件決定依據").fill("\u0085Synthetic distinct event based on the fixed candidate.\u0085");
  await form.getByRole("checkbox", { name: /我確認建立獨立股息事件/ }).check();
  const beforeValidation = database();
  await form.getByRole("button", { name: "建立獨立事件" }).click();
  await expect(form.getByRole("alert")).toContainText("請確認這不是文件驗證或財務資格");
  expect(database()).toEqual(beforeValidation);
  await form.getByRole("checkbox", { name: /我明白這不是文件驗證/ }).check();
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const saved = new Promise<void>(resolve => { committed = resolve; });
  let firstBody = ""; let firstKey = ""; let retryBody = ""; let retryKey = ""; let first!: Event; let attempts = 0;
  const path = "**/api/v1/dividends/events/from-candidate";
  await page.route(path, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    attempts++;
    const response = await route.fetch(); expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
    const bodyText = route.request().postData()!; const key = route.request().headers()["idempotency-key"]!;
    if (attempts === 1) { firstBody = bodyText; firstKey = key; first = await response.json() as Event; committed(); await held; await route.abort("failed"); }
    else { retryBody = bodyText; retryKey = key; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "建立獨立事件" }).click(); await saved;
    const afterWrite = database(); expect(afterWrite.dividend_events).toHaveLength(1);
    for (const table of financialTables) expect(afterWrite[table]).toEqual(before[table]);
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(sheet).toBeVisible(); await expect(form.getByLabel("獨立事件決定依據")).toHaveValue("\u0085Synthetic distinct event based on the fixed candidate.\u0085");
    await expect(form.getByRole("button", { name: "取消" })).toBeDisabled();
  } finally { release(); }
  stage("LOST");
  await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  expect(first.candidate_revision_id).toBe(target.revision_id); expect(first.dps).toBe("92233720368.54775807");
  expect(first.eligibility_scope).toBe("NOT_ESTABLISHED"); expect(first.document_verification).toBe("NOT_ESTABLISHED");
  const dismissedResponse = await page.request.patch(`/api/v1/dividends/candidates/${target.id}`, { headers: await headers(page, 1), data: {
    ...body, state: "DISMISSED", change_reason: "Synthetic candidate dismissal after event response was lost.",
  } });
  expect(dismissedResponse.status()).toBe(200);
  await passwordForm.getByLabel("目前密碼").fill(password); await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  const beforeReplay = database();
  stage("REPLAY");
  await form.getByRole("button", { name: "建立獨立事件" }).click(); await expect(sheet).toHaveCount(0); await page.unroute(path);
  expect(attempts).toBe(2); expect(retryKey).toBe(firstKey); expect(retryBody).toBe(firstBody);
  expect(JSON.parse(firstBody)).toEqual({ candidate_id: target.id, candidate_revision_id: target.revision_id, candidate_version: target.version,
    confirmation: "CREATE_DISTINCT_EVENT", reason: "Synthetic distinct event based on the fixed candidate." });
  expect(database()).toEqual(beforeReplay);
  await expect(page.getByRole("status").filter({ hasText: "伺服器已回覆獨立事件" })).toContainText("可能是原鍵歷史回覆");
  stage("SECOND");
  const reopenedResponse = await page.request.patch(`/api/v1/dividends/candidates/${target.id}`, { headers: await headers(page, 2), data: {
    ...body, change_reason: "Synthetic reopening to check a previously linked candidate.",
  } });
  expect(reopenedResponse.status()).toBe(200);
  const reopened = await reopenedResponse.json() as Candidate;
  await page.goto("/dividends/candidates");
  const reused = page.getByRole("article", { name: `股息候選 ${target.id}` });
  await reused.getByRole("button", { name: "查看候選資料" }).click();
  await expect(reused).toContainText(reopened.revision_id);
  await reused.getByRole("button", { name: "建立獨立股息事件" }).click();
  const duplicateSheet = page.getByRole("dialog", { name: "由候選建立獨立股息事件" });
  const duplicateForm = duplicateSheet.getByRole("form", { name: "由候選建立獨立股息事件" });
  await duplicateForm.getByLabel("獨立事件決定依據").fill("Synthetic duplicate event attempt must be rejected.");
  await duplicateForm.getByRole("checkbox", { name: /我確認建立獨立股息事件/ }).check();
  await duplicateForm.getByRole("checkbox", { name: /我明白這不是文件驗證/ }).check();
  const beforeDuplicate = database();
  const duplicateResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/dividends/events/from-candidate" && response.request().method() === "POST");
  await duplicateForm.getByRole("button", { name: "建立獨立事件" }).click();
  expect((await duplicateResponse).status()).toBe(409);
  await expect(duplicateForm.getByRole("alert")).toContainText("此候選已關聯另一事件");
  expect(database()).toEqual(beforeDuplicate);
  await duplicateSheet.getByRole("button", { name: "關閉編輯" }).click();
  stage("CHANGED_EVIDENCE");
  const separateResponse = await page.request.post("/api/v1/dividends/candidates", { headers: await headers(page), data: {
    ...body, change_reason: "Synthetic second candidate used for a version race.",
  } });
  expect(separateResponse.status()).toBe(201);
  const separate = await separateResponse.json() as Candidate;
  await page.goto("/dividends/candidates");
  const raceCard = page.getByRole("article", { name: `股息候選 ${separate.id}` });
  await raceCard.getByRole("button", { name: "查看候選資料" }).click();
  await raceCard.getByRole("button", { name: "建立獨立股息事件" }).click();
  const raceSheet = page.getByRole("dialog", { name: "由候選建立獨立股息事件" });
  const raceForm = raceSheet.getByRole("form", { name: "由候選建立獨立股息事件" });
  await raceForm.getByLabel("獨立事件決定依據").fill("Synthetic stale candidate event decision remains uncommitted.");
  await raceForm.getByRole("checkbox", { name: /我確認建立獨立股息事件/ }).check();
  await raceForm.getByRole("checkbox", { name: /我明白這不是文件驗證/ }).check();
  const competing = await page.request.patch(`/api/v1/dividends/candidates/${separate.id}`, { headers: await headers(page, 1), data: {
    ...body, state: "DISMISSED", change_reason: "Synthetic competing dismissal after event form opened.",
  } });
  expect(competing.status()).toBe(200);
  const beforeDenied = database();
  const denied = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/dividends/events/from-candidate" && response.request().method() === "POST");
  await raceForm.getByRole("button", { name: "建立獨立事件" }).click();
  expect((await denied).status()).toBe(412);
  await expect(raceForm.getByRole("alert")).toContainText("不會覆蓋新版資料");
  await expect(raceForm.getByLabel("獨立事件決定依據")).toHaveValue("Synthetic stale candidate event decision remains uncommitted.");
  expect(database()).toEqual(beforeDenied);
  const after = database();
  for (const table of financialTables) expect(after[table]).toEqual(before[table]);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
