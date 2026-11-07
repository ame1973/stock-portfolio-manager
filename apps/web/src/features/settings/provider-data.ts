import { useQuery } from "@tanstack/react-query";
import { api, type components } from "@spm/contracts";
import { checked } from "../../lib/api";
import { networkProviders, providerNames, type ProviderId } from "../sources/data";
import type { usePageState } from "./data";

export type ProviderProfile = components["schemas"]["ProviderProfileRecord"];
export type ProviderPolicy = components["schemas"]["ProviderPolicy"];
export const acceptsCredential = (provider: ProviderId) => ["eodhd", "alphavantage", "massive"].includes(provider);
export const networkProfile = (provider: ProviderId) => networkProviders.some(id => id === provider);
export const providerId = (value: string): ProviderId | undefined => Object.hasOwn(providerNames, value) ? value as ProviderId : undefined;

export function useProviderProfiles() {
  return useQuery({ queryKey: ["sources", "profiles"], queryFn: async ({ signal }) => checked(await api.GET("/api/v1/provider-profiles", { signal })) });
}
export function useProviderProfile(provider: ProviderId | undefined) {
  return useQuery({ queryKey: ["sources", "profile", provider], enabled: !!provider, queryFn: async ({ signal }) => {
    if (!provider) throw new Error("請選擇已登記來源。");
    return checked(await api.GET("/api/v1/provider-profiles/{provider_id}", { params: { path: { provider_id: provider } }, signal }));
  } });
}
export function useProfileHistory(record: ProviderProfile, page: ReturnType<typeof usePageState>) {
  return useQuery({ queryKey: ["sources", "profile-history", record.policy.provider_id, record.version, page.q, page.cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/provider-profiles/{provider_id}/revisions", { params: { path: { provider_id: record.policy.provider_id }, query: { q: page.q, cursor: page.cursor, limit: 25 } }, signal })) });
}
