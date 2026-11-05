import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router";
import { api } from "@spm/contracts";
import { Field, SelectField } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, mutationHeaders } from "../../lib/api";
import { RecordList } from "../settings/RecordList";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { usePageState } from "../settings/data";
import { BookingForm } from "./BookingForm";
import { CashNav } from "./CashNav";
import { ReasonedAction } from "./ReasonedAction";
import { isCash, refreshLedger, useAccount, type Booking } from "./data";

const kindLabels: Record<string, string> = { DEPOSIT: "外部存入", WITHDRAW: "外部提款", ADJUSTMENT: "非外部調整", REVERSAL: "精確沖銷", TRADE_SETTLEMENT: "交易交收", EXPENSE: "費用現金", TRANSFER_IN: "轉入", TRANSFER_OUT: "轉出", DIVIDEND_RECEIPT: "股息實收", CORPORATE_CASH: "企業行動現金" };
const statusLabels = { ACTIVE: "有效", SUPERSEDED: "已被新修訂取代", VOID: "已作廢，原值保留", REVERSAL: "沖銷紀錄" };
const canEdit = (record: Booking) => isCash(record) ? record.editable : record.status === "ACTIVE";

function BookingDetails({ record }: { record: Booking }) {
  const account = useAccount(record.account_id);
  const cash = isCash(record) ? record : undefined;
  const expense = isCash(record) ? undefined : record;
  const booked = isCash(record) ? record.booked_hkd : record.hkd_amount;
  return <>
    <h2 className="font-semibold">{cash?.effective_date ?? expense?.date} · {cash ? kindLabels[cash.kind] ?? cash.kind : expense?.category} · {record.currency} {record.amount}</h2>
    <p className="mt-2 text-sm leading-7 text-secondary">帳戶：{account.data?.name ?? record.account_id}；{statusLabels[record.status]}</p>
    <p className="mt-2 text-sm leading-7">歷史 HKD {cash ? "現金入帳" : "費用認列"}：{booked === null ? "未知，不是零（缺少歷史 FX）" : `HKD ${booked}`}。這不是剩餘現金的現價估值。</p>
    {cash && <p className="mt-2 text-sm leading-7 text-secondary">{cash.external_flow ? "外部現金流" : "非外部現金流"}；{cash.entry_role === "REVERSAL" ? "精確沖銷，不能單獨修改" : cash.editable ? "可透過完整更正／作廢保留歷史" : "來源流程管理，不能直接改寫或作廢"}</p>}
    <details className="mt-3 text-sm"><summary className="min-h-11 cursor-pointer py-3 font-medium">原值、來源與更正鏈</summary>
      <dl className="grid gap-4 break-words rounded-md bg-muted p-4 sm:grid-cols-2">
        {[
          ["歷史 FX（1 原幣 = HKD）", record.fx_rate_hkd ?? "未知，不是零"], ["備註", record.note || "未填寫"],
          ["紀錄識別碼", record.id], ["前一修訂", record.supersedes_id ?? "首個版本"], ["控制版本", String(record.version)], ["建立時間", record.created_at],
          ...(cash ? [["來源類型", cash.source_type], ["來源識別碼", cash.source_id], ["沖銷原項", cash.reverses_entry_id ?? "不是沖銷"], ["沖銷此項", cash.reversed_by_id ?? "未沖銷"], ["後續修訂", cash.superseded_by_id ?? "未被取代"]] : []),
        ].map(([label, value]) => <div key={label}><dt className="text-secondary">{label}</dt><dd className="mt-1 break-all">{value}</dd></div>)}
      </dl>
    </details>
    <Link to={`/cash/accounts/${record.account_id}`} className="inline-flex min-h-11 items-center text-sm text-primary underline">查看此帳戶餘額與期初</Link>
  </>;
}

export function BookingPage({ mode }: { mode: "cash" | "expense" }) {
  const cache = useQueryClient();
  const page = usePageState();
  const [query] = useSearchParams();
  const emptyFilters = { account_id: "", portfolio_id: "", currency: "" as "" | "HKD" | "USD", from_date: "", to_date: "", source_type: "", entry_role: "" as "" | "PRIMARY" | "REVERSAL", include_history: false };
  const [filters, setFilters] = useState({ ...emptyFilters, account_id: query.get("account_id") ?? "" });
  const [editor, setEditor] = useState<Booking | "new" | null>(null);
  const [voiding, setVoiding] = useState<Booking | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const title = mode === "cash" ? "現金紀錄" : "費用紀錄";
  const result = useQuery<{ items: Booking[]; next_cursor?: string | null }>({ queryKey: ["ledger", mode, filters, page.q, page.cursor], queryFn: async ({ signal }) => {
    const common = { q: page.q, cursor: page.cursor, limit: 25, account_id: filters.account_id || undefined, portfolio_id: filters.portfolio_id || undefined, currency: filters.currency || undefined, from_date: filters.from_date || undefined, to_date: filters.to_date || undefined };
    return mode === "cash"
      ? checked(await api.GET("/api/v1/cash-entries", { params: { query: { ...common, source_type: filters.source_type || undefined, entry_role: filters.entry_role || undefined } }, signal }))
      : checked(await api.GET("/api/v1/expenses", { params: { query: { ...common, include_history: filters.include_history } }, signal }));
  } });
  function filter(change: Partial<typeof emptyFilters>) { setFilters(previous => ({ ...previous, ...change })); page.search(page.q); }
  function saved() { setEditor(null); setVoiding(null); page.search(""); void refreshLedger(cache); }
  return <div className="grid gap-6">
    <CashNav />
    <div className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-2xl"><h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-2 text-sm leading-7 text-secondary">{mode === "cash" ? "完整日誌保留原始、修訂與精確沖銷。這不是已確認現金總額；不同幣別不會相加，來源管理項目必須透過原流程更正。" : "逐筆記錄營運費用及有依據的退款；交易費已包含在交收成本，不要重複登記。"}</p></div>
      <Button disabled={editor !== null || voiding !== null} onClick={() => { setNotice(null); setEditor("new"); }}>新增{title}</Button>
    </div>
    {notice && <p role="status" className="text-sm leading-7 text-success">{notice}</p>}
    {editor !== null && <BookingForm mode={mode} key={editor === "new" ? "new" : editor.id} record={editor === "new" ? undefined : editor}
      onCancel={() => { setEditor(null); void result.refetch(); }} onSaved={record => { saved(); setNotice(record.quality === "MISSING_FX" ? "已儲存原幣事實；歷史 HKD 仍未知，沒有採用預設匯率。" : "已儲存原幣及歷史入帳結果；原值、沖銷與審計仍保留。"); }} />}
    {voiding && <ReasonedAction key={voiding.id} title={`作廢${title}`} label={`確認作廢${title}`} reasonLabel="作廢入帳依據" description="會保留原始事實並精確沖銷原幣、歷史 FX 與 HKD；不會刪除或直接覆寫任何金融紀錄。" onCancel={() => setVoiding(null)}
      onConfirm={async (reason, key) => {
        const params = { path: { id: voiding.id }, header: { ...await mutationHeaders(key), "If-Match": `"${voiding.version}"` } };
        if (isCash(voiding)) checked(await api.DELETE("/api/v1/cash-entries/{id}", { params, body: { reason } }));
        else checked(await api.DELETE("/api/v1/expenses/{id}", { params, body: { reason } }));
        saved(); setNotice("已作廢並精確沖銷；原始紀錄與審計仍保留。");
      }} />}
    <details className="rounded-lg border border-border bg-surface p-4"><summary className="flex min-h-11 cursor-pointer items-center font-medium">篩選{title}</summary>
      <div className="mt-5 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
        <ReferenceSelect kind="portfolios" label="日誌篩選組合" value={filters.portfolio_id} registration={{ onChange: event => filter({ portfolio_id: event.target.value, account_id: "" }) }} clearSelection={() => filter({ portfolio_id: "", account_id: "" })} />
        <ReferenceSelect key={filters.portfolio_id} kind="accounts" label="日誌篩選帳戶" activeOnly={false} portfolioId={filters.portfolio_id || undefined} value={filters.account_id} registration={{ onChange: event => filter({ account_id: event.target.value }) }} clearSelection={() => filter({ account_id: "" })} />
        <SelectField label="日誌幣別" value={filters.currency} onChange={event => { const value = event.target.value; if (value === "" || value === "HKD" || value === "USD") filter({ currency: value }); }}><option value="">全部原幣（不加總）</option><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>
        <Field label="入帳起日" type="date" value={filters.from_date} onChange={event => filter({ from_date: event.target.value })} />
        <Field label="入帳迄日" type="date" value={filters.to_date} onChange={event => filter({ to_date: event.target.value })} />
        {mode === "cash" ? <><SelectField label="來源流程" value={filters.source_type} onChange={event => filter({ source_type: event.target.value })}>
          <option value="">全部來源</option>{["MANUAL_CASH", "CASH_CORRECTION", "TRADE", "EXPENSE", "OPENING_BALANCE", "OPENING_BALANCE_CORRECTION", "OPENING_REBASE", "CASH_TRANSFER_OUT", "CASH_TRANSFER_IN", "DIVIDEND_RECEIPT"].map(source => <option key={source} value={source}>{source}</option>)}
        </SelectField><SelectField label="日誌角色" value={filters.entry_role} onChange={event => { const value = event.target.value; if (value === "" || value === "PRIMARY" || value === "REVERSAL") filter({ entry_role: value }); }}><option value="">原始及沖銷</option><option value="PRIMARY">原始／修訂</option><option value="REVERSAL">精確沖銷</option></SelectField></>
          : <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" checked={filters.include_history} onChange={event => filter({ include_history: event.target.checked })} />包括已被取代的費用修訂</label>}
        <Button variant="outline" onClick={() => { setFilters(emptyFilters); page.reset(); }}>清除日誌篩選</Button>
      </div>
    </details>
    {filters.account_id && <Link to={`/cash/accounts/${filters.account_id}`} className="inline-flex min-h-11 items-center text-sm text-primary underline">查看所選帳戶餘額與期初</Link>}
    <RecordList title={title} page={page} result={result} filtered={Object.values(filters).some(value => typeof value === "string" && value !== "")}>{record => <article aria-label={`${isCash(record) ? record.effective_date : record.date} ${record.currency} ${record.amount}`} className="min-w-0 break-words">
      <BookingDetails record={record} />
      {canEdit(record) && <div className="mt-3 flex flex-wrap gap-3"><Button variant="outline" disabled={editor !== null || voiding !== null} onClick={() => { setNotice(null); setEditor(record); }}>更正{title}</Button>
        <Button variant="outline" disabled={editor !== null || voiding !== null} onClick={() => { setNotice(null); setVoiding(record); }}>作廢{title}</Button></div>}
    </article>}</RecordList>
  </div>;
}
