import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, seedCandidateSource, stage } from "./helpers";

type Candidate = components["schemas"]["CandidateRecord"];
type Basis = components["schemas"]["DividendBasisRecord"];
type Observation = components["schemas"]["DividendObservationRecord"];
const financialTables = ["dividend_events", "dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "trades", "expenses", "security_transfers", "fx_rates", "upstream_attempts", "jobs"];

test("candidate basis selection pins a historical declaration and rechecks later action changes", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-basis-picker-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click(); await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect((await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } })).status()).toBe(200);
  stage("MASTER");
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: {
    exchange_code: "NASDAQ", display_ticker: "BASISP", name: "Synthetic basis picker security", quote_currency: "USD",
  } });
  expect(securityResponse.status()).toBe(201); const security = await securityResponse.json() as { id: string };
  stage("SEED"); await seedCandidateSource(page, security.id);
  const citationReason = "Synthetic explicit split-adjusted citation for a basis picker.";
  const cited = await page.request.post("/api/v1/providers/cited-observations", { headers: await headers(page), data: {
    operation: "DIVIDENDS", provider_id: "manual", security_id: security.id, provider_symbol: "CANDE2E.CITED",
    ex_date: "2026-11-01", event_type: "SPECIAL", dps: "0.6", currency: "USD", adjustment_basis: "SPLIT_ADJUSTED",
    citation: { citation: "Synthetic split-adjusted source", reason: citationReason, url: null },
  } });
  expect(cited.status()).toBe(201); const createdSource = await cited.json() as components["schemas"]["CitedSourceCreated"];
  const sourceResponse = await page.request.get(`/api/v1/dividends/observations?security_id=${security.id}`);
  expect(sourceResponse.status()).toBe(200);
  const sourceRows = await sourceResponse.json() as { items: Observation[] };
  const source = sourceRows.items.find(item => item.id === createdSource.observation_id)!;
  expect(source.adjustment_basis).toBe("SPLIT_ADJUSTED");
  const actionBody = { security_id: security.id, action_type: "SPLIT", effective_date: "2026-11-01",
    ratio_numerator: "9223372036854775807", ratio_denominator: "9223372036854775806", status: "CONFIRMED",
    reason: "Synthetic split with an exact large rational factor for basis review." };
  const actionResponse = await page.request.post("/api/v1/corporate-actions", { headers: await headers(page), data: actionBody });
  expect(actionResponse.status()).toBe(201);
  const originalAction = await actionResponse.json() as { id: string; version: number };
  const basisResponse = await page.request.post("/api/v1/dividends/basis-assertions", { headers: await headers(page), data: {
    observation_id: source.id, source_sha256: source.normalized_sha256, share_basis_date: "2026-11-02",
    source_basis: "AFTER_EFFECTIVE_DATE_ACTIONS", target_basis: "PRE_EX_DATE_EVENTS", complete: true, split_only: true,
    actions: [{ action_id: originalAction.id, version: originalAction.version }],
    citation: { citation: "Synthetic declaration scope", reason: "Synthetic explicit complete split-only basis declaration.", url: "https://example.test/basis?private=synthetic-secret#scope" },
  } });
  expect(basisResponse.status()).toBe(201); const basis = await basisResponse.json() as Basis;
  expect(basis.source_sha256).toBe(source.normalized_sha256); expect(basis.actions).toHaveLength(1); expect(basis.citation.url_withheld).toBe(true);
  const before = database(); for (const table of financialTables) expect(before[table]).toEqual([]);
  stage("NAVIGATE"); await page.setViewportSize({ width: 360, height: 900 }); await page.goto("/dividends/candidates");
  await page.getByRole("button", { name: "新增股息候選" }).click();
  const sheet = page.getByRole("dialog", { name: "新增股息候選" }); const form = sheet.getByRole("form", { name: "新增股息候選" });
  await expect(sheet.getByRole("button", { name: "關閉編輯" })).toBeFocused();
  await form.getByLabel("候選證券", { exact: true }).selectOption(security.id); await form.getByRole("button", { name: "瀏覽來源觀測" }).click();
  await form.getByRole("article", { name: `可選來源 ${source.id}` }).getByRole("button", { name: "加入此來源" }).click();
  const browse = form.getByRole("button", { name: "瀏覽來源 1 基準聲明" }); await expect(browse).toBeEnabled();
  const read = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/dividends/basis-assertions" && response.request().method() === "GET");
  await browse.focus(); await page.keyboard.press("Enter");
  const listed = await read; expect(listed.status()).toBe(200); expect(listed.headers()["cache-control"]).toBe("no-store");
  const query = new URL(listed.url()).searchParams; expect(query.get("observation_id")).toBe(source.id); expect(query.get("security_id")).toBe(security.id);
  const card = form.getByRole("article", { name: `可選基準 ${basis.id}` });
  await expect(card).toContainText(source.id); await expect(card).toContainText(source.normalized_sha256);
  await expect(card).toContainText("不是市場歷史或文件驗證"); await expect(card).toContainText(originalAction.id);
  await expect(card).toContainText("9223372036854775807 / 9223372036854775806");
  await expect(card).toContainText("私密引用連結未提供"); await expect(card).not.toContainText("synthetic-secret");
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); const box = (await sheet.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.setViewportSize({ width: 360, height: 900 });
  const beforeReload = database();
  const reloaded = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/dividends/basis-assertions" && response.request().method() === "GET");
  await form.getByRole("button", { name: "重新載入可選基準" }).click(); expect((await reloaded).status()).toBe(200); expect(database()).toEqual(beforeReload);
  await card.getByRole("button", { name: "使用此基準聲明" }).click();
  await expect(form.getByLabel("來源 1 基準聲明識別碼（選填）")).toHaveValue(basis.id);
  await expect(form.getByLabel("來源 1 觀測識別碼")).toHaveValue(source.id); await expect(form.getByLabel("來源 1 原始雜湊")).toHaveValue(source.normalized_sha256);
  await expect(form.getByLabel("來源 1 關聯依據")).toHaveValue(""); await expect(form.getByLabel("候選每股金額（選填）")).toHaveValue("");
  await expect(form.getByLabel("候選股息幣別")).toHaveValue(""); await expect(card.getByRole("button", { name: "已選此基準" })).toBeDisabled();
  stage("FILL"); await form.getByLabel("來源 1 關聯依據").fill("Synthetic selected source and basis association.");
  await form.getByLabel("候選變更依據").fill("Synthetic candidate using the explicitly selected basis.");
  await form.getByRole("checkbox", { name: /我已核對本次候選條款與來源關聯/ }).check();
  let release!: () => void; let committed!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; }); const saved = new Promise<void>(resolve => { committed = resolve; });
  let first!: Candidate; let firstBody = ""; let firstKey = ""; let retryBody = ""; let retryKey = ""; let attempts = 0;
  const path = "**/api/v1/dividends/candidates";
  await page.route(path, async route => {
    if (route.request().method() !== "POST") { await route.continue(); return; }
    attempts++; const response = await route.fetch(); expect(response.status()).toBe(201); expect(response.headers()["cache-control"]).toBe("no-store");
    const body = route.request().postData()!; const key = route.request().headers()["idempotency-key"]!;
    if (attempts === 1) { first = await response.json() as Candidate; firstBody = body; firstKey = key; committed(); await held; await route.abort("failed"); }
    else { retryBody = body; retryKey = key; await route.fulfill({ response }); }
  });
  stage("COMMIT_HELD");
  try {
    await form.getByRole("button", { name: "儲存候選資料" }).click(); await saved;
    expect(first.matches[0]!.basis_assertion_id).toBe(basis.id); expect(first.terms.dps).toBeNull(); expect(first.terms.currency).toBeNull();
    await page.keyboard.press("Escape"); await sheet.getByRole("button", { name: "關閉編輯" }).click(); await expect(sheet).toBeVisible();
  } finally { release(); }
  stage("LOST"); await expect(form.getByRole("alert")).toContainText("網絡連線中斷");
  const action = await page.request.patch(`/api/v1/corporate-actions/${originalAction.id}`, { headers: await headers(page, originalAction.version), data: {
    ...actionBody, ratio_numerator: "3", ratio_denominator: "2", reason: "Synthetic later split revision that changes the retained basis scope.",
  } });
  expect(action.status()).toBe(200); const beforeReplay = database(); stage("REPLAY");
  await form.getByRole("button", { name: "儲存候選資料" }).click(); await expect(sheet).toHaveCount(0); await page.unroute(path);
  expect(attempts).toBe(2); expect(retryKey).toBe(firstKey); expect(retryBody).toBe(firstBody); expect(database()).toEqual(beforeReplay);
  const body = JSON.parse(firstBody) as components["schemas"]["CandidateWrite"];
  expect(body.matches).toEqual([{ observation_id: source.id, source_sha256: source.normalized_sha256, basis_assertion_id: basis.id,
    reason: "Synthetic selected source and basis association." }]);
  expect(body.terms).toMatchObject({ dps: null, currency: null, ex_date: null, adjustment_basis: "UNKNOWN" });

  stage("CHANGED_EVIDENCE"); await page.getByRole("button", { name: "新增股息候選" }).click();
  const staleSheet = page.getByRole("dialog", { name: "新增股息候選" }); const stale = staleSheet.getByRole("form", { name: "新增股息候選" });
  await stale.getByLabel("候選證券", { exact: true }).selectOption(security.id); await stale.getByRole("button", { name: "瀏覽來源觀測" }).click();
  await stale.getByRole("article", { name: `可選來源 ${source.id}` }).getByRole("button", { name: "加入此來源" }).click();
  await stale.getByRole("button", { name: "瀏覽來源 1 基準聲明" }).click();
  const staleCard = stale.getByRole("article", { name: `可選基準 ${basis.id}` });
  await expect(staleCard).toContainText("行動版本由伺服器重新核對"); await staleCard.getByRole("button", { name: "使用此基準聲明" }).click();
  await stale.getByLabel("來源 1 關聯依據").fill("Synthetic stale basis selection must be rejected.");
  await stale.getByLabel("候選變更依據").fill("Synthetic candidate attempted after the recorded action changed.");
  await stale.getByRole("checkbox", { name: /我已核對本次候選條款與來源關聯/ }).check();
  const beforeDenied = database(); const denied = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/dividends/candidates" && response.request().method() === "POST");
  await stale.getByRole("button", { name: "儲存候選資料" }).click(); expect((await denied).status()).toBe(409);
  await expect(stale.getByRole("alert")).toContainText("候選基準與來源不相符或已變更");
  await expect(stale.getByLabel("來源 1 基準聲明識別碼（選填）")).toHaveValue(basis.id); expect(database()).toEqual(beforeDenied);
  const after = database(); for (const table of financialTables) expect(after[table]).toEqual(before[table]);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
