import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import { checkedRevisionPreview } from "../../../apps/web/src/features/dividends/revision-preview-validation";
import type { components } from "../../../packages/contracts/src/schema";
import { test } from "../private-test";
import { database, headers, stage, type Event } from "./helpers";

type Schemas = components["schemas"];

test("cross-store recorded comparison stays private and does not grant payment", async ({ page }) => {
  stage("SETUP"); const password = `synthetic-cross-reconciliation-${randomUUID()}`;
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
  const portfolio = await create("portfolios", { name: "Synthetic cross reconciliation portfolio" });
  const broker = await create("brokers", { name: "Synthetic cross reconciliation broker" });
  const account = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic cross reconciliation account", default_currency: "USD" });
  const otherAccount = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic comparison empty account", default_currency: "USD" });
  const security = await create("securities", { exchange_code: "NASDAQ", display_ticker: "CROSSREC", name: "Synthetic cross reconciliation security", quote_currency: "USD" });
  await create("trades", { portfolio_id: portfolio.id, account_id: account.id, security_id: security.id, side: "BUY", trade_date: "2026-11-02",
    quantity: "10", price: "10", currency: "USD", fee: "0", tax: "0", fx_rate_hkd: "7.8" });
  const created = await page.request.post("/api/v1/dividends/events", { headers: await headers(page), data: {
    security_id: security.id, event_type: "SPECIAL", ex_date: "2026-11-03", dps: "1", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: "Synthetic cross-store event reference", source_url: null, reason: "Synthetic original event for comparison review.",
  } });
  expect(created.status()).toBe(201); const first = await created.json() as Event;
  const revised = await page.request.patch(`/api/v1/dividends/events/${first.id}`, { headers: await headers(page, first.version), data: {
    security_id: security.id, revision_id: first.current_revision_id, resolve_rejection_id: null,
    event_type: "SPECIAL", ex_date: "2026-11-03", announce_date: null, record_date: null, pay_date: null,
    dps: "1.1", currency: "USD", adjustment_basis: "AS_DECLARED", citation: "Synthetic later cross-store event reference",
    source_url: null, reason: "Synthetic successor before recording financial history.", confirm_unlock: false, confirm_reopen: false,
  } });
  expect(revised.status()).toBe(200); const event = await revised.json() as Event; expect(event.version).toBe(2);
  const base = `/api/v1/dividends/events/${event.id}/accounts/${account.id}`;
  const taxRead = await page.request.get(`${base}/tax-settings?effective_on=2026-11-03`); expect(taxRead.status()).toBe(200);
  const tax = await taxRead.json() as Schemas["TaxSettingSelection"];
  const taxSaved = await page.request.post(`${base}/tax-settings`, { headers: await headers(page, event.version), data: {
    revision_id: tax.revision_id, portfolio_id: tax.portfolio_id, broker_id: tax.broker_id, account_version: tax.account_version, broker_version: tax.broker_version,
    expected_head_no: tax.head_no, expected_head_id: tax.head_id, effective_from: "2026-11-01", rate: "0", reason: "Synthetic explicit tax for cross-store comparison.",
  } });
  expect(taxSaved.status()).toBe(201);
  const feeRead = await page.request.get(`${base}/fee-settings?effective_on=2026-11-03`); expect(feeRead.status()).toBe(200);
  const fee = await feeRead.json() as Schemas["FeeSettingSelection"];
  const feeSaved = await page.request.post(`${base}/fee-settings`, { headers: await headers(page, event.version), data: {
    revision_id: fee.revision_id, portfolio_id: fee.portfolio_id, broker_id: fee.broker_id, account_version: fee.account_version, broker_version: fee.broker_version,
    expected_head_no: fee.head_no, expected_head_id: fee.head_id, effective_from: "2026-11-01", fee_status: "ESTIMATED", fee: "0", fee_currency: "USD",
    reason: "Synthetic explicit fee for cross-store comparison.",
  } });
  expect(feeSaved.status()).toBe(201);
  const query = new URLSearchParams({ as_of: "2026-11-03", eligibility_rule: "ORDINARY_PRE_EX_DATE", tax_effective_on: "2026-11-03", fee_effective_on: "2026-11-03" });
  const inputResponse = await page.request.get(`${base}/projection-inputs?${query}`); expect(inputResponse.status()).toBe(200);
  const input = await inputResponse.json() as Schemas["ProjectionInputs"];
  const saved = await page.request.post(`${base}/projections`, { headers: await headers(page, event.version), data: {
    revision_id: input.revision_id, portfolio_id: input.portfolio_id, broker_id: input.broker_id, account_version: input.account_version, broker_version: input.broker_version,
    expected_data_revision: input.portfolio_data_revision, expected_projection_no: input.projection_head_no, expected_projection_id: input.projection_head_id,
    as_of: input.as_of, eligibility_rule: input.eligibility_rule, tax_effective_on: input.tax.effective_on, expected_tax_head_no: input.tax.head_no,
    expected_tax_head_id: input.tax.head_id, expected_tax_selected_id: input.tax.selected_id, fee_effective_on: input.fee.effective_on,
    expected_fee_head_no: input.fee.head_no, expected_fee_head_id: input.fee.head_id, expected_fee_selected_id: input.fee.selected_id,
    fx_rate_hkd: null, fx_source: null, fee_rate_event: null, fee_rate_source: null, confirmation: "RECORDED_PROJECTION_ONLY", confirm_revision_fence: true,
    reason: "Synthetic recorded projection for cross-store comparison.",
  } });
  expect(saved.status()).toBe(201); const projection = await saved.json() as Schemas["ProjectionRecord"];
  expect(projection.state).toBe("EXPECTED"); expect(projection.estimated_amounts?.net_local).toBe("11.000000");
  stage("SEED");
  execFileSync("uv", ["run", "python", "-I", "tests/e2e/dividend-events/seed_cross_reconciliation.py", event.id, account.id, first.current_revision_id, event.current_revision_id], { stdio: "ignore" });
  const before = database(); expect(before.dividend_receipts).toHaveLength(1); expect(before.dividend_direct_receipts).toHaveLength(1);
  expect(before.dividend_direct_receipt_origins).toHaveLength(1); expect(before.dividend_projection_versions).toHaveLength(1);
  expect(() => execFileSync("uv", ["run", "python", "-I", "tests/e2e/dividend-events/seed_cross_reconciliation.py", event.id, account.id, first.current_revision_id, event.current_revision_id], { stdio: "ignore" })).toThrow();
  expect(database()).toEqual(before);
  expect(before.jobs).toEqual([]); expect(before.upstream_attempts).toEqual([]); expect(before.fx_rates).toEqual([]);
  const rejection = await page.request.post(`/api/v1/dividends/${event.id}/verify`, { headers: await headers(page, event.version), data: {
    revision_id: event.current_revision_id, decision: "REJECT_REVISION", citation: "Synthetic fenced rejection attempt", evidence_url: null,
    reason: "Synthetic rejection after recorded financial history.",
  } });
  expect(rejection.status()).toBe(409); expect((await rejection.json() as { code: string }).code).toBe("DIVIDEND_EVENT_PROJECTION_REQUIRED");
  expect(database()).toEqual(before);
  stage("PREVIEW_API");
  const previewUrl = `/api/v1/dividends/events/${event.id}/revision-preview`;
  const previewBody: Schemas["PreviewRequest"] = {
    revision_id: event.current_revision_id, limit: 1, after_account_id: null, snapshot_token: null,
    proposal: { ex_date: "2026-11-03", as_of: "2026-11-03", eligibility_rule: "ORDINARY_PRE_EX_DATE",
      dps: "1.2", currency: "USD", adjustment_basis: "AS_DECLARED",
      accounts: [account.id, otherAccount.id].map(accountId => ({ account_id: accountId,
        tax_rate: "0", fee: "0", fee_currency: "USD", fee_rate_event: null, fee_rate_source: null,
        fx_rate_hkd: null, fx_source: null })) },
  };
  const previewHeaders = await headers(page, event.version);
  const firstPreview = await page.request.post(previewUrl, { headers: previewHeaders, data: previewBody });
  expect(firstPreview.status()).toBe(200); expect(firstPreview.headers()["cache-control"]).toBe("no-store");
  const firstPreviewPage = await firstPreview.json() as Schemas["PreviewPage"];
  const ownedAccounts: Schemas["Account"][] = [];
  for (const accountId of [account.id, otherAccount.id]) {
    const detail = await page.request.get(`/api/v1/accounts/${accountId}`);
    expect(detail.status()).toBe(200); expect(detail.headers()["cache-control"]).toBe("no-store");
    ownedAccounts.push(await detail.json() as Schemas["Account"]);
  }
  expect(checkedRevisionPreview(firstPreviewPage, event, ownedAccounts, previewBody)).toBe(firstPreviewPage);
  expect(firstPreviewPage.items).toHaveLength(1); expect(firstPreviewPage.next_after_account_id).not.toBeNull();
  expect(firstPreviewPage).toMatchObject({ event_id: event.id, revision_id: event.current_revision_id,
    scope: "HYPOTHETICAL_RECORDED_COMPARISON_ONLY", history_completeness: "UNVERIFIED",
    current_use_scope: "REVALIDATION_REQUIRED", eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED" });
  const continued = { ...previewBody, after_account_id: firstPreviewPage.next_after_account_id, snapshot_token: firstPreviewPage.snapshot_token };
  const secondPreview = await page.request.post(previewUrl, { headers: previewHeaders, data: continued });
  expect(secondPreview.status()).toBe(200); expect(secondPreview.headers()["cache-control"]).toBe("no-store");
  const secondPreviewPage = await secondPreview.json() as Schemas["PreviewPage"];
  expect(checkedRevisionPreview(secondPreviewPage, event, ownedAccounts, continued)).toBe(secondPreviewPage);
  expect(() => checkedRevisionPreview({ ...secondPreviewPage, snapshot_token: "b".repeat(64) }, event, ownedAccounts, continued)).toThrow();
  expect(secondPreviewPage.next_after_account_id).toBeNull(); expect(secondPreviewPage.snapshot_token).toBe(firstPreviewPage.snapshot_token);
  const allPreviewItems = [...firstPreviewPage.items, ...secondPreviewPage.items];
  expect(allPreviewItems).toHaveLength(2);
  const heldPreview = allPreviewItems.find(value => value.account_id === account.id)!;
  const emptyPreview = allPreviewItems.find(value => value.account_id === otherAccount.id)!;
  expect(heldPreview).toMatchObject({ previous_expected: "11.000000", expected_delta: "1.000000",
    recorded_received: "8.000000", recorded_outstanding: "4.000000", active_receipts: "2", unverified_origins: "1",
    comparison_quality: "MISSING_FX", state: "RECORDED_PARTIAL" });
  expect(heldPreview.proposed_amounts).toMatchObject({ net_local: "12.000000", estimated_hkd: null, quality: "MISSING_FX" });
  expect(emptyPreview).toMatchObject({ state: "NO_RECORDED_POSITION", proposed_amounts: null, recorded_received: null });
  const stalePreview = await page.request.post(previewUrl, { headers: previewHeaders,
    data: { ...continued, proposal: { ...continued.proposal, dps: "1.3" } } });
  expect(stalePreview.status()).toBe(409); expect(stalePreview.headers()["cache-control"]).toBe("no-store");
  expect((await stalePreview.json() as { code: string }).code).toBe("DIVIDEND_PREVIEW_SNAPSHOT_CHANGED");
  expect(database()).toEqual(before);
  stage("PREVIEW_HISTORY");
  const combinedResponse = await page.request.get(`${base}/recorded-receipts?limit=1`);
  expect(combinedResponse.status()).toBe(200); expect(combinedResponse.headers()["cache-control"]).toBe("no-store");
  const directPage = await combinedResponse.json() as Schemas["Page_CombinedRecordedReceipt_"];
  expect(directPage.items).toHaveLength(1); expect(directPage.next_cursor).not.toBeNull();
  expect(directPage.items[0]).toMatchObject({ event_id: event.id, account_id: account.id, receipt_basis: "DIRECT_EVENT_ACCOUNT",
    entitlement_id: null, revision_id: event.current_revision_id, revision_no: 2, current_revision_no: 2,
    recorded_amount: "2.000000", audit_scope: "RECORDED_AUDIT_MATCHED_ONLY", payment_scope: "OWNER_RECORDED_NOT_VERIFIED" });
  const legacyResponse = await page.request.get(`${base}/recorded-receipts?${new URLSearchParams({ limit: "1", cursor: directPage.next_cursor! })}`);
  expect(legacyResponse.status()).toBe(200); expect(legacyResponse.headers()["cache-control"]).toBe("no-store");
  const legacyPage = await legacyResponse.json() as Schemas["Page_CombinedRecordedReceipt_"];
  expect(legacyPage.items).toHaveLength(1); expect(legacyPage.next_cursor).toBeNull();
  expect(legacyPage.items[0]).toMatchObject({ receipt_basis: "LEGACY_ENTITLEMENT", revision_id: first.current_revision_id,
    revision_no: 1, current_revision_no: 2, recorded_amount: "6.000000", audit_scope: "RECEIPT_AUDIT_NOT_VERIFIED" });
  expect(legacyPage.items[0]?.entitlement_id).not.toBeNull(); expect(database()).toEqual(before);
  const inputPinsResponse = await page.request.get(`${base}/recorded-receipts/inputs`);
  expect(inputPinsResponse.status()).toBe(200); expect(inputPinsResponse.headers()["cache-control"]).toBe("no-store");
  const pins = await inputPinsResponse.json() as Schemas["DirectReceiptInputs"];
  expect(pins).toMatchObject({ event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version,
    workflow_status: event.workflow_status, account_id: account.id, portfolio_id: portfolio.id, broker_id: broker.id, account_status: "ACTIVE",
    account_version: input.account_version, broker_version: input.broker_version, receipt_basis: "DIRECT_EVENT_ACCOUNT",
    rejection_state: "NOT_REJECTED", rejection_id: null, scope: "DIRECT_RECORDED_RECEIPT_INPUTS_ONLY",
    current_use_scope: "REVALIDATION_REQUIRED", history_completeness: "UNVERIFIED", eligibility_scope: "NOT_ESTABLISHED",
    payment_scope: "OWNER_RECORDED_NOT_VERIFIED" });
  expect(pins.portfolio_version).toMatch(/^[1-9][0-9]*$/); expect(pins.portfolio_data_revision).toMatch(/^(0|[1-9][0-9]*)$/);
  expect(pins).not.toHaveProperty("entitlement_id"); expect(pins).not.toHaveProperty("entitlements"); expect(pins).not.toHaveProperty("received_amount");
  expect(database()).toEqual(before);
  const fencedHistory = await page.request.get(`${base}/receipts`);
  expect(fencedHistory.status()).toBe(503); expect(fencedHistory.headers()["cache-control"]).toBe("no-store");
  const legacyVoidInputs = await page.request.get(`${base}/receipts/${legacyPage.items[0]!.id}/void-inputs`);
  expect(legacyVoidInputs.status()).toBe(503); expect(legacyVoidInputs.headers()["cache-control"]).toBe("no-store");
  stage("RESIZE");
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 }); await page.goto("/dividends");
    const article = page.getByRole("article", { name: `股息事件 ${event.id}` });
    if (width >= 1024) await page.locator(`tr[data-record-id="${event.id}"]`).getByRole("button", { name: "查看原值與操作" }).click();
    else await article.getByRole("button", { name: "查看事件與拒絕狀態" }).click();
    await article.getByRole("button", { name: "查看帳戶收款紀錄" }).click();
    const receipts = article.getByRole("region", { name: "已記錄股息收款" });
    const historyResponse = page.waitForResponse(response => new URL(response.url()).pathname === `${base}/recorded-receipts` && response.request().method() === "GET");
    const selectAccount = receipts.getByRole("button", { name: "選擇收款紀錄帳戶 Synthetic cross reconciliation account" });
    await selectAccount.focus(); await page.keyboard.press("Enter");
    const history = await historyResponse; expect(history.status()).toBe(200); expect(history.headers()["cache-control"]).toBe("no-store");
    const recorded = receipts.getByRole("region", { name: "帳戶收款紀錄歷史" });
    await expect(recorded.getByRole("listitem")).toHaveCount(2);
    const directRecord = recorded.getByRole("listitem").filter({ hasText: directPage.items[0]!.id });
    const legacyRecord = recorded.getByRole("listitem").filter({ hasText: legacyPage.items[0]!.id });
    await expect(directRecord).toContainText("直接關聯事件與帳戶"); await expect(directRecord).toContainText("USD 2.000000");
    await expect(directRecord).toContainText("原事件修訂 2"); await expect(directRecord).toContainText("保留稽核記錄相符，付款仍未驗證");
    await expect(directRecord.getByRole("button", { name: /核對作廢收款/ })).toHaveCount(0);
    await expect(legacyRecord).toContainText("原應收識別碼"); await expect(legacyRecord).toContainText("USD 6.000000");
    await expect(legacyRecord).toContainText("原事件修訂 1"); await expect(legacyRecord).toContainText("歷史稽核及付款均未驗證");
    await expect(legacyRecord.getByRole("button", { name: /核對作廢收款/ })).toBeDisabled();
    await expect(recorded).toContainText("本頁包含直接收款"); await expect(recorded.getByRole("alert")).toHaveCount(0);
    const historyBox = (await recorded.boundingBox())!; expect(historyBox.x).toBeGreaterThanOrEqual(0); expect(historyBox.x + historyBox.width).toBeLessThanOrEqual(width);
    const comparisonResponse = page.waitForResponse(response => new URL(response.url()).pathname === `${base}/reconciliation` && response.request().method() === "GET");
    const open = receipts.getByRole("button", { name: "比較已記錄金額" }); await open.focus(); await page.keyboard.press("Enter");
    const response = await comparisonResponse; expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    expect(await response.json() as Schemas["RecordedReconciliation"]).toMatchObject({ event_id: event.id, revision_id: event.current_revision_id,
      account_id: account.id, state: "RECORDED_PARTIAL", recorded_expected: "11.000000", recorded_received: "8.000000", recorded_outstanding: "3.000000",
      active_receipts: "2", void_receipts: "0", unverified_origins: "1", unavailable_cash_links: "0", eligibility_scope: "NOT_ESTABLISHED",
      payment_scope: "OWNER_RECORDED_NOT_VERIFIED" });
    const panel = receipts.getByRole("region", { name: "已記錄股息金額比較" });
    for (const amount of ["11.000000", "8.000000", "3.000000"]) await expect(panel).toContainText(`USD ${amount}`);
    await expect(panel).toContainText("不代表付款、資格或收入已確認"); await expect(panel).toContainText("收款來源稽核未核對 1 筆");
    const box = (await panel.boundingBox())!; expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
    stage("PREVIEW_UI_OPEN");
    const accountsResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/accounts" && response.request().method() === "GET");
    const previewOpen = article.getByRole("button", { name: "預覽擬議修訂" }); await previewOpen.focus(); await page.keyboard.press("Enter");
    const accountsRead = await accountsResponse; expect(accountsRead.status()).toBe(200); expect(accountsRead.headers()["cache-control"]).toBe("no-store");
    const preview = page.getByRole("region", { name: "擬議股息修訂預覽" });
    await expect(preview.getByRole("heading", { name: "擬議股息修訂預覽" })).toBeFocused();
    stage("PREVIEW_UI_INPUT");
    await expect(preview).toContainText("不會接受修訂或改寫收款");
    await preview.getByLabel("查閱日").fill("2026-11-03"); await preview.getByLabel("持股規則").selectOption("ORDINARY_PRE_EX_DATE");
    await preview.getByLabel("擬議每股股息").fill("1.2"); await preview.getByLabel("擬議每股股息").focus(); await page.keyboard.press("Tab");
    await expect(preview.getByLabel("擬議幣別")).toBeFocused();
    for (const name of ["Synthetic cross reconciliation account", "Synthetic comparison empty account"]) {
      const group = preview.getByRole("group", { name: `估算 ${name}` });
      await group.getByLabel("稅率估算").selectOption("PROVIDED"); await group.getByLabel("稅率小數").fill("0");
      await group.getByLabel("費用估算").selectOption("PROVIDED"); await group.getByLabel("估算費用").fill("0");
      await group.getByLabel("費用幣別").selectOption("USD"); await group.getByLabel("港幣估值匯率").selectOption("UNKNOWN");
    }
    if (width === 360) {
      stage("PREVIEW_UI_RESIZE");
      for (const resizedWidth of [768, 1440, 360]) {
        await page.setViewportSize({ width: resizedWidth, height: 900 });
        await expect(preview.getByLabel("擬議每股股息")).toHaveValue("1.2");
        await expect(preview.getByRole("group", { name: "估算 Synthetic cross reconciliation account" }).getByLabel("稅率小數")).toHaveValue("0");
      }
    }
    stage("PREVIEW_UI_SUBMIT");
    const previewResponse = page.waitForResponse(response => new URL(response.url()).pathname === previewUrl && response.request().method() === "POST");
    await preview.getByRole("button", { name: "計算預覽" }).click(); const calculated = await previewResponse;
    expect(calculated.status()).toBe(200); expect(calculated.headers()["cache-control"]).toBe("no-store");
    expect(calculated.request().headers()["if-match"]).toBe(`"${event.version}"`);
    stage("PREVIEW_UI_VALUES");
    const compared = preview.getByRole("region", { name: "擬議修訂比較結果" });
    const held = compared.getByRole("listitem").filter({ hasText: "Synthetic cross reconciliation account" });
    await expect(held).toContainText("毛額 12.000000"); await expect(held).toContainText("淨額 12.000000");
    await expect(held).toContainText("擬議減舊投影 1.000000"); await expect(held).toContainText("已記錄收款 8.000000");
    await expect(held).toContainText("已記錄差額 4.000000"); await expect(held).toContainText("港幣估值 未知");
    await expect(held).toContainText("比較品質：缺少港幣匯率");
    await expect(compared.getByRole("listitem").filter({ hasText: "Synthetic comparison empty account" })).toContainText("沒有已記錄持倉，不代表已確認零持倉");
    await expect(compared).toContainText("不代表付款、持倉資格或收入已確認");
    await expect(compared.getByRole("button", { name: "下一頁預覽" })).toBeDisabled();
    const previewBox = (await preview.boundingBox())!; expect(previewBox.x).toBeGreaterThanOrEqual(0); expect(previewBox.x + previewBox.width).toBeLessThanOrEqual(width);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  stage("PREVIEW_UI_UNCHANGED"); expect(database()).toEqual(before);
  stage("PREVIEW_UI_ABORT");
  let releasePreview!: () => void; let previewReached!: () => void;
  const heldRequest = new Promise<void>(resolve => { releasePreview = resolve; });
  const reachedPreview = new Promise<void>(resolve => { previewReached = resolve; });
  const previewRoute = `**${previewUrl}`;
  await page.route(previewRoute, async route => {
    const response = await route.fetch(); expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
    previewReached(); await heldRequest; await route.abort("failed").catch(() => {});
  });
  let openPreview = page.getByRole("region", { name: "擬議股息修訂預覽" });
  await openPreview.getByRole("button", { name: "計算預覽" }).click(); await reachedPreview;
  await openPreview.getByRole("button", { name: "中止並關閉預覽" }).click(); await expect(openPreview).toHaveCount(0);
  releasePreview(); await page.unroute(previewRoute); expect(database()).toEqual(before);
  await page.getByRole("button", { name: "預覽擬議修訂" }).click();
  openPreview = page.getByRole("region", { name: "擬議股息修訂預覽" });
  await openPreview.getByLabel("查閱日").fill("2026-11-03"); await openPreview.getByLabel("持股規則").selectOption("ORDINARY_PRE_EX_DATE");
  await openPreview.getByLabel("擬議每股股息").fill("1.2");
  for (const name of ["Synthetic cross reconciliation account", "Synthetic comparison empty account"]) {
    const group = openPreview.getByRole("group", { name: `估算 ${name}` });
    await group.getByLabel("稅率估算").selectOption("PROVIDED"); await group.getByLabel("稅率小數").fill("0");
    await group.getByLabel("費用估算").selectOption("PROVIDED"); await group.getByLabel("估算費用").fill("0");
    await group.getByLabel("費用幣別").selectOption("USD"); await group.getByLabel("港幣估值匯率").selectOption("UNKNOWN");
  }
  stage("PREVIEW_UI_ACCOUNT");
  const changedAccount = await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name: "Synthetic account added during preview", default_currency: "USD" });
  expect(changedAccount.id).not.toBe(account.id); const afterChange = database(); expect(afterChange).not.toEqual(before);
  stage("PREVIEW_UI_STALE");
  const preview = page.getByRole("region", { name: "擬議股息修訂預覽" });
  const staleRead = page.waitForResponse(response => new URL(response.url()).pathname === previewUrl && response.request().method() === "POST");
  await preview.getByRole("button", { name: "計算預覽" }).click(); const stale = await staleRead;
  expect(stale.status()).toBe(409); expect(stale.headers()["cache-control"]).toBe("no-store");
  expect((await stale.json() as { code: string }).code).toBe("DIVIDEND_PREVIEW_ACCOUNT_SCOPE_CHANGED");
  await expect(preview.getByRole("alert")).toContainText("帳戶範圍已變更");
  await expect(preview.getByLabel("擬議每股股息")).toHaveValue("1.2");
  await expect(preview.getByRole("region", { name: "擬議修訂比較結果" })).toHaveCount(0); expect(database()).toEqual(afterChange);
  stage("PREVIEW_UI_REREAD");
  await preview.getByRole("button", { name: "重新讀取全部帳戶" }).click();
  await expect(preview.getByRole("alert")).toContainText("帳戶清單已變更");
  await expect(preview.getByRole("button", { name: "計算預覽" })).toBeDisabled();
  await expect(preview.getByLabel("擬議每股股息")).toHaveValue("1.2"); expect(database()).toEqual(afterChange);

  stage("PREVIEW_UI_PAGINATION_SETUP");
  const extraNames = Array.from({ length: 23 }, (_, index) => `Synthetic pagination account ${String(index + 1).padStart(2, "0")}`);
  for (const name of extraNames) await create("accounts", { portfolio_id: portfolio.id, broker_id: broker.id, name, default_currency: "HKD" });
  const beforePagination = database();
  await preview.getByRole("button", { name: "取消" }).click();
  await page.getByRole("button", { name: "預覽擬議修訂" }).click();
  const pagedPreview = page.getByRole("region", { name: "擬議股息修訂預覽" });
  await pagedPreview.getByLabel("查閱日").fill("2026-11-03");
  await pagedPreview.getByLabel("持股規則").selectOption("ORDINARY_PRE_EX_DATE");
  await pagedPreview.getByLabel("擬議幣別").selectOption("HKD");
  for (const name of ["Synthetic cross reconciliation account", "Synthetic comparison empty account", "Synthetic account added during preview", ...extraNames]) {
    const group = pagedPreview.getByRole("group", { name: `估算 ${name}` });
    await group.getByLabel("稅率估算").selectOption("UNKNOWN"); await group.getByLabel("費用估算").selectOption("UNKNOWN");
    await group.getByLabel("費用幣別").selectOption("UNKNOWN");
  }
  stage("PREVIEW_UI_PAGINATION");
  const nextResponse = () => page.waitForResponse(response => new URL(response.url()).pathname === previewUrl && response.request().method() === "POST");
  let responsePromise = nextResponse(); await pagedPreview.getByRole("button", { name: "計算預覽" }).click();
  const pageOneResponse = await responsePromise; expect(pageOneResponse.status()).toBe(200); expect(pageOneResponse.headers()["cache-control"]).toBe("no-store");
  const pageOne = await pageOneResponse.json() as Schemas["PreviewPage"];
  expect(pageOne.items).toHaveLength(25); expect(pageOne.next_after_account_id).not.toBeNull();
  const pageOneView = pagedPreview.getByRole("region", { name: "擬議修訂比較結果" }); await expect(pageOneView).toContainText("第 1 頁");
  responsePromise = nextResponse(); await pageOneView.getByRole("button", { name: "下一頁預覽" }).click();
  const pageTwoResponse = await responsePromise; expect(pageTwoResponse.status()).toBe(200); expect(pageTwoResponse.headers()["cache-control"]).toBe("no-store");
  const continuedBody = pageTwoResponse.request().postDataJSON() as Schemas["PreviewRequest"];
  expect(continuedBody.after_account_id).toBe(pageOne.next_after_account_id); expect(continuedBody.snapshot_token).toBe(pageOne.snapshot_token);
  const pageTwo = await pageTwoResponse.json() as Schemas["PreviewPage"];
  expect(pageTwo.items).toHaveLength(1); expect(pageTwo.snapshot_token).toBe(pageOne.snapshot_token);
  await expect(pageOneView).toContainText("第 2 頁");
  responsePromise = nextResponse(); await pageOneView.getByRole("button", { name: "上一頁預覽" }).click();
  const previousResponse = await responsePromise; expect(previousResponse.status()).toBe(200);
  const previousBody = previousResponse.request().postDataJSON() as Schemas["PreviewRequest"];
  expect(previousBody.after_account_id).toBeNull(); expect(previousBody.snapshot_token).toBeNull();
  expect((await previousResponse.json() as Schemas["PreviewPage"]).snapshot_token).toBe(pageOne.snapshot_token);
  await expect(pageOneView).toContainText("第 1 頁"); expect(database()).toEqual(beforePagination);

  stage("PREVIEW_UI_PAGE_STALE");
  const accountDetail = await page.request.get(`/api/v1/accounts/${account.id}`); expect(accountDetail.status()).toBe(200);
  const accountVersion = (await accountDetail.json() as Schemas["Account"]).version;
  const changed = await page.request.patch(`/api/v1/accounts/${account.id}`, { headers: await headers(page, accountVersion), data: { name: "Synthetic renamed account after preview" } });
  expect(changed.status()).toBe(200); const beforeStalePage = database();
  responsePromise = nextResponse(); await pageOneView.getByRole("button", { name: "下一頁預覽" }).click();
  const stalePage = await responsePromise; expect(stalePage.status()).toBe(409); expect(stalePage.headers()["cache-control"]).toBe("no-store");
  expect((await stalePage.json() as { code: string }).code).toBe("DIVIDEND_PREVIEW_SNAPSHOT_CHANGED");
  await expect(pagedPreview.getByRole("alert")).toContainText("比較資料已變更");
  await expect(pagedPreview.getByRole("button", { name: "計算預覽" })).toBeDisabled();
  await expect(pagedPreview.getByLabel("查閱日")).toHaveValue("2026-11-03");
  await expect(pagedPreview.getByRole("region", { name: "擬議修訂比較結果" })).toHaveCount(0); expect(database()).toEqual(beforeStalePage);
  await pagedPreview.getByRole("button", { name: "重新讀取全部帳戶" }).click();
  await expect(pagedPreview.getByRole("alert")).toContainText("帳戶清單已變更");
  await expect(pagedPreview.getByRole("button", { name: "計算預覽" })).toBeDisabled(); expect(database()).toEqual(beforeStalePage);
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0); expect(await page.evaluate(() => caches.keys())).toEqual([]);
  stage("FINAL");
});
