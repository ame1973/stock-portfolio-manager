import { useState, type ComponentProps } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@spm/contracts";
import { checked, failureMessage } from "../../lib/api";
import { SelectField } from "../../components/forms/fields";

export function ReferenceSelect({ kind, label, registration, error, value, clearSelection, portfolioId, autoFocus, activeOnly = true }: {
  kind: "portfolios" | "brokers" | "accounts" | "securities"; label: string; registration: ComponentProps<"select">; error?: string;
  value: string; clearSelection: () => void; portfolioId?: string; autoFocus?: boolean; activeOnly?: boolean;
}) {
  const [search, setSearch] = useState("");
  const result = useQuery({
    queryKey: [kind, "picker", search, portfolioId, activeOnly],
    queryFn: async ({ signal }) => {
      const query = { q: search, limit: 100 };
      switch (kind) {
        case "portfolios": return checked(await api.GET("/api/v1/portfolios", { params: { query }, signal }));
        case "brokers": return checked(await api.GET("/api/v1/brokers", { params: { query: { ...query, status: activeOnly ? "ACTIVE" : undefined } }, signal }));
        case "accounts": return checked(await api.GET("/api/v1/accounts", { params: { query: { ...query, status: activeOnly ? "ACTIVE" : undefined, portfolio_id: portfolioId } }, signal }));
        case "securities": return checked(await api.GET("/api/v1/securities", { params: { query }, signal }));
      }
    },
  });
  const found = result.data?.items.some(item => item.id === value);
  const selected = useQuery({
    queryKey: [kind, "record", value], enabled: !!value && !found,
    queryFn: async ({ signal }) => {
      const params = { path: { id: value } };
      switch (kind) {
        case "portfolios": return checked(await api.GET("/api/v1/portfolios/{id}", { params, signal }));
        case "brokers": throw new Error("請從目前的有效券商選項重新選取。");
        case "accounts": return checked(await api.GET("/api/v1/accounts/{id}", { params, signal }));
        case "securities": return checked(await api.GET("/api/v1/securities/{id}", { params, signal }));
      }
    },
  });
  const items = result.data?.items ?? [];
  const options = !found && selected.data ? [selected.data, ...items] : items;
  return <div className="grid gap-3">
    <label className="grid gap-2 text-sm font-medium">搜尋{label}
      <input className="field" type="search" autoFocus={autoFocus} maxLength={100} value={search} onChange={event => {
        clearSelection(); setSearch(event.target.value);
      }} />
    </label>
    <SelectField label={label} {...registration} value={value} error={error} disabled={!result.data} help="更改搜尋會清除選擇，請從結果重新選取。">
      <option value="">請選擇{label}</option>
      {!!value && !found && !selected.data && <option value={value}>正在確認已選紀錄：{value}</option>}
      {options.map(item => <option key={item.id} value={item.id} disabled={activeOnly && "status" in item && item.status === "ARCHIVED"}>
        {"display_ticker" in item ? `${item.display_ticker} / ${item.exchange_code} — ${item.name}` : "default_currency" in item ? `${item.name} (${item.default_currency})` : item.name}{"status" in item && item.status === "ARCHIVED" ? "（已封存）" : ""}
      </option>)}
    </SelectField>
    {result.isPending && <p role="status" className="text-sm text-secondary">正在載入{label}…</p>}
    {result.isError && <div role="alert" className="text-sm text-danger">
      <p>{failureMessage(result.error)}</p><button type="button" className="min-h-11 underline" onClick={() => void result.refetch()}>重試載入{label}</button>
    </div>}
    {!!value && !found && selected.isError && <div role="alert" className="text-sm text-danger"><p>未能確認已選{label}；請重試或重新選取。</p><button type="button" className="min-h-11 underline" onClick={() => void selected.refetch()}>重試確認{label}</button></div>}
    {activeOnly && !!value && selected.data && "status" in selected.data && selected.data.status === "ARCHIVED" && <p role="status" className="text-sm text-danger">此{label}已封存；更正前請先啟用或改選有效紀錄。歷史仍可作廢。</p>}
    {result.data?.next_cursor && <p className="text-sm text-secondary">僅列出前 100 個符合選項；請輸入更完整的名稱縮小搜尋。</p>}
    {result.data?.items.length === 0 && !value && <p className="text-sm text-secondary">沒有符合的{label}；請先建立可使用的紀錄。</p>}
  </div>;
}
