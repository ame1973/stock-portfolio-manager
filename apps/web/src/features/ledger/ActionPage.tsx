import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "@spm/contracts";
import { Field, SelectField } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { EditorSheet } from "../../components/ui/editor-sheet";
import { checked, mutationHeaders } from "../../lib/api";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { FilterChips } from "../../components/forms/filter-chips";
import { useViewState } from "../../lib/view-state";
import { RecordList } from "../settings/RecordList";
import { usePageState } from "../settings/data";
import { ActionForm } from "./ActionForm";
import { ReasonedAction } from "./ReasonedAction";
import { SecurityName } from "./ReferenceName";
import { actionLabels, refreshLedger, useSecurity, type CorporateAction } from "./data";

const statusLabels = { CONFIRMED: "已採納條款，不代表完整估值", NEEDS_REVIEW: "待覆核，未完成分配", VOID: "已作廢，原始條款保留" };
const scopeLabels = { QUANTITY_TERMS: "股數條款，非市值／現金估值", DISPLAY_ONLY: "只改目前顯示代號，保留穩定證券身分", REVIEW_ONLY: "只保留覆核證據，未分配股數／成本／現金", NONE: "條款已停用" };
const canEdit = (record: CorporateAction) => record.is_current && record.status !== "VOID";

function ActionDetails({ record }: { record: CorporateAction }) {
  const security = useSecurity(record.security_id);
  return <>
    <h2 className="font-semibold">{record.effective_date} · {actionLabels[record.action_type]}</h2>
    <p className="mt-2 text-sm leading-7 text-secondary">目前代號：{security.data ? `${security.data.display_ticker} / ${security.data.exchange_code}` : record.security_id}；{record.lifecycle_status === "SUPERSEDED" ? "已被新修訂取代" : statusLabels[record.status]}</p>
    <p className="mt-2 break-all text-sm leading-7">條款比例：{record.ratio_numerator !== null && record.ratio_denominator !== null ? `${record.ratio_numerator} : ${record.ratio_denominator}` : "未提供，不代表 1:1"}</p>
    <p className="mt-2 text-sm leading-7">效用口徑：{scopeLabels[record.effect_scope]}。TERMS_NOT_A_VALUATION。</p>
    {record.review_required && <p className="mt-2 text-sm leading-7 text-danger">此穩定事件仍有未解決覆核；採納已知條款不代表已取得完整可計算持倉或估值。</p>}
    {record.action_type === "SYMBOL_CHANGE" && <p className="mt-2 text-sm leading-7 text-secondary">原顯示代號：{record.previous_symbol ?? "未採納／舊版未保存，不會猜測"}；新代號：{record.new_symbol ?? "未知"}。識別碼及供應商期間不會自動改寫，仍須核對映射。</p>}
    <details className="mt-3 text-sm"><summary className="min-h-11 cursor-pointer py-3 font-medium">原始條款、證據與版本</summary><dl className="grid gap-4 break-words rounded-md bg-muted p-4 sm:grid-cols-2">
      {[["紀錄識別碼", record.id], ["穩定事件識別碼", record.root_id], ["前一修訂", record.supersedes_id ?? "首個版本"], ["修訂／控制版本", `${record.revision} / ${record.version}`], ["穩定排序序號", String(record.created_sequence)], ["證券識別碼", record.security_id], ["人工依據", record.reason || "舊版未保存"], ["證據參考網址（未抓取）", record.evidence_url ?? "未提供"], ["備註", record.note || "未提供"], ["建立時間", record.created_at],
      ].map(([label, value]) => <div key={label}><dt className="text-secondary">{label}</dt><dd className="mt-1 break-all">{value}</dd></div>)}
    </dl></details>
    <Link to={`/settings/securities/${record.security_id}`} className="mt-2 inline-flex min-h-11 items-center text-sm text-primary underline">核對穩定身分、識別碼及供應商期間</Link>
  </>;
}

const empty = { security_id: "", root_id: "", action_type: "" as "" | CorporateAction["action_type"], status: "" as "" | CorporateAction["status"], from_date: "", to_date: "", include_history: false };

export function ActionPage() {
  const cache = useQueryClient();
  const page = usePageState("actions");
  const [filters, setFilters] = useViewState("filters:actions", empty);
  const [editor, setEditor] = useState<CorporateAction | "new" | null>(null);
  const [voiding, setVoiding] = useState<CorporateAction | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const result = useQuery({ queryKey: ["ledger", "actions", filters, page.q, page.cursor], queryFn: async ({ signal }) => checked(await api.GET("/api/v1/corporate-actions", { params: { query: {
    q: page.q, cursor: page.cursor, limit: 25, security_id: filters.security_id || undefined, root_id: filters.root_id || undefined, action_type: filters.action_type || undefined, status: filters.status || undefined, from_date: filters.from_date || undefined, to_date: filters.to_date || undefined, include_history: filters.include_history,
  } }, signal })) });
  function filter(change: Partial<typeof empty>) { setFilters(previous => ({ ...previous, ...change })); page.search(page.q); }
  function refresh() { void refreshLedger(cache); void cache.invalidateQueries({ queryKey: ["securities"] }); }
  return <div className="grid gap-6">
    <div className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-2xl"><h1 className="text-2xl font-semibold">企業行動</h1><p className="mt-2 text-sm leading-7 text-secondary">安裝共用證券目錄的明確條款與證據，不是多租戶邊界。精確拆合股、只改目前顯示代號及未分配事件的待覆核，保留不同口徑與穩定修訂鏈。</p></div>
      <Button data-editor-create disabled={editor !== null || voiding !== null} onClick={() => { setNotice(null); setEditor("new"); }}>新增企業行動</Button></div>
    <p className="text-sm leading-7 text-secondary">未確認的零碎股現金、合併與分拆不會編造持倉、成本或現金；已採納的未來股數條款不會提前改變今日股數。普通交易與轉倉仍由伺服器嚴格驗證。</p>
    {notice && <p role="status" className="text-sm leading-7 text-success">{notice}</p>}
    {editor !== null && <EditorSheet title={editor === "new" ? "新增企業行動" : "更正企業行動"} onClose={() => { setEditor(null); void result.refetch(); }}>
      <ActionForm key={editor === "new" ? "new" : editor.id} record={editor === "new" ? undefined : editor} onCancel={() => { setEditor(null); void result.refetch(); }} onSaved={record => {
        setEditor(null); page.search(""); refresh(); setNotice(record.review_required || record.status === "NEEDS_REVIEW" ? "已儲存企業行動條款；仍有待覆核，不代表已完成分配或估值。" : "已儲存企業行動條款；這不是完整估值或現金分配成功的宣稱。");
      }} />
    </EditorSheet>}
    {voiding && <ReasonedAction key={voiding.id} title="作廢企業行動" label="確認作廢企業行動" reasonLabel="企業行動作廢依據" description="原始條款與審計不會刪除。依賴此股數來源的後續賣出／轉倉、較後採納的代號變更或已獨立更改的目前代號，都可能拒絕整項操作。" onCancel={() => setVoiding(null)} onConfirm={async (reason, key) => {
      checked(await api.DELETE("/api/v1/corporate-actions/{id}", { params: { path: { id: voiding.id }, header: { ...await mutationHeaders(key), "If-Match": `"${voiding.version}"` } }, body: { reason } }));
      setVoiding(null); refresh(); setNotice("企業行動已作廢；原始條款、穩定排序及審計仍保留。");
    }} />}
    <details className="rounded-lg border border-border bg-surface p-4"><summary className="flex min-h-11 cursor-pointer items-center font-medium">篩選企業行動</summary><div className="mt-5 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
      <ReferenceSelect kind="securities" label="企業行動篩選證券" value={filters.security_id} registration={{ onChange: event => filter({ security_id: event.target.value }) }} clearSelection={() => filter({ security_id: "" })} />
      <SelectField label="篩選行動類型" value={filters.action_type} onChange={event => { const value = event.target.value; if (!value || Object.hasOwn(actionLabels, value)) filter({ action_type: value as typeof filters.action_type }); }}><option value="">全部條款類型</option>{Object.entries(actionLabels).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</SelectField>
      <SelectField label="篩選條款狀態" value={filters.status} onChange={event => { const value = event.target.value; if (value === "" || value === "CONFIRMED" || value === "NEEDS_REVIEW" || value === "VOID") filter({ status: value }); }}><option value="">全部條款狀態</option><option value="CONFIRMED">已採納條款（非估值）</option><option value="NEEDS_REVIEW">待覆核</option><option value="VOID">已作廢</option></SelectField>
      <Field label="行動生效起日" type="date" value={filters.from_date} onChange={event => filter({ from_date: event.target.value })} /><Field label="行動生效迄日" type="date" value={filters.to_date} onChange={event => filter({ to_date: event.target.value })} />
      <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" checked={filters.include_history} onChange={event => filter({ include_history: event.target.checked })} />包括已被取代的條款修訂</label>
      <Button variant="outline" onClick={() => { setFilters(empty); page.reset(); }}>清除企業行動篩選</Button>
    </div></details>
    <FilterChips values={filters} labels={{ security_id: "證券", action_type: "條款類型", status: "條款狀態", from_date: "生效起日", to_date: "生效迄日", root_id: "事件修訂鏈", include_history: "歷史修訂" }} onRemove={key => filter({ [key]: key === "include_history" ? false : "" })} />
    {filters.root_id && <div className="flex flex-wrap items-center gap-3 text-sm"><p>只顯示所選企業行動的修訂鏈。</p><Button variant="outline" onClick={() => filter({ root_id: "" })}>顯示全部行動事件</Button></div>}
    <RecordList title="企業行動" result={result} page={page} filtered={Object.values(filters).some(value => value !== false && value !== "")} columns={[
      { accessorKey: "effective_date", header: "生效日期" },
      { id: "security", header: "目前證券代號", cell: ({ row }) => <SecurityName id={row.original.security_id} /> },
      { id: "type", header: "條款類型", cell: ({ row }) => actionLabels[row.original.action_type] },
      { id: "terms", header: "精確比例／新代號", cell: ({ row }) => row.original.ratio_numerator !== null && row.original.ratio_denominator !== null
        ? `${row.original.ratio_numerator} : ${row.original.ratio_denominator}` : row.original.new_symbol ?? "未提供，不代表 1:1" },
      { id: "scope", header: "效用口徑，非估值", cell: ({ row }) => scopeLabels[row.original.effect_scope] },
      { id: "status", header: "紀錄狀態／覆核", cell: ({ row }) => <>{row.original.lifecycle_status === "SUPERSEDED" ? "已被新修訂取代" : statusLabels[row.original.status]}{row.original.review_required && <span className="block text-danger">仍有未解決覆核</span>}</> },
    ]}>{record => <article aria-label={`${record.effective_date} ${record.action_type} ${record.ratio_numerator ?? "未提供"}:${record.ratio_denominator ?? "未提供"}`} className="min-w-0 break-words">
      <ActionDetails record={record} /><div className="mt-3 flex flex-wrap gap-3">
        {canEdit(record) && <><Button variant="outline" disabled={editor !== null || voiding !== null} onClick={() => { setNotice(null); setEditor(record); }}>更正企業行動</Button><Button variant="outline" disabled={editor !== null || voiding !== null} onClick={() => { setNotice(null); setVoiding(record); }}>作廢企業行動</Button></>}
        <Button variant="outline" onClick={() => { filter({ root_id: record.root_id, include_history: true }); page.search(""); }}>查看企業行動修訂鏈</Button>
      </div>
    </article>}</RecordList>
  </div>;
}
