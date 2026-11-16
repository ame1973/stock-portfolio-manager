import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, type components } from "@spm/contracts";
import { Field, SelectField } from "../../components/forms/fields";
import { FilterChips } from "../../components/forms/filter-chips";
import { Button } from "../../components/ui/button";
import { EditorSheet } from "../../components/ui/editor-sheet";
import { checked } from "../../lib/api";
import { useDesktop } from "../../lib/use-desktop";
import { useViewState } from "../../lib/view-state";
import { SecurityName } from "../ledger/ReferenceName";
import { RecordList } from "../settings/RecordList";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { PasswordForm } from "../settings/SecuritySettingsPage";
import { usePageState } from "../settings/data";
import { SourceLink, SourceReadState, SourceValues } from "../sources/details";
import { ManualEventCreateForm, ManualEventRevisionForm } from "./ManualEventForm";
import { RejectEventForm } from "./RejectEventForm";
import { validRejection, validTarget, type RevisionTarget } from "./event-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Revision = Schemas["DividendEventRevisionRecord"];
type Terms = Event | Revision;
type Rejection = Schemas["RejectionDecision"];

const statuses: Record<Event["workflow_status"], string> = {
  PROVISIONAL: "暫定，待核對", CORROBORATED: "來源條款相符，非文件驗證", VERIFIED: "人工接受（非文件驗證）",
  CONFLICT: "來源條款有衝突", INCOMPLETE: "條款不完整", WITHDRAWN: "已撤回，歷史保留",
};
const eventTypes: Record<Event["event_type"], string> = {
  REGULAR: "一般股息", SPECIAL: "特別股息", INTERIM: "中期股息", FINAL: "末期股息", SUPPLEMENTAL: "補充股息",
  ETF_DISTRIBUTION: "ETF 分派", REIT_DISTRIBUTION: "REIT 分派", UNKNOWN: "類型未知",
};
const fields: Record<Schemas["EventEvidence"]["fields"][number], string> = {
  ex_date: "除淨日", announce_date: "宣告日", record_date: "記錄日", pay_date: "付款日", event_type: "事件類型",
  currency: "幣別", dps: "每股金額", adjustment_basis: "調整基準",
};
const basis: Record<Event["adjustment_basis"], string> = {
  AS_DECLARED: "記錄為宣告口徑，不代表已驗證", SPLIT_ADJUSTED: "拆股調整後，不能直接當作宣告金額", UNKNOWN: "未知，不會猜測",
};
const decisions: Record<Event["decision_kind"], string> = {
  CANDIDATE: "明確關聯候選資料", MANUAL: "人工條款", VERIFY: "人工接受", WITHDRAW: "人工撤回",
};
const names = (items: Schemas["EventEvidence"]["fields"]) => items.length ? items.map(item => fields[item]).join("、") : "未列明";

function Citation({ reference, label }: { reference: Schemas["EventReference"] | null; label: string }) {
  if (!reference) return <p className="text-sm text-secondary">此版本沒有人工引用；不推定文件已核實。</p>;
  let hasPrivateParts = false;
  try { if (reference.url) { const url = new URL(reference.url); hasPrivateParts = !!(url.search || url.hash); } } catch { /* SourceLink rejects invalid URLs. */ }
  return <div className="grid gap-2 text-sm leading-7"><p>引用參考：<span className="break-all">{reference.citation}</span></p>
    {reference.url_withheld || hasPrivateParts ? <p className="text-secondary">引用包含查詢或片段，連結未提供；請依引用參考核對文件。</p>
      : <SourceLink url={reference.url} label={label} />}</div>;
}

function EventTerms({ record }: { record: Terms }) {
  return <div className="grid min-w-0 gap-4">
    <SourceValues values={[
      ["事件類型", eventTypes[record.event_type]], ["調整基準", basis[record.adjustment_basis]],
      ["其他日期", `宣告：${record.announce_date ?? "未知"}；記錄：${record.record_date ?? "未知"}；付款：${record.pay_date ?? "未知"}`],
      ["決定方式", `${decisions[record.decision_kind]}${record.manual_override ? "；人工覆寫" : ""}`],
      ["人工鎖定", record.manual_locked ? "已鎖定；不是文件驗證" : "未鎖定"],
      ["已記錄獨立來源分組", record.independent_origin_groups === null ? "未提供，不推定來源數" : `${record.independent_origin_groups} 個；不代表來源覆蓋或文件核實`],
      ["候選修訂識別碼", record.candidate_revision_id ?? "此版本未記錄候選修訂"], ["記錄時間（UTC）", record.created_at],
      ["決定依據", record.reason],
    ]} />
    <Citation reference={record.reference} label="開啟事件引用" />
    {record.evidence.length > 0 && <section aria-label="已關聯來源觀測" className="grid min-w-0 gap-2 text-sm leading-7"><p className="font-medium">已關聯來源觀測，不代表文件已驗證</p>
      <ul className="grid gap-2">{record.evidence.map(item => <li key={item.observation_id} className="break-all">觀測 {item.observation_id}；欄位：{names(item.fields)}</li>)}</ul></section>}
    {record.assessment && <section aria-label="已記錄候選評估" className="grid min-w-0 gap-2 text-sm leading-7"><p className="font-medium">已記錄候選評估，不是即時來源或收入證明</p>
      <p>來源評估狀態：{record.assessment.status}；來源分組：{record.assessment.independent_origins.join("、") || "未列明"}。</p>
      <p>衝突欄位：{names(record.assessment.conflicting_fields)}；未完整原因：{record.assessment.incomplete_reasons.join("、") || "未列明"}。</p>
      <p className="break-all">評估列入的觀測識別碼：{record.assessment.eligible_observation_ids.join("、") || "未列明"}；不是收入資格。</p>
      <ul className="grid gap-2">{record.assessment.checks.map(check => <li key={check.observation_id} className="break-all">觀測 {check.observation_id}；比較金額 {check.comparison_dps ?? "未知"}；口徑 {check.comparison_basis}；支持欄位 {names(check.supporting_fields)}；衝突欄位 {names(check.conflicting_fields)}；未完整原因 {check.incomplete_reasons.join("、") || "未列明"}；基準聲明 {check.basis_assertion_id ?? "未記錄"}。</li>)}</ul>
    </section>}
  </div>;
}

function RejectionDetails({ record }: { record: Rejection }) {
  return <div className="grid min-w-0 gap-3"><SourceValues values={[
    ["拒絕識別碼", record.id], ["拒絕的實體修訂", `${record.revision_no} / ${record.revision_id}`],
    ["記錄時間（UTC）", record.created_at], ["拒絕依據", record.reason],
  ]} /><Citation reference={record.reference} label="開啟拒絕引用" /></div>;
}

function RejectionState({ record, disabled, onRevise, onReject }: { record: Event; disabled: boolean; onRevise: (target: RevisionTarget) => void; onReject: (target: Event) => void }) {
  const result = useQuery({
    queryKey: ["dividends", "rejection", record.id, record.current_revision_id, record.version], staleTime: 0, refetchOnMount: "always",
    queryFn: async ({ signal }) => checked(await api.GET("/api/v1/dividends/events/{event_id}/rejection", { params: { path: { event_id: record.id } }, signal })),
  });
  const value = result.data;
  const matches = value && value.event_id === record.id && value.revision_id === record.current_revision_id && value.revision_no === record.version;
  const consistent = value && value.scope === "OWNER_REJECTION_STATE_ONLY" && value.current_use_scope === "REVALIDATION_REQUIRED"
    && value.document_verification === "NOT_ESTABLISHED" && value.eligibility_scope === "NOT_ESTABLISHED"
    && (value.state === "NOT_REJECTED" ? value.rejection === null
      : value.state === "REJECTED" && value.rejection !== null && validRejection(value.rejection, record));
  const target = value && matches && consistent ? { event: record, rejection: value.rejection } : null;
  const ready = target && !result.isError && !result.isFetching && !result.isPaused;
  return <section aria-label="獨立拒絕狀態" className="grid min-w-0 gap-4 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-semibold">獨立拒絕狀態</h3><Button variant="outline" disabled={result.isFetching} onClick={() => void result.refetch()}>重新載入拒絕狀態</Button></div>
    <p className="text-sm leading-7 text-secondary">事件與拒絕狀態兩者分開讀取，不是同一快照；即使事件版本不變，拒絕狀態也可能改變。使用前必須重新核對。</p>
    <SourceReadState result={result} label="拒絕狀態" />
    {value && (!matches ? <p role="alert" className="text-sm text-danger">事件與拒絕狀態的版本不一致；請重新載入事件清單及拒絕狀態，不能據此判斷。</p>
      : !consistent ? <p role="alert" className="text-sm text-danger">拒絕狀態無法核對；請重新載入，不能據此判斷。</p>
        : <><p className="text-sm leading-7">{value.state === "REJECTED" ? "此版本已記錄拒絕；不代表撤回事件、實收沖銷或文件驗證。" : "此版本未記錄拒絕；這不是接受決定或財務資格。"}</p>
          {value.rejection && <RejectionDetails record={value.rejection} />}</>)}
    {ready && target && (validTarget(target) ? <div className="flex flex-wrap gap-3"><Button variant="outline" disabled={disabled} onClick={() => onRevise({
      event: { ...record, reference: record.reference ? { ...record.reference } : null },
      rejection: target.rejection ? { ...target.rejection, reference: { ...target.rejection.reference } } : null,
    })}>修訂人工股息事件</Button>
      {target.rejection === null && <Button variant="outline" disabled={disabled} onClick={() => onReject(record)}>拒絕此事件版本</Button>}</div> : <p role="alert" className="text-sm text-danger">事件或拒絕範圍無法核對，或版本已達上限；不能開始修訂或拒絕，請重新載入。</p>)}
    {value && <p className="text-xs leading-6 text-secondary">拒絕狀態最後載入：{new Date(result.dataUpdatedAt).toLocaleString("zh-HK", { hour12: false })}；只表示該次讀取。</p>}
  </section>;
}

function RevisionHistory({ record }: { record: Event }) {
  const page = usePageState();
  const result = useQuery({ queryKey: ["dividends", "revisions", record.id, page.cursor], staleTime: 0,
    queryFn: async ({ signal }) => checked(await api.GET("/api/v1/dividends/events/{event_id}/revisions", { params: { path: { event_id: record.id }, query: { cursor: page.cursor, limit: 25 } }, signal })),
    select: data => ({ ...data, items: data.items.map(row => ({ ...row, id: row.revision_id })) }),
  });
  return <section aria-label="事件修訂歷史" className="grid min-w-0 gap-4"><h3 className="font-semibold">事件修訂歷史</h3>
    <p className="text-sm leading-7 text-secondary">按版本倒序保留原始決定；歷史版本不是目前資格、應收或實收。</p>
    <RecordList title="事件修訂" page={page} result={result} searchable={false} emptyMessage="此頁沒有事件修訂；請重新載入核對。">{row => <article className="grid min-w-0 gap-4">
      <h4 className="font-semibold">歷史版本 {row.revision_no}；{statuses[row.workflow_status]}</h4>
      <p className="break-all text-sm">除淨日 {row.ex_date}；每股 {row.currency} {row.dps}</p>
      <SourceValues values={[["實體修訂識別碼", row.revision_id], ["穩定事件識別碼", row.event_id]]} /><EventTerms record={row} />
    </article>}</RecordList></section>;
}

function RejectionHistory({ record }: { record: Event }) {
  const page = usePageState();
  const result = useQuery({ queryKey: ["dividends", "rejections", record.id, page.cursor], staleTime: 0,
    queryFn: async ({ signal }) => checked(await api.GET("/api/v1/dividends/events/{event_id}/rejections", { params: { path: { event_id: record.id }, query: { cursor: page.cursor, limit: 25 } }, signal })),
    select: data => ({ ...data, items: data.items.map(row => ({ ...row, id: row.decision.id })) }),
  });
  return <section aria-label="拒絕歷史" className="grid min-w-0 gap-4"><h3 className="font-semibold">拒絕歷史</h3>
    <p className="text-sm leading-7 text-secondary">後繼解決版本是已記錄的決定，不證明文件、持倉或實收。</p>
    <RecordList title="拒絕紀錄" page={page} result={result} searchable={false} emptyMessage="沒有已記錄的拒絕；不代表已接受或已確認資格。">{row => <article className="grid min-w-0 gap-4">
      <h4 className="font-semibold">拒絕版本 {row.decision.revision_no}</h4><RejectionDetails record={row.decision} />
      <p className="break-all text-sm">{row.resolution ? `已記錄後繼解決版本 ${row.resolution.successor_revision_no}；修訂 ${row.resolution.successor_revision_id}；解決識別碼 ${row.resolution.id}。` : "未記錄後繼解決版本；不是撤回或實收沖銷。"}</p>
    </article>}</RecordList></section>;
}

function EventCard({ record, disabled, onRevise, onReject }: { record: Event; disabled: boolean; onRevise: (target: RevisionTarget) => void; onReject: (target: Event) => void }) {
  const desktop = useDesktop();
  const [open, setOpen] = useState(false);
  const [revisions, setRevisions] = useState(false);
  const [rejections, setRejections] = useState(false);
  return <article className="grid min-w-0 gap-4" aria-label={`股息事件 ${record.id}`}>
    <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><h2 className="font-semibold"><SecurityName id={record.security_id} /></h2>
      <p className="mt-2 break-all text-sm">除淨日 {record.ex_date}；每股 <span className="font-medium">{record.currency} {record.dps}</span></p></div>
      <span className="rounded-md border border-border px-2 py-1 text-sm">{statuses[record.workflow_status]}</span></div>
    {!desktop && <Button variant="outline" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? "收合事件與拒絕狀態" : "查看事件與拒絕狀態"}</Button>}
    {(desktop || open) && <div className="grid min-w-0 gap-5"><p className="text-sm leading-7 text-secondary">這是已記錄的事件決定，不是文件驗證、持倉資格、應收或實收。</p>
      <SourceValues values={[["穩定事件識別碼", record.id], ["本次清單的版本／實體修訂", `${record.version} / ${record.current_revision_id}`]]} />
      <EventTerms record={record} /><RejectionState record={record} disabled={disabled} onRevise={onRevise} onReject={onReject} />
      <div className="flex flex-wrap gap-3"><Button variant="outline" aria-expanded={revisions} onClick={() => setRevisions(value => !value)}>{revisions ? "收合事件修訂歷史" : "查看事件修訂歷史"}</Button>
        <Button variant="outline" aria-expanded={rejections} onClick={() => setRejections(value => !value)}>{rejections ? "收合拒絕歷史" : "查看拒絕歷史"}</Button></div>
      {revisions && <RevisionHistory record={record} />}{rejections && <RejectionHistory record={record} />}
      <Link to={`/settings/securities/${record.security_id}`} className="inline-flex min-h-11 items-center text-sm text-primary underline">核對穩定證券身分</Link>
    </div>}
  </article>;
}

function EventEditor({ onCancel, onSaved, target, rejectionTarget }: { onCancel: () => void; onSaved: (record: Event) => void; target?: RevisionTarget; rejectionTarget?: Event }) {
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [decisionBusy, setDecisionBusy] = useState(false);
  const title = rejectionTarget ? "拒絕股息事件版本" : target ? "修訂人工股息事件" : "新增人工股息事件";
  return <EditorSheet title={title} onClose={onCancel}>
    <div className="mb-6"><PasswordForm disabled={decisionBusy} onPendingChange={setPasswordBusy} /></div>
    {rejectionTarget ? <RejectEventForm target={rejectionTarget} onCancel={onCancel} onSaved={onSaved} disabled={passwordBusy} onPendingChange={setDecisionBusy} />
      : target ? <ManualEventRevisionForm target={target} onCancel={onCancel} onSaved={onSaved} disabled={passwordBusy} onPendingChange={setDecisionBusy} />
        : <ManualEventCreateForm onCancel={onCancel} onSaved={onSaved} disabled={passwordBusy} onPendingChange={setDecisionBusy} />}
  </EditorSheet>;
}

const initialFilters = { security_id: "", workflow_status: "" as "" | Event["workflow_status"], from_date: "", to_date: "" };
export function DividendEventsPage() {
  const cache = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [revisionTarget, setRevisionTarget] = useState<RevisionTarget | null>(null);
  const [rejectionTarget, setRejectionTarget] = useState<Event | null>(null);
  const [notice, setNotice] = useState<{ id: string; rejection: boolean } | null>(null);
  const page = usePageState("dividends:events");
  const [filters, setFilters] = useViewState("filters:dividends:events", initialFilters);
  const result = useQuery({ queryKey: ["dividends", "events", filters, page.q, page.cursor], staleTime: 0,
    queryFn: async ({ signal }) => checked(await api.GET("/api/v1/dividends/events", { params: { query: {
      security_id: filters.security_id || undefined, workflow_status: filters.workflow_status || undefined,
      from_date: filters.from_date || undefined, to_date: filters.to_date || undefined, q: page.q, cursor: page.cursor, limit: 25,
    } }, signal })),
  });
  function filter(change: Partial<typeof initialFilters>) { setFilters(previous => ({ ...previous, ...change })); page.search(page.q); }
  const editing = creating || !!revisionTarget || !!rejectionTarget;
  function closeEditor() { setCreating(false); setRevisionTarget(null); setRejectionTarget(null); void cache.invalidateQueries({ queryKey: ["dividends"] }); }
  return <div className="grid min-w-0 gap-6"><header className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-3xl"><h1 className="text-2xl font-semibold">股息事件</h1>
    <p className="mt-2 text-sm leading-7 text-secondary">查看已記錄的事件決定、來源與歷史；人工接受不是文件驗證或持倉資格，這裡不計算應收或實收。未知不會補零。</p>
    <Link to="/data-center/dividends" className="inline-flex min-h-11 items-center text-sm text-primary underline">查看股息來源觀測</Link></div>
    <Button data-editor-create disabled={editing} onClick={() => { setNotice(null); setCreating(true); }}>新增人工股息事件</Button></header>
    {notice && <p role="status" className="panel break-all text-sm leading-7">伺服器已回覆{notice.rejection ? "拒絕決定" : "事件決定"} {notice.id}；可能是原鍵回覆，請重新載入核對目前版本及拒絕狀態。這不是文件驗證、持倉資格或實收。</p>}
    {editing && <EventEditor target={revisionTarget ?? undefined} rejectionTarget={rejectionTarget ?? undefined} onCancel={closeEditor} onSaved={record => { setNotice({ id: record.id, rejection: !!rejectionTarget }); closeEditor(); }} />}
    <details className="rounded-lg border border-border bg-surface p-4"><summary className="flex min-h-11 cursor-pointer items-center font-medium">篩選股息事件</summary>
      <div className="mt-5 grid gap-5 sm:grid-cols-2 lg:grid-cols-3"><ReferenceSelect kind="securities" activeOnly={false} label="事件證券" value={filters.security_id} registration={{ onChange: event => filter({ security_id: event.target.value }) }} clearSelection={() => filter({ security_id: "" })} />
        <SelectField label="事件狀態" value={filters.workflow_status} onChange={event => filter({ workflow_status: event.target.value as typeof filters.workflow_status })}><option value="">全部事件狀態</option>{(Object.keys(statuses) as Event["workflow_status"][]).map(status => <option key={status} value={status}>{statuses[status]}</option>)}</SelectField>
        <Field label="事件除淨起日" type="date" value={filters.from_date} onChange={event => filter({ from_date: event.target.value })} />
        <Field label="事件除淨迄日" type="date" value={filters.to_date} onChange={event => filter({ to_date: event.target.value })} />
        <div><Button variant="outline" onClick={() => { setFilters(initialFilters); page.reset(); }}>清除事件篩選</Button></div>
      </div></details>
    <FilterChips values={filters} labels={{ security_id: "事件證券", workflow_status: "事件狀態", from_date: "事件除淨起日", to_date: "事件除淨迄日" }} onRemove={key => filter({ [key]: "" })} />
    <RecordList title="股息事件" page={page} result={result} filtered={Object.values(filters).some(Boolean)} emptyMessage="尚無可顯示的股息事件；這不是已確認沒有股息。請核對來源觀測及事件決定。" order={{ description: "除淨日／事件 ID 倒序", direction: "descending" }} columns={[
      { accessorKey: "ex_date", header: "除淨日" },
      { id: "security", header: "證券", cell: ({ row }) => <SecurityName id={row.original.security_id} /> },
      { id: "dps", header: "每股事件金額，非收入", cell: ({ row }) => `${row.original.currency} ${row.original.dps}` },
      { id: "status", header: "事件決定，非拒絕狀態", cell: ({ row }) => statuses[row.original.workflow_status] },
      { id: "pay", header: "付款日", cell: ({ row }) => row.original.pay_date ?? "未知" },
    ]}>{record => <EventCard record={record} disabled={editing} onRevise={target => { setNotice(null); setRevisionTarget(target); }} onReject={target => { setNotice(null); setRejectionTarget(target); }} />}</RecordList>
  </div>;
}
