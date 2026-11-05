import { useQuery, type QueryClient } from "@tanstack/react-query";
import { api, type components } from "@spm/contracts";
import { checked } from "../../lib/api";

export type Cash = components["schemas"]["CashEntry"];
export type Expense = components["schemas"]["Expense"];
export type Opening = components["schemas"]["OpeningCashRecord"];
export type CorporateAction = components["schemas"]["CorporateAction"];
export const actionLabels: Record<CorporateAction["action_type"], string> = { SPLIT: "拆股", REVERSE_SPLIT: "合股", SYMBOL_CHANGE: "目前顯示代號變更", CASH_IN_LIEU: "零碎股現金補償（待分配）", MERGER: "合併（待分配）", SPIN_OFF: "分拆（待分配）" };
export const unallocatedAction = (kind: CorporateAction["action_type"]) => ["CASH_IN_LIEU", "MERGER", "SPIN_OFF"].includes(kind);
export type CashTransfer = components["schemas"]["CashTransfer"];
export type StockTransfer = components["schemas"]["SecurityTransfer"];
export type Transfer = CashTransfer | StockTransfer;
export const isCashTransfer = (record: Transfer): record is CashTransfer => "source_currency" in record;
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
