import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "@spm/contracts";
import { Field, SelectField } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, mutationHeaders } from "../../lib/api";
import { RecordList } from "../settings/RecordList";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { usePageState } from "../settings/data";
import { CashNav } from "./CashNav";
import { ReasonedAction } from "./ReasonedAction";
import { TransferForm } from "./TransferForm";
import { isCashTransfer, refreshLedger, useAccount, useSecurity, type Transfer } from "./data";

const statusLabels = { ACTIVE: "有效", SUPERSEDED: "已被新修訂取代", VOID: "已作廢，原值保留" };
const qualityLabels = { OK: "歷史兩腿已記錄", MISSING_FX: "歷史 FX／HKD 未知", NEEDS_REVIEW: "歷史入帳差額需要覆核，不是已確認匯兌損益" };
const canEdit = (record: Transfer) => record.status === "ACTIVE" && (isCashTransfer(record) || record.is_current);

function TransferDetails({ record }: { record: Transfer }) {
  const source = useAccount(record.source_account_id);
  const target = useAccount(record.target_account_id);
  const cash = isCashTransfer(record) ? record : undefined;
  const stock = isCashTransfer(record) ? undefined : record;
  const security = useSecurity(stock?.security_id ?? "");
  return <>
    <h2 className="font-semibold">{record.transfer_date} · {cash ? `${cash.source_currency} ${cash.source_amount} → ${cash.target_currency} ${cash.target_amount}` : `${stock?.quantity} 股轉倉`}</h2>
    <p className="mt-2 break-words text-sm leading-7 text-secondary">來源：{source.data?.name ?? record.source_account_id}；目標：{target.data?.name ?? record.target_account_id}。{statusLabels[record.status]}</p>
    {cash ? <div className="mt-3 grid gap-3 text-sm leading-7">
      <p>{qualityLabels[cash.quality]}；兩腿均為內部非外部現金流，本金不含另記費用。</p>
      <p>來源歷史 HKD：{cash.source_booked_hkd === null ? "未知，不是零" : `HKD ${cash.source_booked_hkd}`}；目標歷史 HKD：{cash.target_booked_hkd === null ? "未知，不是零" : `HKD ${cash.target_booked_hkd}`}</p>
      <p>歷史兩腿入帳差額（目標 − 來源）：{cash.booking_delta_hkd === null ? "未知，不是零" : `HKD ${cash.booking_delta_hkd}`}。不是目前估值或已實現 FX 損益。</p>
    </div> : <div className="mt-3 grid gap-3 text-sm leading-7">
      <p>目前證券代碼：{security.data ? `${security.data.display_ticker} / ${security.data.exchange_code}` : stock?.security_id}</p>
      <p>建立時成本快照：HKD {stock?.cost_moved_hkd}（BOOKED_AT_CREATION）。</p>
      <p className="text-secondary">目前回放可因先前交易更正而重新分配成本，這個原始快照不會改寫。轉倉本身不新增現金、不實現獲利，也不是最新成本／估值。</p>
    </div>}
    <details className="mt-3 text-sm"><summary className="min-h-11 cursor-pointer py-3 font-medium">原始條款與穩定修訂鏈</summary><dl className="grid gap-4 break-words rounded-md bg-muted p-4 sm:grid-cols-2">
      {[["紀錄識別碼", record.id], ["穩定事件識別碼", record.root_id], ["前一版本", record.supersedes_id ?? "首個版本"], ["修訂／控制版本", `${record.revision} / ${record.version}`], ["來源帳戶識別碼", record.source_account_id], ["目標帳戶識別碼", record.target_account_id], ["備註", record.note || "未填寫"], ["建立時間", record.created_at],
        ...(cash ? [["歷史 FX", cash.source_currency === "HKD" && cash.target_currency === "HKD" ? "HKD 1:1 數學恆等值" : cash.fx_rate_hkd ?? "未知，不是零"], ["USD 腿指定 HKD", cash.actual_hkd ?? "未指定，由歷史 FX 記錄或保持未知"], ["轉帳依據", cash.reason]] : [["穩定排序序號", String(stock?.created_sequence)], ["成本快照口徑", stock?.cost_scope ?? "未知"]]),
      ].map(([label, value]) => <div key={label}><dt className="text-secondary">{label}</dt><dd className="mt-1 break-all">{value}</dd></div>)}
    </dl></details>
    <div className="mt-3 flex flex-wrap gap-4 text-sm"><Link to={`/cash/accounts/${record.source_account_id}`} className="inline-flex min-h-11 items-center text-primary underline">來源帳戶餘額與期初</Link><Link to={`/cash/accounts/${record.target_account_id}`} className="inline-flex min-h-11 items-center text-primary underline">目標帳戶餘額與期初</Link></div>
  </>;
}

const empty = { portfolio_id: "", account_id: "", security_id: "", root_id: "", from_date: "", to_date: "", currency: "" as "" | "HKD" | "USD", include_history: false };

export function TransferPage({ mode }: { mode: "cash" | "security" }) {
  const page = usePageState();
  const cache = useQueryClient();
  const [filters, setFilters] = useState(empty);
  const [editor, setEditor] = useState<Transfer | "new" | null>(null);
  const [voiding, setVoiding] = useState<Transfer | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const title = mode === "cash" ? "成對現金轉帳" : "股票轉倉";
  const result = useQuery<{ items: Transfer[]; next_cursor?: string | null }>({ queryKey: ["ledger", "transfers", mode, filters, page.q, page.cursor], queryFn: async ({ signal }) => {
    const common = { q: page.q, cursor: page.cursor, limit: 25, portfolio_id: filters.portfolio_id || undefined, account_id: filters.account_id || undefined, root_id: filters.root_id || undefined, from_date: filters.from_date || undefined, to_date: filters.to_date || undefined, include_history: filters.include_history };
    return mode === "cash" ? checked(await api.GET("/api/v1/cash-transfers", { params: { query: { ...common, currency: filters.currency || undefined } }, signal }))
      : checked(await api.GET("/api/v1/security-transfers", { params: { query: { ...common, security_id: filters.security_id || undefined } }, signal }));
  } });
  function filter(change: Partial<typeof empty>) { setFilters(previous => ({ ...previous, ...change })); page.search(page.q); }
  function saved() { setEditor(null); setVoiding(null); page.search(""); void refreshLedger(cache); }
  return <div className="grid gap-6">
    {mode === "cash" && <CashNav />}
    <div className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-2xl"><h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-2 text-sm leading-7 text-secondary">{mode === "cash" ? "同組合內的原幣本金成對移動；明確換幣可在同帳戶內進行。兩腿一起提交或沖銷，費用另記，不以金額比例推算 FX。" : "同組合內不同帳戶的證券轉倉，保留成本及穩定排序。伺服器驗證全期依賴，不會以新修訂日期掩蓋超賣。"}</p></div>
      <Button disabled={editor !== null || voiding !== null} onClick={() => { setNotice(null); setEditor("new"); }}>新增{title}</Button>
    </div>
    <div className="flex flex-wrap gap-4 text-sm"><Link to={mode === "cash" ? "/transfers" : "/cash/transfers"} className="inline-flex min-h-11 items-center text-primary underline">{mode === "cash" ? "前往股票轉倉" : "前往成對現金轉帳"}</Link><Link to="/cash/expenses" className="inline-flex min-h-11 items-center text-primary underline">另記真實費用／退款</Link></div>
    {notice && <p role="status" className="text-sm leading-7 text-success">{notice}</p>}
    {editor !== null && <TransferForm mode={mode} key={editor === "new" ? "new" : editor.id} record={editor === "new" ? undefined : editor} onCancel={() => { setEditor(null); void result.refetch(); }} onSaved={record => { saved(); setNotice(isCashTransfer(record) ? "已儲存成對現金事實；請核對兩腿歷史入帳與品質，差額不是匯兌獲利。" : "已儲存股票轉倉及建立時成本快照；沒有新增現金或已實現獲利。"); }} />}
    {voiding && <ReasonedAction key={voiding.id} title={`作廢${title}`} label={`確認作廢${title}`} reasonLabel="轉倉作廢依據" description={mode === "cash" ? "會保留原始兩腿，並按各腿原幣、歷史 FX 與 HKD 精確沖銷；不能只作廢一腿。" : "會保留原始成本與修訂鏈；若目標帳戶的後續賣出／轉倉失去股份來源，整項操作會拒絕。"} onCancel={() => setVoiding(null)} onConfirm={async (reason, key) => {
      const params = { path: { id: voiding.id }, header: { ...await mutationHeaders(key), "If-Match": `"${voiding.version}"` } };
      if (isCashTransfer(voiding)) checked(await api.DELETE("/api/v1/cash-transfers/{id}", { params, body: { reason } }));
      else checked(await api.DELETE("/api/v1/security-transfers/{id}", { params, body: { reason } }));
      saved(); setNotice(mode === "cash" ? "已作廢成對現金轉帳，兩腿均已精確沖銷；原始證據仍保留。" : "已作廢股票轉倉；原始成本、排序及審計仍保留。");
    }} />}
    <details className="rounded-lg border border-border bg-surface p-4"><summary className="flex min-h-11 cursor-pointer items-center font-medium">篩選{title}</summary><div className="mt-5 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
      <ReferenceSelect kind="portfolios" label="轉倉篩選組合" value={filters.portfolio_id} registration={{ onChange: event => filter({ portfolio_id: event.target.value, account_id: "" }) }} clearSelection={() => filter({ portfolio_id: "", account_id: "" })} />
      <ReferenceSelect key={filters.portfolio_id} kind="accounts" label="轉倉篩選帳戶" activeOnly={false} portfolioId={filters.portfolio_id || undefined} value={filters.account_id} registration={{ onChange: event => filter({ account_id: event.target.value }) }} clearSelection={() => filter({ account_id: "" })} />
      {mode === "cash" ? <SelectField label="轉帳篩選原幣" value={filters.currency} onChange={event => { const value = event.target.value; if (value === "" || value === "HKD" || value === "USD") filter({ currency: value }); }}><option value="">任一腿原幣（不加總）</option><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>
        : <ReferenceSelect kind="securities" label="轉倉篩選證券" value={filters.security_id} registration={{ onChange: event => filter({ security_id: event.target.value }) }} clearSelection={() => filter({ security_id: "" })} />}
      <Field label="轉倉起日" type="date" value={filters.from_date} onChange={event => filter({ from_date: event.target.value })} /><Field label="轉倉迄日" type="date" value={filters.to_date} onChange={event => filter({ to_date: event.target.value })} />
      <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" checked={filters.include_history} onChange={event => filter({ include_history: event.target.checked })} />包括已被取代的轉倉修訂</label>
      <Button variant="outline" onClick={() => { setFilters(empty); page.reset(); }}>清除轉倉篩選</Button>
    </div></details>
    {filters.root_id && <div className="flex flex-wrap items-center gap-3 text-sm"><p>只顯示所選轉倉事件的修訂鏈。</p><Button variant="outline" onClick={() => filter({ root_id: "" })}>顯示全部轉倉事件</Button></div>}
    <RecordList title={title} result={result} page={page} filtered={Object.values(filters).some(value => typeof value === "string" && value !== "")}>{record => <article aria-label={`${record.transfer_date} ${isCashTransfer(record) ? `${record.source_currency} ${record.source_amount} ${record.target_currency} ${record.target_amount}` : `${record.quantity} 股轉倉`}`} className="min-w-0 break-words">
      <TransferDetails record={record} /><div className="mt-3 flex flex-wrap gap-3">
        {canEdit(record) && <><Button variant="outline" disabled={editor !== null || voiding !== null} onClick={() => { setNotice(null); setEditor(record); }}>更正{title}</Button><Button variant="outline" disabled={editor !== null || voiding !== null} onClick={() => { setNotice(null); setVoiding(record); }}>作廢{title}</Button></>}
        <Button variant="outline" onClick={() => { filter({ root_id: record.root_id, include_history: true }); page.search(""); }}>查看轉倉修訂鏈</Button>
      </div>
    </article>}</RecordList>
  </div>;
}
