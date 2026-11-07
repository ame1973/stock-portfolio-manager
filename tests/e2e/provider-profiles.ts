import { expect, type Page } from "@playwright/test";
import type { components } from "../../packages/contracts/src/schema";

type Schemas = components["schemas"];

export async function providerProfileApi(
  page: Page, headers: (version?: number) => Record<string, string>, portfolioId: string, accountId: string,
) {
  async function read<T>(path: string): Promise<T> {
    const response = await page.request.get(`/api/v1/${path}`);
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
    return await response.json() as T;
  }
  const snapshot = async () => ({
    portfolio: await read<Schemas["Portfolio"]>(`portfolios/${portfolioId}`),
    cash: await read<Schemas["AccountCashBalances"]>(`accounts/${accountId}/cash-balances`),
    trades: await read<{ items: Schemas["TradeRecord"][] }>(`trades?portfolio_id=${portfolioId}&include_history=true`),
    jobs: await read<Schemas["Page_SourceJob_"]>("jobs"),
  });
  const before = await snapshot();
  expect(await read<Schemas["Page_ProviderProfileRecord_"]>("provider-profiles")).toEqual({ items: [], next_cursor: null });
  const input: Schemas["ProviderProfileCreate"] = {
    policy: { provider_id: "yahoo", enabled: false, terms_acknowledged: false, terms_reference: "",
      terms_reason: null, requests_per_minute: null, requests_per_day: null, raw_payload_storage: false },
    reason: "Explicit synthetic policy definition, not runtime activation or verified licensing.",
  };
  const intent = headers();
  const created = await page.request.post("/api/v1/provider-profiles", { headers: intent, data: input });
  expect(created.status()).toBe(201);
  expect(created.headers()["cache-control"]).toBe("no-store");
  expect(created.headers()["location"]).toBe("/api/v1/provider-profiles/yahoo");
  expect(created.headers()["etag"]).toBe('"1"');
  const original = await created.json() as Schemas["ProviderProfileRecord"];
  expect(original).toMatchObject({ version: 1, scope: "POLICY_DEFINITION_ONLY", runtime_selection_installed: false,
    credential_reference_present: false, policy: { provider_id: "yahoo", enabled: false } });
  const update: Schemas["ProviderProfileUpdate"] = { clear_credential: false, policy: { ...original.policy, enabled: true,
    terms_acknowledged: true, terms_reference: "Synthetic user policy reference, not a licensed plan",
    terms_reason: input.reason, requests_per_minute: 2, requests_per_day: 20 }, reason: input.reason };
  const versionIntent = headers(1);
  const enabled = await page.request.patch("/api/v1/provider-profiles/yahoo", { headers: versionIntent, data: update });
  expect(enabled.status()).toBe(200);
  expect(enabled.headers()["etag"]).toBe('"2"');
  const declared = await enabled.json() as Schemas["ProviderProfileRecord"];
  expect(declared).toMatchObject({ version: 2, runtime_selection_installed: false, policy: { enabled: true } });
  const stale = await page.request.patch("/api/v1/provider-profiles/yahoo", {
    headers: headers(1), data: { ...update, policy: { ...update.policy, enabled: false } },
  });
  expect(stale.status()).toBe(412);
  const paused = await page.request.patch("/api/v1/provider-profiles/yahoo", {
    headers: headers(2), data: { ...update, policy: { ...update.policy, enabled: false } },
  });
  expect(paused.status()).toBe(200);
  const final = await paused.json() as Schemas["ProviderProfileRecord"];
  expect(final).toMatchObject({ version: 3, runtime_selection_installed: false, policy: { enabled: false } });
  const replay = await page.request.post("/api/v1/provider-profiles", { headers: intent, data: input });
  expect(replay.status()).toBe(201);
  expect(replay.headers()["etag"]).toBe('"1"');
  expect(await replay.json()).toEqual(original);
  const patchReplay = await page.request.patch("/api/v1/provider-profiles/yahoo", { headers: versionIntent, data: update });
  expect(patchReplay.status()).toBe(200);
  expect(await patchReplay.json()).toEqual(declared);
  expect(await read<Schemas["ProviderProfileRecord"]>("provider-profiles/yahoo")).toEqual(final);
  const first = await read<Schemas["Page_ProviderProfileRecord_"]>("provider-profiles/yahoo/revisions?limit=2");
  expect(first.items).toEqual([final, declared]);
  expect(first.next_cursor).not.toBeNull();
  const second = await read<Schemas["Page_ProviderProfileRecord_"]>(`provider-profiles/yahoo/revisions?limit=2&cursor=${encodeURIComponent(first.next_cursor!)}`);
  expect(second).toEqual({ items: [original], next_cursor: null });
  expect((await read<Schemas["Page_ProviderProfileRecord_"]>("provider-profiles")).items).toEqual([final]);
  for (const field of ["owner_id", "credential_artifact_id", "profile_sha256", "profile_json", "provider_secret_dir"]) {
    expect(JSON.stringify({ original, declared, final, first, second })).not.toContain(field);
  }
  expect(await snapshot()).toEqual(before);
}
