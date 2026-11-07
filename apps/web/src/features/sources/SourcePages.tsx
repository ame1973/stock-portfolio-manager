import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, Outlet } from "react-router";
import { Field, SelectField } from "../../components/forms/fields";
import { FilterChips } from "../../components/forms/filter-chips";
import { Button } from "../../components/ui/button";
import { EditorSheet } from "../../components/ui/editor-sheet";
import { useViewState } from "../../lib/view-state";
import { SecurityName } from "../ledger/ReferenceName";
import { RecordList } from "../settings/RecordList";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { usePageState } from "../settings/data";
import { CitedForm, type CitedResult } from "./CitedForm";
import { citationTitle, fetchLabels, isDividend, observationTerms, operationLabels, providerNames, refreshSources, useCoverage, useObservations, useProviderHealth, useProviders, type Coverage, type ObservationKind, type Operation, type ProviderId, type SourceFilters } from "./data";
import { ObservationDetails, RecordedPage, SourceLink, SourceReadState, SourceValues } from "./details";

export function DataCenterLayout() {
  return <div className="grid min-w-0 gap-6">
    <header className="max-w-3xl"><h1 className="text-2xl font-semibold">資料中心</h1><p className="mt-2 text-sm leading-7 text-secondary">先核對來源與穩定證券身分，再以明確範圍擷取。來源觀測不是已驗證事件、收入或實收；缺資料不等於零，所有上游實測仍為未測試。</p></header>
    <nav aria-label="資料中心分類" className="flex flex-wrap gap-2 border-b border-border pb-4">{[["providers", "來源設定"], ["coverage", "證券診斷"], ["dividends", "股息觀測"], ["splits", "拆股因子"], ["jobs", "擷取工作"], ["schedules", "來源排程"]].map(([path, label]) => <NavLink key={path} to={`/data-center/${path}`} className="inline-flex min-h-11 items-center rounded-md border border-transparent px-4 text-sm font-medium aria-[current=page]:border-border aria-[current=page]:bg-surface aria-[current=page]:text-primary">{label}</NavLink>)}</nav>
    <Outlet />
  </div>;
}
function ProviderField({ value, onChange, label }: { value: "" | ProviderId; onChange: (value: "" | ProviderId) => void; label: string }) {
  return <SelectField label={label} value={value} onChange={event => onChange(event.target.value as "" | ProviderId)}><option value="">全部來源（各自保留）</option>{(Object.keys(providerNames) as ProviderId[]).map(id => <option key={id} value={id}>{providerNames[id]}</option>)}</SelectField>;
}
function ProviderHistory({ provider }: { provider: ProviderId }) {
  const result = useProviderHealth(provider);
  return <section aria-label={`${providerNames[provider]} 已記錄來源診斷`} className="panel grid min-w-0 gap-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-semibold">{providerNames[provider]}：本機設定與歷史頁面分開</h3><Button variant="outline" disabled={result.isFetching} onClick={() => void result.refetch()}>重新載入此來源診斷</Button></div>
    <SourceReadState result={result} label="來源診斷" />
    {result.data && <><SourceValues values={[
      ["目前本機設定（不探測上游）", result.data.configured ? "已設定；即時健康未探測" : "未設定／不可用"], ["本機安全診斷碼", result.data.reason],
      ["本機狀態", fetchLabels[result.data.state]], ["上游實測／供應商帳單剩餘配額", "NOT_TESTED / 未知；軟體預留次數不是供應商帳單"],
      ["最後完整解析頁時間（非即時健康）", result.data.last_success_at ?? "尚無記錄"], ["最近來源錯誤碼", result.data.last_error_code ?? "未提供"],
      ["未採用的舊執行資料", `${result.data.legacy_runs_omitted} 筆；未重寫或推定成功`],
    ]} /><RecordedPage record={result.data.last_result} /></>}
  </section>;
}
export function ProvidersPage() {
  const result = useProviders();
  const [selected, setSelected] = useState<ProviderId | null>(null);
  return <div className="grid min-w-0 gap-5">
    <div className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-3xl"><h2 className="text-xl font-semibold">來源設定與候選能力</h2><p className="mt-2 text-sm leading-7 text-secondary">這是候選目錄與唯讀本機診斷，不是即時來源健康、授權核准或市場覆蓋保證。個人政策與私密金鑰由管理設定另行操作；新增及匯入保持停用，權利、啟用及明確排程彼此分開。</p><Link className="mt-2 inline-flex min-h-11 items-center text-sm text-primary underline" to="/settings/providers">管理來源授權與金鑰</Link></div><Button variant="outline" disabled={result.isFetching} onClick={() => void result.refetch()}>重新載入來源設定</Button></div>
    <SourceReadState result={result} label="來源設定" />
    {result.data && <><p className="text-sm text-secondary">已載入 {result.data.items.length} 個候選來源；Yahoo／yfinance 共用 YAHOO 上游與配額，不是兩個獨立來源。</p>
      <ul className="grid min-w-0 gap-4 xl:grid-cols-2">{result.data.items.map(row => <li key={row.provider_id} className="panel grid min-w-0 content-start gap-4" aria-label={providerNames[row.provider_id]}>
        <h3 className="text-lg font-semibold">{providerNames[row.provider_id]}</h3>
        <p className="text-sm leading-7">{row.configured ? "本機已設定；上游未探測（STALE）" : "未設定／本機不可用（UNCONFIGURED）"}</p>
        <SourceValues values={[["穩定上游分組", row.origin_group], ["本機安全診斷碼", row.configuration.reason], ["候選市場／操作", `${row.markets.join(" / ")}；${row.capabilities.length ? row.capabilities.map(value => operationLabels[value]).join(" / ") : "未宣告可用操作"}`], ["接入方式", row.access_mode === "MANUAL_EVIDENCE" ? "人工文件引用，非自動抓取" : row.access_mode === "OPTIONAL_SDK" ? "可選 SDK 包裝，仍需明確設定" : row.access_mode === "API_KEY" ? "伺服器私有密鑰與權限" : "明確啟用的公開 HTTP"], ["資料權利／實測", row.data_rights === "MANUAL_EVIDENCE_ONLY" ? "僅人工引用 / NOT_TESTED" : "未驗證 / NOT_TESTED"], ["原始回應儲存", "停用；不授予再散布權"]]} />
        <div className="flex flex-wrap gap-3"><Button variant="outline" aria-expanded={selected === row.provider_id} onClick={() => setSelected(previous => previous === row.provider_id ? null : row.provider_id)}>{selected === row.provider_id ? "收合" : "查看"}{providerNames[row.provider_id]}診斷</Button><SourceLink url={row.documentation_url ?? null} label="官方文件參考" /></div>
        {selected === row.provider_id && <ProviderHistory provider={selected} />}
      </li>)}</ul>
      <p className="text-xs leading-6 text-secondary">最後載入：{new Date(result.dataUpdatedAt).toLocaleString("zh-HK", { hour12: false })}。讀取不會建立工作、呼叫上游或補寫來源狀態。</p>
    </>}
  </div>;
}

function CoverageDetails({ record }: { record: Coverage }) {
  const availability = { OBSERVATIONS_AVAILABLE: "保留來源觀測，不代表已採納", DATA_NOT_CONFIRMED: "資料未確認，不是零收入", DATA_UNAVAILABLE: "此次來源資料不可用", PROVIDER_UNSUPPORTED: "來源未宣告支援" };
  return <article className="grid min-w-0 gap-4" aria-label={`${record.provider_id} ${record.security_id}`}><h3 className="font-semibold">{providerNames[record.provider_id]} · <SecurityName id={record.security_id} /></h3><SourceValues values={[
    ["操作／最近已記錄狀態", `${operationLabels[record.operation]}；${fetchLabels[record.status]}`], ["來源資料狀態", availability[record.availability]],
    ["目前本機設定", record.configuration.configured ? "已設定，未探測即時健康" : "未設定／不可用"], ["本機安全診斷碼", record.configuration.reason],
    ["候選市場／操作支援", `${record.supported_market ? "目錄宣告支援市場" : "目錄未宣告支援市場"}；${record.supported_operation ? "宣告支援操作" : "未宣告支援操作"}；不保證代號或覆蓋`],
    ["凍結歷史來源代號", record.provider_symbol ?? "未建立歷史代號；不由顯示代號猜測"], ["去重後來源觀測", `${record.observations_count} 筆；不是股息總額、實收或完整事件數`],
    ["可證明的覆蓋起訖", "未知 / 未知（NOT_ESTABLISHED）；要求日期不是覆蓋證明"], ["最後完整解析頁時間", record.last_success_at ?? "尚無記錄"],
    ["舊資料處理", `略過 ${record.legacy_runs_omitted} 筆未驗證執行；${record.legacy_status_ignored ? "既有狀態不採信" : "沒有需採信的舊狀態"}；不回填`],
    ["上游實測／帳單配額", "NOT_TESTED / 未知"],
  ]} /><RecordedPage record={record.last_result} /></article>;
}
export function CoveragePage() {
  const page = usePageState("sources:coverage");
  const initial = { security_id: "", provider_id: "" as "" | ProviderId, operation: "DIVIDENDS" as Operation };
  const [filters, setFilters] = useViewState("filters:sources:coverage", initial);
  const result = useCoverage(page, filters);
  function filter(change: Partial<typeof initial>) { setFilters(previous => ({ ...previous, ...change })); page.search(page.q); }
  return <div className="grid min-w-0 gap-5"><div className="max-w-3xl"><h2 className="text-xl font-semibold">證券來源診斷</h2><p className="mt-2 text-sm leading-7 text-secondary">選擇一個穩定證券身分，只讀比較各來源的已記錄頁面。股息與拆股分開；保留觀測不等於完整歷史、來源現時健康或已確認無收入。</p></div>
    <div className="panel grid content-start gap-5 sm:grid-cols-2 lg:grid-cols-3"><ReferenceSelect kind="securities" activeOnly={false} label="診斷證券" value={filters.security_id} registration={{ onChange: event => filter({ security_id: event.target.value }) }} clearSelection={() => filter({ security_id: "" })} />
      <ProviderField label="診斷來源" value={filters.provider_id} onChange={value => filter({ provider_id: value })} />
      <SelectField label="診斷操作" value={filters.operation} onChange={event => filter({ operation: event.target.value as Operation })}><option value="DIVIDENDS">股息觀測</option><option value="SPLITS">provider-only 拆股因子</option></SelectField>
      <div><Button variant="outline" onClick={() => { setFilters(initial); page.reset(); }}>清除診斷範圍</Button></div>
    </div>
    <FilterChips values={{ ...filters, operation: filters.operation === "DIVIDENDS" ? "" : filters.operation }} labels={{ security_id: "診斷證券", provider_id: "診斷來源", operation: "診斷操作" }} onRemove={key => filter(key === "operation" ? { operation: "DIVIDENDS" } : { [key]: "" })} />
    {!filters.security_id ? <p className="panel text-sm leading-7 text-secondary">請先選擇診斷證券；不會自動掃描全市場或建立擷取工作。</p> : <RecordList title="證券診斷" page={page} result={result} filtered={!!filters.provider_id || filters.operation !== "DIVIDENDS"} emptyMessage="此範圍尚無來源診斷；不代表沒有股息。" order={{ description: "來源 ID／證券 ID 升序", direction: "ascending" }} columns={[
      { id: "provider", header: "來源 ID／名稱", cell: ({ row }) => <>{row.original.provider_id}<span className="block text-secondary">{providerNames[row.original.provider_id]}</span></> },
      { id: "state", header: "已記錄狀態，非即時健康", cell: ({ row }) => fetchLabels[row.original.status] },
      { id: "configured", header: "目前本機設定", cell: ({ row }) => row.original.configuration.configured ? "已設定、未探測" : "未設定／不可用" },
      { id: "count", header: "來源觀測，非收入", cell: ({ row }) => String(row.original.observations_count) },
      { id: "coverage", header: "可證明的覆蓋期間", cell: () => "未知（NOT_ESTABLISHED）" },
    ]}>{record => <CoverageDetails record={record} />}</RecordList>}
  </div>;
}
export function ObservationsPage({ mode }: { mode: ObservationKind }) {
  const cache = useQueryClient();
  const [editor, setEditor] = useState(false);
  const [created, setCreated] = useState<CitedResult | null>(null);
  const page = usePageState(`sources:observations:${mode}`);
  const initial: SourceFilters = { security_id: "", provider_id: "", from_date: "", to_date: "" };
  const [filters, setFilters] = useViewState(`filters:sources:observations:${mode}`, initial);
  const result = useObservations(mode, page, filters);
  const title = mode === "dividends" ? "股息來源觀測" : "provider-only 拆股因子";
  const dateLabel = mode === "dividends" ? "除淨" : "生效";
  function filter(change: Partial<SourceFilters>) { setFilters(previous => ({ ...previous, ...change })); page.search(page.q); }
  return <div className="grid min-w-0 gap-5"><div className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-3xl"><h2 className="text-xl font-semibold">{title}</h2><p className="mt-2 text-sm leading-7 text-secondary">只列 immutable v1 來源證據（SOURCE_ONLY / NOT_ADOPTED）。{mode === "dividends" ? "每股金額、幣別及日期原樣保留；未知不是零，報價幣別不會補成股息幣別。" : "精確比例字串只供核對，不會採納股數、成本或現金。"}重複觀測不會變成多筆收入。</p></div><Button data-editor-create disabled={editor} onClick={() => setEditor(true)}>{citationTitle(mode)}</Button></div>
    {created && <section role="status" className="panel grid min-w-0 gap-3 text-sm leading-7"><p>伺服器已確認來源引用，可能是原鍵回覆；不是文件驗證、財務採納或實收。請以符合的篩選重新載入核對，不把回覆當成最新版本。</p><SourceValues values={[["確認引用識別碼", created.citation_id], ["確認觀測識別碼", created.observation_id]]} /></section>}
    {editor && <EditorSheet title={citationTitle(mode)} onClose={() => setEditor(false)}><CitedForm key={mode} mode={mode} onCancel={() => setEditor(false)} onSaved={record => { setEditor(false); setCreated(record); void refreshSources(cache); }} /></EditorSheet>}
    <details className="rounded-lg border border-border bg-surface p-4"><summary className="flex min-h-11 cursor-pointer items-center font-medium">篩選{title}</summary><div className="mt-5 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
      <ReferenceSelect kind="securities" activeOnly={false} label="觀測證券" value={filters.security_id} registration={{ onChange: event => filter({ security_id: event.target.value }) }} clearSelection={() => filter({ security_id: "" })} />
      <ProviderField label="觀測來源" value={filters.provider_id} onChange={value => filter({ provider_id: value })} />
      <Field label={`${dateLabel}起日`} type="date" value={filters.from_date} onChange={event => filter({ from_date: event.target.value })} />
      <Field label={`${dateLabel}迄日`} type="date" value={filters.to_date} onChange={event => filter({ to_date: event.target.value })} help={mode === "dividends" ? "明確日期範圍會排除未知除淨日，不會用擷取日代替。" : "按來源生效日期篩選，不是擷取時間。"} />
      <div><Button variant="outline" onClick={() => { setFilters(initial); page.reset(); }}>清除觀測篩選</Button></div>
    </div></details>
    <FilterChips values={filters} labels={{ security_id: "觀測證券", provider_id: "觀測來源", from_date: `${dateLabel}起日`, to_date: `${dateLabel}迄日` }} onRemove={key => filter({ [key]: "" })} />
    {result.data && <p className="text-sm leading-7 text-secondary">IMMUTABLE_SOURCE_V1；保留的舊來源 {result.data.legacy_records_omitted} 筆未列入，不會推定驗證或重寫。排序固定首次擷取時間／ID 倒序，後續觀測不會改變首次時間或頁面順序。</p>}
    <RecordList title={title} page={page} result={result} filtered={Object.values(filters).some(value => value !== "")} emptyMessage="尚無可顯示的 v1 來源觀測；這不是已確認沒有股息或拆股。請核對來源設定、證券代號期間與明確擷取工作。" order={{ description: "首次擷取時間／ID 倒序（非事件日期）", direction: "descending" }} columns={[
      { id: "retrieved", header: "首次擷取時間（UTC）", cell: ({ row }) => row.original.retrieved_at },
      { id: "source", header: "來源／當次代號", cell: ({ row }) => <>{providerNames[row.original.provider_id]}<span className="block text-secondary">{row.original.provider_symbol}</span></> },
      { id: "date", header: `${dateLabel}日期`, cell: ({ row }) => isDividend(row.original) ? row.original.ex_date ?? "未知，不用擷取日代替" : row.original.effective_date },
      { id: "terms", header: mode === "dividends" ? "每股來源金額／幣別" : "精確因子，非採納股數", cell: ({ row }) => <span className="break-all">{observationTerms(row.original)}</span> },
      { id: "basis", header: "來源口徑／未採納", cell: ({ row }) => isDividend(row.original) ? row.original.adjustment_basis : row.original.effect_scope },
    ]}>{record => <ObservationDetails record={record} />}</RecordList>
  </div>;
}
