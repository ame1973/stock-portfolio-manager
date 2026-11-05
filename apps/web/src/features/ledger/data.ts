import { useQuery, type QueryClient } from "@tanstack/react-query";
import { api, type components } from "@spm/contracts";
import { checked } from "../../lib/api";

export type Cash = components["schemas"]["CashEntry"];
export type Expense = components["schemas"]["Expense"];
export type Opening = components["schemas"]["OpeningCashRecord"];
export type Booking = Cash | Expense;
export const isCash = (record: Booking): record is Cash => "entry_role" in record;

export function refreshLedger(cache: QueryClient) {
  return Promise.all(["ledger", "accounts", "portfolios"].map(kind => cache.invalidateQueries({ queryKey: [kind] })));
}

export function useSecurity(id: string) {
  return useQuery({ queryKey: ["securities", "record", id], enabled: !!id, queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/securities/{id}", { params: { path: { id } }, signal })) });
}

export function useAccount(id: string) {
  return useQuery({ queryKey: ["accounts", "record", id], enabled: !!id, queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/accounts/{id}", { params: { path: { id } }, signal })) });
}
