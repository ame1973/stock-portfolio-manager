import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, stage } from "./helpers";

type Candidate = components["schemas"]["CandidateRecord"];

test("isolated candidate review preserves unknowns, versions and read-only boundaries", async ({ page }) => {
  stage("SETUP");
  const password = `synthetic-candidate-browser-${randomUUID()}`;
  await page.goto("/");
  const bootstrap = execFileSync("uv", ["run", "spm-manage", "setup-token"], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap); await page.getByLabel(/設定密碼/).fill(password); await page.getByLabel("再次輸入密碼").fill(password);
  await page.getByRole("button", { name: "建立帳戶", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  const reauth = await page.request.post("/api/v1/auth/reauth", { headers: await headers(page), data: { password } });
  expect(reauth.status()).toBe(200);
  stage("MASTER");
  const securityResponse = await page.request.post("/api/v1/securities", { headers: await headers(page), data: {
    exchange_code: "NASDAQ", display_ticker: "CANDE2E", name: "Synthetic candidate security", quote_currency: "USD",
  } });
  expect(securityResponse.status()).toBe(201);
  const security = await securityResponse.json() as { id: string };
  stage("SEED");
  const reason = "Synthetic non-network source citation for candidate history.";
  const symbol = await page.request.post(`/api/v1/securities/${security.id}/provider-symbols`, { headers: await headers(page), data: {
    provider_id: "manual", provider_symbol: "CANDE2E.CITED", effective_from: "2020-01-01", reason,
  } });
  expect(symbol.status()).toBe(201);
  const policy = { provider_id: "manual", enabled: false, terms_acknowledged: true, terms_reference: "Synthetic local citation permission",
    terms_reason: reason, requests_per_minute: null, requests_per_day: null, raw_payload_storage: false };
  const profile = await page.request.post("/api/v1/provider-profiles", { headers: await headers(page), data: { policy, reason } });
  expect(profile.status()).toBe(201);
  const enable = await page.request.patch("/api/v1/provider-profiles/manual", { headers: await headers(page, 1), data: {
    policy: { ...policy, enabled: true }, clear_credential: false, reason,
  } });
  expect(enable.status()).toBe(200);
  const cited = await page.request.post("/api/v1/providers/cited-observations", { headers: await headers(page), data: {
    operation: "DIVIDENDS", provider_id: "manual", security_id: security.id, provider_symbol: "CANDE2E.CITED",
    ex_date: "2026-11-01", event_type: "SPECIAL", dps: "0", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: { citation: "Synthetic source reference", reason, url: "https://example.test/citation?private=synthetic-secret#page" },
  } });
  expect(cited.status()).toBe(201);
  const createdSource = await cited.json() as components["schemas"]["CitedSourceCreated"];
  const observations = await page.request.get(`/api/v1/dividends/observations?security_id=${security.id}`);
  expect(observations.status()).toBe(200); expect(observations.headers()["cache-control"]).toBe("no-store");
  const sourceRows = await observations.json() as { items: components["schemas"]["DividendObservationRecord"][] };
  const source = sourceRows.items.find(row => row.id === createdSource.observation_id)!;
  expect(source.normalized_sha256).toMatch(/^[a-f0-9]{64}$/);
  const firstBody = { state: "OPEN", terms: { security_id: security.id, ex_date: "2026-11-01", event_type: "SPECIAL",
    dps: "0", currency: "USD", adjustment_basis: "AS_DECLARED" },
    matches: [{ observation_id: source.id, source_sha256: source.normalized_sha256, basis_assertion_id: null, reason }],
    change_reason: "Synthetic original candidate with explicitly recorded zero." };
  const created = await page.request.post("/api/v1/dividends/candidates", { headers: await headers(page), data: firstBody });
  expect(created.status()).toBe(201); expect(created.headers()["cache-control"]).toBe("no-store");
  const first = await created.json() as Candidate;
  expect(first.terms.dps).toBe("0.00000000");
  const linked = await page.request.post("/api/v1/dividends/events/from-candidate", { headers: await headers(page), data: {
    candidate_id: first.id, candidate_revision_id: first.revision_id, candidate_version: first.version,
    confirmation: "CREATE_DISTINCT_EVENT", reason: "Synthetic distinct event linked to the candidate's first revision.",
  } });
  expect(linked.status()).toBe(201);
  const linkedEvent = await linked.json() as components["schemas"]["DividendEventRecord"];
  expect(linkedEvent.candidate_revision_id).toBe(first.revision_id);
  const revised = await page.request.patch(`/api/v1/dividends/candidates/${first.id}`, { headers: await headers(page, 1), data: {
    ...firstBody, terms: { ...firstBody.terms, ex_date: null, dps: "92233720368.54775807", currency: null, adjustment_basis: "UNKNOWN" },
    change_reason: "Synthetic candidate revision with an unknown date and currency.",
  } });
  expect(revised.status()).toBe(200); expect(revised.headers()["cache-control"]).toBe("no-store");
  const current = await revised.json() as Candidate;
  expect(current.id).toBe(first.id); expect(current.revision_id).not.toBe(first.revision_id); expect(current.version).toBe(2);
  expect(current.terms.dps).toBe("92233720368.54775807"); expect(current.terms.currency).toBeNull();
  expect(current.canonical_adoption).toBe("NOT_ADOPTED"); expect(current.canonical_adoption_scope).toBe("RESPONSE_ONLY_NOT_LINKAGE");
  const missingResponse = await page.request.post("/api/v1/dividends/candidates", { headers: await headers(page), data: {
    ...firstBody, state: "DISMISSED", terms: { ...firstBody.terms, ex_date: null, dps: null, currency: null, adjustment_basis: "UNKNOWN" }, matches: [],
    change_reason: "Synthetic candidate with an unknown amount, kept separate from zero.",
  } });
  expect(missingResponse.status()).toBe(201);
  const missing = await missingResponse.json() as Candidate;
  const before = database();
  expect(before.dividend_candidate_revisions).toHaveLength(3); expect(before.dividend_events).toHaveLength(1);
  for (const table of ["dividend_receipts", "dividend_entitlements", "dividend_projection_versions", "cash_ledger_entries", "upstream_attempts", "jobs"]) expect(before[table]).toEqual([]);
  const writes: string[] = []; const candidateCache: string[] = [];
  page.on("request", request => { if (new URL(request.url()).pathname.startsWith("/api/") && !["GET", "HEAD"].includes(request.method())) writes.push(request.method()); });
  page.on("response", response => { if (new URL(response.url()).pathname.startsWith("/api/v1/dividends/candidates")) candidateCache.push(response.headers()["cache-control"] ?? ""); });
  stage("NAVIGATE");
  await page.setViewportSize({ width: 360, height: 900 });
  await page.goto("/dividends");
  const link = page.getByRole("link", { name: "查看股息候選資料" });
  await link.focus(); await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "股息候選資料" })).toBeVisible();
  const mobile = page.getByRole("navigation", { name: "手機主要導覽" });
  await expect(mobile.getByRole("link")).toHaveCount(5);
  await expect(mobile.getByRole("link", { name: "股息事件" })).toHaveAttribute("aria-current", "page");
  const card = page.getByRole("article", { name: `股息候選 ${current.id}` });
  const unknown = page.getByRole("article", { name: `股息候選 ${missing.id}` });
  await expect(card).toContainText("除淨日 未知"); await expect(card).toContainText("幣別未知 92233720368.54775807");
  await expect(unknown).toContainText("幣別未知 未知");
  await card.getByRole("button", { name: "查看候選資料" }).focus(); await page.keyboard.press("Enter");
  await expect(card).toContainText("不是目前或過往事件關聯查詢");
  await expect(card).toContainText(source.id); await expect(card).toContainText(source.normalized_sha256); await expect(card).toContainText(reason);
  await expect(card.getByRole("region", { name: "歷史來源比較" })).toContainText(source.id);
  await unknown.getByRole("button", { name: "查看候選資料" }).click();
  await expect(unknown).toContainText("不推定不存在來源");
  await expect(unknown.getByRole("region", { name: "歷史來源比較" })).toContainText("NO_MATCHED_EVIDENCE");
  expect(await page.locator("body").innerText()).not.toContain("synthetic-secret");
  const historyRead = page.waitForResponse(response => new URL(response.url()).pathname === `/api/v1/dividends/candidates/${current.id}/revisions`);
  await card.getByRole("button", { name: "查看候選修訂歷史" }).focus(); await page.keyboard.press("Enter");
  expect((await historyRead).headers()["cache-control"]).toBe("no-store");
  const history = card.getByRole("region", { name: "候選修訂歷史" });
  await expect(history).toContainText("歷史版本 1"); await expect(history).toContainText("USD 0.00000000");
  await expect(history).toContainText(first.revision_id); await expect(history).toContainText(current.revision_id);
  await page.getByText("篩選股息候選", { exact: true }).click();
  await page.getByLabel("候選狀態").selectOption("DISMISSED");
  await expect(unknown).toBeVisible(); await expect(card).toHaveCount(0);
  await page.getByRole("button", { name: "清除候選篩選" }).click();
  await expect(card).toBeVisible();
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    stage(width === 360 ? "RESIZE_MOBILE" : width === 768 ? "RESIZE_TABLET" : "RESIZE_DESKTOP");
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/dividends/candidates");
    stage("RESIZE_OPEN");
    if (width >= 1024) {
      const row = page.locator(`tr[data-record-id="${current.id}"]`);
      await row.getByRole("button", { name: "查看原值與操作" }).click();
    } else {
      await card.getByRole("button", { name: "查看候選資料" }).click();
    }
    stage("RESIZE_DETAIL");
    await expect(card).toContainText(current.revision_id);
    await expect(card).toContainText("幣別未知 92233720368.54775807");
    await expect(card).toContainText("不是目前或過往事件關聯查詢");
    stage("RESIZE_SCROLL");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  stage("COMPARE");
  expect(candidateCache.length).toBeGreaterThan(0); expect(candidateCache.every(value => value === "no-store")).toBe(true);
  expect(writes).toEqual([]); expect(database()).toEqual(before);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);
  expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
