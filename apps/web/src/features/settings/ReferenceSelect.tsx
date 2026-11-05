import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { UseFormRegisterReturn } from "react-hook-form";
import { api } from "@spm/contracts";
import { checked, failureMessage } from "../../lib/api";
import { SelectField } from "../../components/forms/fields";

export function ReferenceSelect({ kind, label, registration, error, value, clearSelection }: {
  kind: "portfolios" | "brokers"; label: string; registration: UseFormRegisterReturn; error?: string;
  value: string; clearSelection: () => void;
}) {
  const [search, setSearch] = useState("");
  const result = useQuery({
    queryKey: [kind, "picker", search],
    queryFn: async ({ signal }) => kind === "portfolios"
      ? checked(await api.GET("/api/v1/portfolios", { params: { query: { q: search, limit: 100 } }, signal }))
      : checked(await api.GET("/api/v1/brokers", { params: { query: { q: search, limit: 100, status: "ACTIVE" } }, signal })),
  });
  return <div className="grid gap-3">
    <label className="grid gap-2 text-sm font-medium">搜尋{label}
      <input className="field" type="search" maxLength={100} value={search} onChange={event => {
        clearSelection(); setSearch(event.target.value);
      }} />
    </label>
    <SelectField label={label} {...registration} value={value} error={error} disabled={!result.data} help="更改搜尋會清除選擇，請從結果重新選取。">
      <option value="">請選擇{label}</option>
      {result.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
    </SelectField>
    {result.isPending && <p role="status" className="text-sm text-secondary">正在載入{label}…</p>}
    {result.isError && <div role="alert" className="text-sm text-danger">
      <p>{failureMessage(result.error)}</p><button type="button" className="min-h-11 underline" onClick={() => void result.refetch()}>重試載入{label}</button>
    </div>}
    {result.data?.next_cursor && <p className="text-sm text-secondary">僅列出前 100 個符合選項；請輸入更完整的名稱縮小搜尋。</p>}
    {result.data?.items.length === 0 && <p className="text-sm text-secondary">沒有符合的{label}；請先建立可使用的紀錄。</p>}
  </div>;
}
