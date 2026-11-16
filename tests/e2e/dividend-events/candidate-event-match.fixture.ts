import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, readEvent, seedCandidateSource, stage, type Event } from "./helpers";

type Candidate = components["schemas"]["CandidateRecord"];
type RejectionState = components["schemas"]["RejectionState"];
const financialTables = ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "expenses", "security_transfers", "fx_rates", "upstream_attempts", "jobs"];

test("candidate association pins the existing event, rejection and candidate through historical replay", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-candidate-match-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  stage("MASTER");
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: {
    exchange_code: "NASDAQ", display_ticker: "MATCHV", name: "Synthetic candidate match security", quote_currency: "USD",
  } });
  expect(securityResponse.status()).toBe(201);
  const security = await securityResponse.json() as { id: string };
  stage("SEED");
  const { source, reason } = await seedCandidateSource(page, security.id);
  const candidateBody = { state: "OPEN", terms: { security_id: security.id, ex_date: "2026-11-01", event_type: "SPECIAL",
    dps: "92233720368.54775807", currency: "USD", adjustment_basis: "AS_DECLARED" },
    matches: [{ observation_id: source.id, source_sha256: source.normalized_sha256, basis_assertion_id: null, reason }],
    change_reason: "Synthetic candidate with a conflicting source amount for association." };
  const createdCandidate = await page.request.post("/api/v1/dividends/candidates", { headers: await headers(page), data: candidateBody });
  expect(createdCandidate.status()).toBe(201);
  const candidate = await createdCandidate.json() as Candidate;
  const eventBody = { security_id: security.id, event_type: "REGULAR", ex_date: "2026-10-31", dps: "1", currency: "USD",
    adjustment_basis: "AS_DECLARED", citation: "Synthetic existing event citation", reason: "Synthetic existing event before candidate association." };
  const createdEvent = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: eventBody });
  expect(createdEvent.status()).toBe(201);
  const event = await createdEvent.json() as Event;
  const beforeRejection = await readEvent(page, event.id);
  const rejection = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, event.version), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic rejection reference",
    evidence_url: "https://example.test/reject?private=synthetic-secret#page", reason: "Synthetic rejection requiring explicit candidate resolution.",
  } });
  expect(rejection.status()).toBe(200); expect(await readEvent(page, event.id)).toEqual(beforeRejection);
  const rejectionResponse = await page.request.get(`/api/v1/dividends/events/${event.id}/rejection`);
  expect(rejectionResponse.status()).toBe(200); expect(rejectionResponse.headers()["cache-control"]).toBe("no-store");
  const rejected = await rejectionResponse.json() as RejectionState;
  expect(rejected.state).toBe("REJECTED");
  const before = database();
  for (const table of financialTables) expect(before[table]).toEqual([]);
  stage("NAVIGATE");
  await page.setViewportSize({ width: 360, height: 900 }); await page.goto("/dividends");
  const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
  await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const panel = article.getByRole("region", { name: "獨立拒絕狀態" }); await expect(panel).toContainText("此版本已記錄拒絕");
  const candidateRead = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/dividends/candidates" && response.request().method() === "GET");
  const opener = panel.getByRole("button", { name: "關聯候選至此事件" }); await opener.focus(); await page.keyboard.press("Enter");
  const candidatePage = await candidateRead; expect(candidatePage.status()).toBe(200); expect(candidatePage.headers()["cache-control"]).toBe("no-store");
  expect(new URL(candidatePage.url()).searchParams.get("security_id")).toBe(security.id); expect(new URL(candidatePage.url()).searchParams.get("state")).toBe("OPEN");
  const sheet = page.getByRole("dialog", { name: "關聯候選至現有事件" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  const passwordForm = sheet.getByRole("form", { name: "重新驗證" });
  await passwordForm.getByLabel("目前密碼").fill(password); await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  await expect(sheet).toContainText(event.current_revision_id); await expect(sheet).toContainText(rejected.rejection!.id);
  await expect(sheet).not.toContainText("synthetic-secret");
  const choice = sheet.getByRole("article", { name: `可選候選 ${candidate.id}` });
  await expect(choice).toContainText(candidate.revision_id); await expect(choice).toContainText("USD 92233720368.54775807");
  await choice.getByText("核對候選來源及歷史比較").click();
  await expect(choice).toContainText(source.id); await expect(choice).toContainText(source.normalized_sha256);
  await expect(sheet).toContainText("不是目前或過往事件關聯查詢");
  await choice.getByRole("button", { name: "選擇此候選" }).click();
  const form = sheet.getByRole("form", { name: "關聯候選至現有事件" });
  await expect(form).toContainText(candidate.revision_id); await expect(form).toContainText("USD 92233720368.54775807");
  await expect(form).toContainText("已有應收或投影歷史時將拒絕新版本");
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); const box = (await sheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  stage("FILL");
  await form.getByLabel("候選關聯依據").fill("Synthetic explicit association with the pinned existing event.");
  await form.getByRole("checkbox", { name: /我確認將所示候選關聯至此事件/ }).check();
  const beforeValidation = database(); await form.getByRole("button", { name: "記錄候選關聯" }).click();
  await expect(form.getByText("請確認明確處理所示拒絕。")).toBeVisible();
  await expect(form.getByText("請確認這不是文件驗證或財務資格。")).toBeVisible(); expect(database()).toEqual(beforeValidation);
  await form.getByRole("checkbox", { name: /我確認以此關聯明確處理所示拒絕/ }).check();
  await form.getByRole("checkbox", { name: /我明白這不是文件驗證/ }).check();
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const saved = new Promise<void>(resolve => { committed = resolve; });
  let firstBody = ""; let firstKey = ""; let match = ""; let retryBody = ""; let retryKey = ""; let first!: Event; let attempts = 0;
  const path = `**/api/v1/dividends/events/${event.id}/from-candidate`;
  await page.route(path, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    attempts++; const response = await route.fetch(); expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    const body = route.request().postData()!; const requestHeaders = route.request().headers();
    if (attempts === 1) { firstBody = body; firstKey = requestHeaders["idempotency-key"]!; match = requestHeaders["if-match"]!;
      first = await response.json() as Event; committed(); await held; await route.abort("failed"); }
    else { retryBody = body; retryKey = requestHeaders["idempotency-key"]!; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "記錄候選關聯" }).click(); await saved;
    expect(first.id).toBe(event.id); expect(first.version).toBe(event.version + 1); expect(first.candidate_revision_id).toBe(candidate.revision_id);
    expect(first.dps).toBe("92233720368.54775807"); expect(first.workflow_status).toBe("CONFLICT");
    expect(first.document_verification).toBe("NOT_ESTABLISHED"); expect(first.eligibility_scope).toBe("NOT_ESTABLISHED");
    const afterWrite = database(); expect(afterWrite.dividend_rejection_resolutions).toHaveLength(before.dividend_rejection_resolutions!.length + 1);
    for (const table of financialTables) expect(afterWrite[table]).toEqual(before[table]);
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click();
    await expect(sheet).toBeVisible(); await expect(form.getByLabel("候選關聯依據")).toHaveValue("Synthetic explicit association with the pinned existing event.");
  } finally { release(); }
  stage("LOST"); await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  const dismissed = await page.request.patch(`/api/v1/dividends/candidates/${candidate.id}`, { headers: await headers(page, candidate.version), data: {
    ...candidateBody, state: "DISMISSED", change_reason: "Synthetic dismissal after the association response was lost.",
  } });
  expect(dismissed.status()).toBe(200);
  const successor = await page.request.patch(`/api/v1/dividends/events/${event.id}`, { headers: await headers(page, first.version), data: {
    ...eventBody, revision_id: first.current_revision_id, resolve_rejection_id: null, dps: "1.00000001",
    reason: "Synthetic later manual event revision after the association response was lost.", confirm_unlock: false, confirm_reopen: false,
  } });
  expect(successor.status()).toBe(200);
  await passwordForm.getByLabel("目前密碼").fill(password); await passwordForm.getByRole("button", { name: "確認目前密碼" }).click();
  await expect(passwordForm).toContainText("已重新驗證");
  const beforeReplay = database(); stage("REPLAY");
  await form.getByRole("button", { name: "記錄候選關聯" }).click(); await expect(sheet).toHaveCount(0); await page.unroute(path);
  expect(attempts).toBe(2); expect(retryKey).toBe(firstKey); expect(retryBody).toBe(firstBody); expect(match).toBe(`"${event.version}"`);
  expect(JSON.parse(firstBody)).toEqual({ candidate_id: candidate.id, candidate_revision_id: candidate.revision_id, candidate_version: candidate.version,
    revision_id: event.current_revision_id, resolve_rejection_id: rejected.rejection!.id, confirmation: "MATCH_EXISTING_EVENT",
    reason: "Synthetic explicit association with the pinned existing event." });
  expect(database()).toEqual(beforeReplay); expect((await readEvent(page, event.id)).etag).toBe('"3"');
  await expect(page.getByRole("status").filter({ hasText: "伺服器已回覆候選關聯決定" })).toContainText("可能是原鍵回覆");

  stage("SECOND");
  const otherCandidateResponse = await page.request.post("/api/v1/dividends/candidates", { headers: await headers(page), data: {
    ...candidateBody, change_reason: "Synthetic separate candidate for rejected and stale races.",
  } });
  expect(otherCandidateResponse.status()).toBe(201); const otherCandidate = await otherCandidateResponse.json() as Candidate;
  const otherEventResponse = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    ...eventBody, reason: "Synthetic second event for competing rejection and candidate revision.",
  } });
  expect(otherEventResponse.status()).toBe(201); const otherEvent = await otherEventResponse.json() as Event;
  await page.goto("/dividends");
  const otherArticle = page.getByRole("article", { name: `股息事件 ${otherEvent.id}` });
  await otherArticle.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const otherPanel = otherArticle.getByRole("region", { name: "獨立拒絕狀態" }); await expect(otherPanel).toContainText("此版本未記錄拒絕");
  await otherPanel.getByRole("button", { name: "關聯候選至此事件" }).click();
  const raceSheet = page.getByRole("dialog", { name: "關聯候選至現有事件" });
  await raceSheet.getByRole("article", { name: `可選候選 ${otherCandidate.id}` }).getByRole("button", { name: "選擇此候選" }).click();
  const raceForm = raceSheet.getByRole("form", { name: "關聯候選至現有事件" });
  await raceForm.getByLabel("候選關聯依據").fill("Synthetic unpinned association must not resolve a later rejection.");
  await raceForm.getByRole("checkbox", { name: /我確認將所示候選關聯至此事件/ }).check();
  await raceForm.getByRole("checkbox", { name: /我明白這不是文件驗證/ }).check();
  const competingRejection = await page.request.post(`/api/v1/dividends/${otherEvent.id}/verify`, { headers: await headers(page, otherEvent.version), data: {
    revision_id: otherEvent.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic competing rejection",
    reason: "Synthetic rejection created after a clear candidate association read.",
  } });
  expect(competingRejection.status()).toBe(200); const beforeDenied = database(); stage("CHANGED_EVIDENCE");
  const denied = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/events/${otherEvent.id}/from-candidate` && response.request().method() === "POST");
  await raceForm.getByRole("button", { name: "記錄候選關聯" }).click(); expect((await denied).status()).toBe(409);
  await expect(raceForm.getByRole("alert")).toContainText("已有拒絕");
  await expect(raceForm.getByLabel("候選關聯依據")).toHaveValue("Synthetic unpinned association must not resolve a later rejection."); expect(database()).toEqual(beforeDenied);
  await raceSheet.getByRole("button", { name: "關閉編輯" }).click();

  await page.goto("/dividends");
  const staleArticle = page.getByRole("article", { name: `股息事件 ${otherEvent.id}` });
  await staleArticle.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
  const stalePanel = staleArticle.getByRole("region", { name: "獨立拒絕狀態" }); await expect(stalePanel).toContainText("此版本已記錄拒絕");
  await stalePanel.getByRole("button", { name: "關聯候選至此事件" }).click();
  const staleSheet = page.getByRole("dialog", { name: "關聯候選至現有事件" });
  await staleSheet.getByRole("article", { name: `可選候選 ${otherCandidate.id}` }).getByRole("button", { name: "選擇此候選" }).click();
  const staleForm = staleSheet.getByRole("form", { name: "關聯候選至現有事件" });
  await staleForm.getByLabel("候選關聯依據").fill("Synthetic stale candidate association remains uncommitted.");
  await staleForm.getByRole("checkbox", { name: /我確認將所示候選關聯至此事件/ }).check();
  await staleForm.getByRole("checkbox", { name: /我確認以此關聯明確處理所示拒絕/ }).check();
  await staleForm.getByRole("checkbox", { name: /我明白這不是文件驗證/ }).check();
  const competingCandidate = await page.request.patch(`/api/v1/dividends/candidates/${otherCandidate.id}`, { headers: await headers(page, otherCandidate.version), data: {
    ...candidateBody, state: "DISMISSED", change_reason: "Synthetic competing dismissal after candidate selection.",
  } });
  expect(competingCandidate.status()).toBe(200); const beforeStale = database();
  const staleResponse = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/events/${otherEvent.id}/from-candidate` && response.request().method() === "POST");
  await staleForm.getByRole("button", { name: "記錄候選關聯" }).click(); expect((await staleResponse).status()).toBe(412);
  await expect(staleForm.getByRole("alert")).toContainText("不會覆蓋新版資料");
  await expect(staleForm.getByLabel("候選關聯依據")).toHaveValue("Synthetic stale candidate association remains uncommitted."); expect(database()).toEqual(beforeStale);
  const after = database(); for (const table of financialTables) expect(after[table]).toEqual(before[table]);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
