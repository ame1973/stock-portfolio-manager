import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { api, type components } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { CandidateCreateForm, CandidateRevisionForm } from "./CandidateForm";

type Schemas = components["schemas"];
type Candidate = Schemas["CandidateRecord"];
const security = { id: "11111111-1111-4111-8111-111111111111", name: "Synthetic candidate security", display_ticker: "SYN", exchange_code: "NASDAQ", quote_currency: "USD" };
const match = { observation_id: "22222222-2222-4222-8222-222222222222", source_sha256: "a".repeat(64), basis_assertion_id: null, reason: "Synthetic source association reason" };
const observation: Schemas["DividendObservationRecord"] = {
  id: match.observation_id, security_id: security.id, normalized_sha256: match.source_sha256, provider_id: "manual", origin_group: "USER_VERIFIED",
  provider_run_id: "77777777-7777-4777-8777-777777777777", provider_symbol: "SYN.CITED", provider_event_id: null, source_url: null, evidence: null,
  retrieved_at: "2026-11-01T00:00:00Z", last_seen_at: "2026-11-01T00:00:00Z", sightings_count: 1, provenance_version: 1,
  scope: "SOURCE_ONLY", canonical_adoption: "NOT_ADOPTED", canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE",
  ex_date: "2026-11-01", announce_date: null, record_date: null, pay_date: null, event_type: "SPECIAL", dps: "0", currency: "USD",
  adjustment_basis: "SPLIT_ADJUSTED", source_currency: null, source_period: null, source_frequency: null, incomplete_reasons: ["UNRESOLVED_ADJUSTMENT"],
};
const sourcePage = { items: [observation], next_cursor: null, scope: "IMMUTABLE_SOURCE_V1", legacy_records_omitted: 0 };
const basis: Schemas["DividendBasisRecord"] = { id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", security_id: security.id,
  observation_id: observation.id, source_sha256: observation.normalized_sha256, ex_date: "2026-11-01", share_basis_date: "2026-11-01",
  source_basis: "AFTER_EFFECTIVE_DATE_ACTIONS", target_basis: "PRE_EX_DATE_EVENTS", complete: true, split_only: true, actions: [],
  citation: { id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", citation: "Synthetic split-only reference", reason: "Synthetic explicit split-only scope", url: null, url_withheld: false },
  created_at: "2026-11-05T00:00:00Z", scope: "BASIS_DECLARATION_ONLY", canonical_adoption: "NOT_ADOPTED",
  canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE", current_use_scope: "REVALIDATION_REQUIRED_FOR_NEW_ADOPTION", market_history: "OWNER_ASSERTED_NOT_VERIFIED" };
const created: Candidate = {
  id: "33333333-3333-4333-8333-333333333333", revision_id: "44444444-4444-4444-8444-444444444444", version: 1, state: "OPEN",
  terms: { security_id: security.id, ex_date: null, announce_date: null, record_date: null, pay_date: null, event_type: "UNKNOWN", dps: null, currency: null, adjustment_basis: "UNKNOWN" },
  matches: [], change_reason: "Synthetic explicit candidate creation", created_at: "2026-11-05T00:00:00Z",
  assessment: { status: "INCOMPLETE", independent_origins: [], eligible_observation_ids: [], conflicting_fields: [], incomplete_reasons: ["NO_MATCHED_EVIDENCE"], checks: [] },
  scope: "CANDIDATE_REVIEW_ONLY", canonical_adoption: "NOT_ADOPTED", canonical_adoption_scope: "RESPONSE_ONLY_NOT_LINKAGE", current_use_scope: "REVALIDATION_REQUIRED",
};
const reply = <T,>(data: T) => ({ data, response: new Response() });
function reads() {
  return vi.spyOn(api, "GET").mockImplementation(async path => {
    if (path === "/api/v1/auth/csrf") return reply({ csrf_token: "synthetic-csrf", expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/api/v1/securities") return reply({ items: [security], next_cursor: null });
    if (path === "/api/v1/securities/{id}") return reply(security);
    if (path === "/api/v1/dividends/observations") return reply(sourcePage);
    if (path === "/api/v1/dividends/basis-assertions") return reply({ items: [basis], next_cursor: null });
    throw new Error(`Unexpected read: ${path}`);
  });
}
function renderForm(ui: React.ReactElement) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);
}
async function chooseSecurity() {
  await screen.findByRole("option", { name: "SYN / NASDAQ — Synthetic candidate security" });
  fireEvent.change(screen.getByLabelText("候選證券", { exact: true }), { target: { value: security.id } });
}
function acknowledge() { fireEvent.click(screen.getByRole("checkbox", { name: /我已核對本次候選條款與來源關聯/ })); }
function submit() { fireEvent.click(screen.getByRole("button", { name: /儲存候選/ })); }
function body(spy: { mock: { calls: readonly (readonly unknown[])[] } }, index = 0) {
  return spy.mock.calls[index]?.[1] as { body: Schemas["CandidateWrite"]; params: { header: Record<string, string>; path?: { id: string } } };
}

describe("explicit candidate forms", () => {
  it("requires acknowledgement of an empty source list and preserves unknowns", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValue(reply(created)); const saved = vi.fn();
    renderForm(<CandidateCreateForm onSaved={saved} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } }); acknowledge(); submit();
    expect(await screen.findByText("請確認本次明確不列來源關聯。")).toBeVisible(); expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); submit();
    await waitFor(() => expect(saved).toHaveBeenCalledWith(created));
    expect(post).toHaveBeenCalledWith("/api/v1/dividends/candidates", expect.anything());
    expect(body(post).body).toMatchObject({ state: "OPEN", terms: { security_id: security.id, ex_date: null, dps: null, currency: null, adjustment_basis: "UNKNOWN" }, matches: [], change_reason: created.change_reason });
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("adds an explicitly selected source with exact pins without filling terms or reason", async () => {
    const get = reads(); const response = { ...created, matches: [match] }; const saved = vi.fn();
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(response));
    renderForm(<CandidateCreateForm onSaved={saved} onCancel={vi.fn()} />);
    expect(screen.getByRole("button", { name: "瀏覽來源觀測" })).toBeDisabled();
    expect(get).not.toHaveBeenCalledWith("/api/v1/dividends/observations", expect.anything());
    await chooseSecurity(); fireEvent.click(screen.getByRole("button", { name: "瀏覽來源觀測" }));
    const card = await screen.findByRole("article", { name: `可選來源 ${observation.id}` });
    expect(card).toHaveTextContent(observation.normalized_sha256); expect(card).toHaveTextContent("每股 USD 0");
    expect(card).toHaveTextContent("不是事件關聯查詢");
    fireEvent.click(within(card).getByRole("button", { name: "加入此來源" }));
    expect(screen.getByLabelText("來源 1 觀測識別碼")).toHaveValue(observation.id);
    expect(screen.getByLabelText("來源 1 原始雜湊")).toHaveValue(observation.normalized_sha256);
    expect(screen.getByLabelText("來源 1 基準聲明識別碼（選填）")).toHaveValue("");
    expect(screen.getByLabelText("來源 1 關聯依據")).toHaveValue("");
    expect(screen.getByLabelText("候選除淨日（選填）")).toHaveValue("");
    expect(screen.getByLabelText("候選每股金額（選填）")).toHaveValue("");
    expect(screen.getByLabelText("候選股息幣別")).toHaveValue("");
    expect(within(card).getByRole("button", { name: "已列入來源" })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("來源 1 關聯依據"), { target: { value: match.reason } });
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } }); acknowledge(); submit();
    await waitFor(() => expect(saved).toHaveBeenCalledWith(response));
    expect(body(post).body.matches).toEqual([match]); expect(body(post).body.terms.dps).toBeNull(); expect(body(post).body.terms.currency).toBeNull();
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("keeps manually entered source pins when changing the selected security", async () => {
    reads(); renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.click(screen.getByRole("button", { name: "瀏覽來源觀測" }));
    const card = await screen.findByRole("article", { name: `可選來源 ${observation.id}` });
    fireEvent.click(within(card).getByRole("button", { name: "加入此來源" }));
    fireEvent.change(screen.getByLabelText("來源 1 關聯依據"), { target: { value: match.reason } });
    fireEvent.change(screen.getByLabelText("候選證券", { exact: true }), { target: { value: "" } });
    expect(screen.getByLabelText("來源 1 觀測識別碼")).toHaveValue(observation.id);
    expect(screen.getByLabelText("來源 1 關聯依據")).toHaveValue(match.reason);
    expect(screen.queryByRole("article", { name: `可選來源 ${observation.id}` })).not.toBeInTheDocument();
  });

  it("adds a separately selected basis pin without changing source, terms or association reason", async () => {
    const get = reads(); const saved = vi.fn(); const association = { ...match, basis_assertion_id: basis.id };
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ...created, matches: [association] }));
    renderForm(<CandidateCreateForm onSaved={saved} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.click(screen.getByRole("button", { name: "新增來源關聯" }));
    const browse = screen.getByRole("button", { name: "瀏覽來源 1 基準聲明" }); expect(browse).toBeDisabled();
    fireEvent.change(screen.getByLabelText("來源 1 觀測識別碼"), { target: { value: match.observation_id } });
    fireEvent.change(screen.getByLabelText("來源 1 原始雜湊"), { target: { value: match.source_sha256 } });
    expect(browse).toBeEnabled(); fireEvent.click(browse);
    const card = await screen.findByRole("article", { name: `可選基準 ${basis.id}` });
    fireEvent.click(within(card).getByRole("button", { name: "使用此基準聲明" }));
    expect(screen.getByLabelText("來源 1 基準聲明識別碼（選填）")).toHaveValue(basis.id);
    expect(screen.getByLabelText("來源 1 觀測識別碼")).toHaveValue(match.observation_id);
    expect(screen.getByLabelText("來源 1 原始雜湊")).toHaveValue(match.source_sha256);
    expect(screen.getByLabelText("來源 1 關聯依據")).toHaveValue(""); expect(screen.getByLabelText("候選每股金額（選填）")).toHaveValue("");
    expect(get).toHaveBeenCalledWith("/api/v1/dividends/basis-assertions", expect.objectContaining({ params: { query: expect.objectContaining({
      observation_id: observation.id, security_id: security.id,
    }) } }));
    fireEvent.change(screen.getByLabelText("來源 1 關聯依據"), { target: { value: match.reason } });
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } }); acknowledge(); submit();
    await waitFor(() => expect(saved).toHaveBeenCalledOnce()); expect(body(post).body.matches).toEqual([association]);
  });

  it("retains an entered basis ID when source scope changes", async () => {
    reads(); renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.click(screen.getByRole("button", { name: "新增來源關聯" }));
    fireEvent.change(screen.getByLabelText("來源 1 觀測識別碼"), { target: { value: match.observation_id } });
    fireEvent.change(screen.getByLabelText("來源 1 原始雜湊"), { target: { value: match.source_sha256 } });
    fireEvent.click(screen.getByRole("button", { name: "瀏覽來源 1 基準聲明" }));
    const card = await screen.findByRole("article", { name: `可選基準 ${basis.id}` }); fireEvent.click(within(card).getByRole("button", { name: "使用此基準聲明" }));
    fireEvent.change(screen.getByLabelText("來源 1 原始雜湊"), { target: { value: "b".repeat(63) } });
    expect(screen.getByLabelText("來源 1 基準聲明識別碼（選填）")).toHaveValue(basis.id);
    expect(screen.queryByRole("article", { name: `可選基準 ${basis.id}` })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "瀏覽來源 1 基準聲明" })).toBeDisabled();
  });

  it("sends explicit exact source pins and zero without inheriting quote currency", async () => {
    reads(); const response: Candidate = { ...created, terms: { ...created.terms, dps: "0.00000000" }, matches: [match] };
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply(response)); const saved = vi.fn();
    renderForm(<CandidateCreateForm onSaved={saved} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選每股金額（選填）"), { target: { value: "000.00000000" } });
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    fireEvent.click(screen.getByRole("button", { name: "新增來源關聯" }));
    fireEvent.change(screen.getByLabelText("來源 1 觀測識別碼"), { target: { value: match.observation_id.toUpperCase() } });
    fireEvent.change(screen.getByLabelText("來源 1 原始雜湊"), { target: { value: match.source_sha256 } });
    fireEvent.change(screen.getByLabelText("來源 1 關聯依據"), { target: { value: `\u0085${match.reason}\u0085` } });
    acknowledge(); submit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(response));
    expect(body(post).body.terms).toMatchObject({ dps: "000.00000000", currency: null });
    expect(body(post).body.matches).toEqual([match]);
    expect(screen.getByRole("link", { name: /另開分頁核對來源觀測/ })).toHaveAttribute("target", "_blank");
  });

  it("freezes the observed version, permits explicit dismissal and preserves source pins", async () => {
    reads(); const target: Candidate = { ...created, version: 2, revision_id: "55555555-5555-4555-8555-555555555555", matches: [match], terms: { ...created.terms, dps: "0.00000000" } };
    const response: Candidate = { ...target, version: 3, revision_id: "66666666-6666-4666-8666-666666666666", state: "DISMISSED", change_reason: "Synthetic explicit dismissal reason" };
    const patch = vi.spyOn(api, "PATCH").mockResolvedValue(reply(response)); const saved = vi.fn();
    renderForm(<CandidateRevisionForm target={target} onSaved={saved} onCancel={vi.fn()} />);
    expect(screen.getByText(new RegExp(target.revision_id))).toBeVisible();
    fireEvent.change(screen.getByLabelText("候選狀態"), { target: { value: "DISMISSED" } });
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: response.change_reason } }); acknowledge();
    target.version = 9; target.revision_id = "77777777-7777-4777-8777-777777777777";
    submit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(response));
    expect(body(patch).params.header["If-Match"]).toBe('"2"');
    expect(body(patch).body).toMatchObject({ state: "DISMISSED", matches: [match] });
    expect(body(patch).params.path?.id).toBe(created.id);
  });

  it("keeps a same-key uncertain intent and changes the key only when submitted input changes", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("Synthetic network failure"));
    renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); acknowledge();
    submit(); expect(await screen.findByText(/網絡連線中斷/)).toBeVisible();
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(body(post, 0).params.header["Idempotency-Key"]).toBe(body(post, 1).params.header["Idempotency-Key"]);
    expect(body(post, 0).body).toEqual(body(post, 1).body);
    fireEvent.change(screen.getByLabelText("候選每股金額（選填）"), { target: { value: "0" } });
    submit(); await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(body(post, 2).params.header["Idempotency-Key"]).not.toBe(body(post, 1).params.header["Idempotency-Key"]);
    expect(screen.getByLabelText("候選每股金額（選填）")).toHaveValue("0");
  });

  it.each([
    { ...created, version: 2 }, { ...created, revision_id: "not-a-uuid" },
    { ...created, terms: { ...created.terms, currency: "USD" } },
    { ...created, terms: { ...created.terms, dps: "0" } },
    { ...created, canonical_adoption_scope: "LOOKUP" }, { ...created, matches: [match] },
    { ...created, change_reason: "Substituted response reason" },
  ])("does not treat a substituted response as saved", async malformed => {
    reads(); vi.spyOn(api, "POST").mockResolvedValue(reply(malformed)); const saved = vi.fn();
    renderForm(<CandidateCreateForm onSaved={saved} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); acknowledge(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("伺服器回覆與本次候選決定不符");
    expect(screen.getByLabelText("候選變更依據")).toHaveValue(created.change_reason); expect(saved).not.toHaveBeenCalled();
  });

  it("keeps an uncertain key when a decimal is reformatted without changing its exact value", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockRejectedValue(new TypeError("Synthetic network failure"));
    renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    fireEvent.change(screen.getByLabelText("候選每股金額（選填）"), { target: { value: "000.0100" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); acknowledge(); submit();
    expect(await screen.findByText(/網絡連線中斷/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("候選每股金額（選填）"), { target: { value: "0.01" } }); submit();
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(body(post, 0).params.header["Idempotency-Key"]).toBe(body(post, 1).params.header["Idempotency-Key"]);
  });

  it("rejects financial strings longer than the API limit without writing", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    fireEvent.change(screen.getByLabelText("候選每股金額（選填）"), { target: { value: "0".repeat(51) } });
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); acknowledge(); submit();
    expect(await screen.findByText("每股金額最多 50 個字元。")).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("retains the same key after an inconsistent response until the user changes intent", async () => {
    reads(); const post = vi.spyOn(api, "POST").mockResolvedValueOnce(reply({ ...created, version: 2 })).mockResolvedValueOnce(reply(created)); const saved = vi.fn();
    renderForm(<CandidateCreateForm onSaved={saved} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); acknowledge(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("不會假定未儲存");
    submit(); await waitFor(() => expect(saved).toHaveBeenCalledWith(created));
    expect(body(post, 0).params.header["Idempotency-Key"]).toBe(body(post, 1).params.header["Idempotency-Key"]);
    expect(body(post, 0).body).toEqual(body(post, 1).body);
  });

  it.each([
    ["CANDIDATE_SOURCE_UNAVAILABLE", "來源觀測無法安全核對"],
    ["CANDIDATE_SOURCE_CHANGED", "候選來源與證券或雜湊不相符"],
    ["CANDIDATE_BASIS_UNAVAILABLE", "基準聲明無法安全核對"],
    ["CANDIDATE_BASIS_CHANGED", "候選基準與來源不相符或已變更"],
    ["INVALID_CANDIDATE_DECISION", "候選條款或來源關聯不符"],
    ["CANDIDATE_VERSION_CONFLICT", "候選版本或固定證券已變更"],
  ])("reports safe, actionable candidate failure %s without exposing server content", async (code, message) => {
    reads(); vi.spyOn(api, "POST").mockResolvedValue({ error: { code, message: "synthetic private error", request_id: "synthetic", details: {} }, response: new Response(null, { status: 409 }) });
    renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); acknowledge(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByText(/synthetic private error/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("候選變更依據")).toHaveValue(created.change_reason);
  });

  it("preserves revision inputs on a stale If-Match response", async () => {
    reads(); const patch = vi.spyOn(api, "PATCH").mockResolvedValue({ error: { code: "PRECONDITION_FAILED", message: "private", request_id: "synthetic", details: {} }, response: new Response(null, { status: 412 }) });
    const saved = vi.fn(); renderForm(<CandidateRevisionForm target={created} onSaved={saved} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: "Synthetic stale revision rationale" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); acknowledge(); submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("不會覆蓋新版資料");
    expect(body(patch).params.header["If-Match"]).toBe('"1"');
    expect(screen.getByLabelText("候選變更依據")).toHaveValue("Synthetic stale revision rationale"); expect(saved).not.toHaveBeenCalled();
  });

  it("fails closed for an invalid or exhausted observed candidate", () => {
    reads(); const patch = vi.spyOn(api, "PATCH");
    renderForm(<CandidateRevisionForm target={{ ...created, version: 2_147_483_647 }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("版本已達上限");
    expect(screen.queryByRole("form", { name: "修訂股息候選" })).not.toBeInTheDocument(); expect(patch).not.toHaveBeenCalled();
  });

  it("requires empty-source confirmation again after removing the last association", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); acknowledge();
    fireEvent.click(screen.getByRole("button", { name: "新增來源關聯" }));
    fireEvent.click(screen.getByRole("button", { name: "移除來源 1 關聯" }));
    expect(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })).not.toBeChecked();
    submit(); expect(await screen.findByText("請確認本次明確不列來源關聯。")).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("rejects an unpaired Unicode surrogate without writing", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: `Synthetic rationale with \ud800` } });
    fireEvent.click(screen.getByRole("checkbox", { name: /本次明確不列來源關聯/ })); acknowledge(); submit();
    expect(await screen.findByText(/依據含有無效的 Unicode 字元/)).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("surfaces the UTF-8 association storage bound before any write", async () => {
    reads(); const patch = vi.spyOn(api, "PATCH");
    const matches = Array.from({ length: 33 }, () => ({ ...match, observation_id: crypto.randomUUID(), reason: "🧪".repeat(1000) }));
    renderForm(<CandidateRevisionForm target={{ ...created, matches }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: "Synthetic source-bound revision reason" } }); acknowledge(); submit();
    expect(await screen.findByText(/來源關聯內容超過 128 KiB/)).toBeVisible(); expect(patch).not.toHaveBeenCalled();
  });

  it("rejects one basis declaration reused across different observations", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    for (let index = 1; index <= 2; index++) {
      fireEvent.click(screen.getByRole("button", { name: "新增來源關聯" }));
      fireEvent.change(screen.getByLabelText(`來源 ${index} 觀測識別碼`), { target: { value: index === 1 ? match.observation_id : "55555555-5555-4555-8555-555555555555" } });
      fireEvent.change(screen.getByLabelText(`來源 ${index} 原始雜湊`), { target: { value: match.source_sha256 } });
      fireEvent.change(screen.getByLabelText(`來源 ${index} 基準聲明識別碼（選填）`), { target: { value: index === 1 ? "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" : "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA" } });
      fireEvent.change(screen.getByLabelText(`來源 ${index} 關聯依據`), { target: { value: match.reason } });
    }
    acknowledge(); submit(); expect(await screen.findByText("同一基準聲明不可重複關聯。" )).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });

  it("rejects duplicate or malformed source pins without contacting the writer", async () => {
    reads(); const post = vi.spyOn(api, "POST"); renderForm(<CandidateCreateForm onSaved={vi.fn()} onCancel={vi.fn()} />); await chooseSecurity();
    fireEvent.change(screen.getByLabelText("候選變更依據"), { target: { value: created.change_reason } });
    for (let index = 1; index <= 2; index++) {
      fireEvent.click(screen.getByRole("button", { name: "新增來源關聯" }));
      fireEvent.change(screen.getByLabelText(`來源 ${index} 觀測識別碼`), { target: { value: match.observation_id } });
      fireEvent.change(screen.getByLabelText(`來源 ${index} 原始雜湊`), { target: { value: match.source_sha256 } });
      fireEvent.change(screen.getByLabelText(`來源 ${index} 關聯依據`), { target: { value: match.reason } });
    }
    acknowledge(); submit(); expect(await screen.findByText("同一觀測不可重複關聯。" )).toBeVisible(); expect(post).not.toHaveBeenCalled();
  });
});
