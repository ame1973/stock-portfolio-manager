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
import { refreshLedger } from "../ledger/data";
import { RecordList } from "../settings/RecordList";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { PasswordForm } from "../settings/SecuritySettingsPage";
import { usePageState } from "../settings/data";
import { SourceLink, SourceReadState, SourceValues } from "../sources/details";
import { CandidateEventMatchForm } from "./CandidateEventMatchForm";
import { DividendEstimatePanel } from "./DividendEstimatePanel";
import { DividendProjectionPanel } from "./DividendProjectionPanel";
import { DividendReceiptPanel } from "./DividendReceiptPanel";
import { ProjectionWriteForm } from "./ProjectionWriteForm";
import { ReceiptWriteForm } from "./ReceiptWriteForm";
import { ReceiptVoidForm } from "./ReceiptVoidForm";
import type { ProjectionTarget } from "./projection-input-validation";
import type { ReceiptTarget } from "./receipt-input-validation";
import type { ReceiptVoidTarget } from "./receipt-void-validation";
import { EstimateWriteForm, type EstimateTarget } from "./EstimateWriteForm";
import { AcceptEventForm, WithdrawEventForm } from "./CitedEventDecisionForm";
import { ManualEventCreateForm, ManualEventOverrideForm, ManualEventRevisionForm } from "./ManualEventForm";
import { RejectEventForm } from "./RejectEventForm";
import { basisLabels as basis, eventTypeLabels as eventTypes, statuses, termLabels } from "./event-labels";
import { validAcceptanceTarget, validCandidateEventTarget, validCitedTarget, validRejection, validTarget, type RevisionTarget } from "./event-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Revision = Schemas["DividendEventRevisionRecord"];
type Terms = Event | Revision;
type Rejection = Schemas["RejectionDecision"];
type EventEditorTarget = { mode: "create" } | { mode: "revise" | "accept" | "withdraw" | "override" | "match"; target: RevisionTarget } | { mode: "reject"; target: Event };
type Editor = EventEditorTarget | { mode: "estimate"; target: EstimateTarget } | { mode: "projection"; target: ProjectionTarget }
  | { mode: "receipt"; target: ReceiptTarget } | { mode: "receipt-void"; target: ReceiptVoidTarget };

const decisions: Record<Event["decision_kind"], string> = {
  CANDIDATE: "明確關聯候選資料", MANUAL: "人工條款", VERIFY: "人工接受", WITHDRAW: "人工撤回",
};
const names = (items: Schemas["EventEvidence"]["fields"]) => items.length ? items.map(item => termLabels[item]).join("、") : "未列明";

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

function RejectionState({ record, disabled, associationDisabled, onOpen }: { record: Event; disabled: boolean; associationDisabled: boolean; onOpen: (editor: Editor) => void }) {
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
    {ready && target && (validTarget(target) ? <div className="flex flex-wrap gap-3"><Button variant="outline" disabled={disabled} onClick={() => onOpen({ mode: "revise", target })}>修訂人工股息事件</Button>
      {validCandidateEventTarget(target) && <Button variant="outline" disabled={associationDisabled} onClick={() => onOpen({ mode: "match", target })}>關聯候選至此事件</Button>}
      {validAcceptanceTarget(target) && <Button variant="outline" disabled={disabled} onClick={() => onOpen({ mode: "accept", target })}>人工接受此版本</Button>}
      {target.rejection === null && <Button variant="outline" disabled={disabled} onClick={() => onOpen({ mode: "reject", target: record })}>拒絕此事件版本</Button>}
      {validCitedTarget(target) && <Button variant="outline" className="border-danger text-danger" disabled={disabled} onClick={() => onOpen({ mode: "withdraw", target })}>撤回此事件</Button>}
      <Button variant="outline" className="border-danger text-danger" disabled={disabled} onClick={() => onOpen({ mode: "override", target })}>人工覆寫此版本</Button></div> : <p role="alert" className="text-sm text-danger">事件或拒絕範圍無法核對，或版本已達上限；不能開始新決定，請重新載入。</p>)}
    {ready && target && validTarget(target) && !validCandidateEventTarget(target) && <p className="text-sm leading-7 text-secondary">事件已鎖定、撤回或條款無法核對；不能關聯候選。</p>}
    {ready && target && validTarget(target) && !validCitedTarget(target) && <p className="text-sm leading-7 text-secondary">事件已撤回或條款格式不符；不能開始撤回，請重新載入並核對。</p>}
    {ready && target && validTarget(target) && !validAcceptanceTarget(target) && <p className="text-sm leading-7 text-secondary">事件已撤回、條款格式不符或金額口徑未明確記錄為宣告條款；不能開始人工接受，請先核對事件與來源。</p>}
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

function EventCard({ record, disabled, associationDisabled, onOpen, estimateRefresh, projectionRefresh, receiptRefresh }: { record: Event; disabled: boolean; associationDisabled: boolean; onOpen: (editor: Editor) => void; estimateRefresh: number; projectionRefresh: number; receiptRefresh: number }) {
  const desktop = useDesktop();
  const [open, setOpen] = useState(false);
  const [revisions, setRevisions] = useState(false);
  const [rejections, setRejections] = useState(false);
  const [estimates, setEstimates] = useState(false);
  const [projections, setProjections] = useState(false);
  const [receipts, setReceipts] = useState(false);
  return <article className="grid min-w-0 gap-4" aria-label={`股息事件 ${record.id}`}>
    <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><h2 className="font-semibold"><SecurityName id={record.security_id} /></h2>
      <p className="mt-2 break-all text-sm">除淨日 {record.ex_date}；每股 <span className="font-medium">{record.currency} {record.dps}</span></p></div>
      <span className="rounded-md border border-border px-2 py-1 text-sm">{statuses[record.workflow_status]}</span></div>
    {!desktop && <Button variant="outline" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? "收合事件與拒絕狀態" : "查看事件與拒絕狀態"}</Button>}
    {(desktop || open) && <div className="grid min-w-0 gap-5"><p className="text-sm leading-7 text-secondary">這是已記錄的事件決定，不是文件驗證、持倉資格、應收或實收。</p>
      <SourceValues values={[["穩定事件識別碼", record.id], ["本次清單的版本／實體修訂", `${record.version} / ${record.current_revision_id}`]]} />
      <EventTerms record={record} /><RejectionState record={record} disabled={disabled} associationDisabled={associationDisabled} onOpen={onOpen} />
      <div className="flex flex-wrap gap-3"><Button variant="outline" aria-expanded={revisions} onClick={() => setRevisions(value => !value)}>{revisions ? "收合事件修訂歷史" : "查看事件修訂歷史"}</Button>
        <Button variant="outline" aria-expanded={rejections} onClick={() => setRejections(value => !value)}>{rejections ? "收合拒絕歷史" : "查看拒絕歷史"}</Button>
        <Button variant="outline" aria-expanded={estimates} onClick={() => setEstimates(value => !value)}>{estimates ? "收合帳戶稅費估算" : "查看帳戶稅費估算"}</Button>
        <Button variant="outline" aria-expanded={projections} onClick={() => setProjections(value => !value)}>{projections ? "收合帳戶投影歷史" : "查看帳戶投影歷史"}</Button>
        <Button variant="outline" aria-expanded={receipts} onClick={() => setReceipts(value => !value)}>{receipts ? "收合帳戶收款紀錄" : "查看帳戶收款紀錄"}</Button></div>
      {revisions && <RevisionHistory record={record} />}{rejections && <RejectionHistory record={record} />}
      {estimates && <DividendEstimatePanel key={`${record.id}:${record.current_revision_id}:${record.version}`} event={record} refresh={estimateRefresh} writeDisabled={associationDisabled} onWrite={target => onOpen({ mode: "estimate", target })} />}
      {projections && <DividendProjectionPanel key={`${record.id}:${record.current_revision_id}:${record.version}`} event={record} refresh={projectionRefresh} writeDisabled={associationDisabled} onWrite={target => onOpen({ mode: "projection", target })} />}
      {receipts && <DividendReceiptPanel key={`${record.id}:${record.current_revision_id}:${record.version}`} event={record} refresh={receiptRefresh} comparisonRefresh={projectionRefresh} writeDisabled={associationDisabled}
        onWrite={target => onOpen({ mode: "receipt", target })} onVoid={target => onOpen({ mode: "receipt-void", target })} />}
      <Link to={`/settings/securities/${record.security_id}`} className="inline-flex min-h-11 items-center text-sm text-primary underline">核對穩定證券身分</Link>
    </div>}
  </article>;
}

function EventEditor({ onCancel, onSaved, editor }: { onCancel: () => void; onSaved: (record: Event) => void; editor: EventEditorTarget }) {
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [decisionBusy, setDecisionBusy] = useState(false);
  const title = { create: "新增人工股息事件", revise: "修訂人工股息事件", reject: "拒絕股息事件版本", accept: "人工接受股息事件版本", withdraw: "撤回股息事件", override: "人工覆寫股息事件", match: "關聯候選至現有事件" }[editor.mode];
  const props = { onCancel, onSaved, disabled: passwordBusy, onPendingChange: setDecisionBusy };
  return <EditorSheet title={title} onClose={onCancel}>
    <div className="mb-6"><PasswordForm disabled={decisionBusy} onPendingChange={setPasswordBusy} /></div>
    {editor.mode === "match" ? <CandidateEventMatchForm target={editor.target} {...props} />
      : editor.mode === "reject" ? <RejectEventForm target={editor.target} {...props} />
      : editor.mode === "accept" ? <AcceptEventForm target={editor.target} {...props} />
        : editor.mode === "withdraw" ? <WithdrawEventForm target={editor.target} {...props} />
          : editor.mode === "override" ? <ManualEventOverrideForm target={editor.target} {...props} />
            : editor.mode === "revise" ? <ManualEventRevisionForm target={editor.target} {...props} />
              : <ManualEventCreateForm {...props} />}
  </EditorSheet>;
}

function EstimateEditor({ target, onCancel, onSaved }: { target: EstimateTarget; onCancel: () => void; onSaved: (id: string) => void }) {
  const [passwordBusy, setPasswordBusy] = useState(false); const [decisionBusy, setDecisionBusy] = useState(false);
  return <EditorSheet title={target.kind === "tax" ? "記錄稅率估算" : "記錄費用估算"} onClose={onCancel}>
    <div className="mb-6"><PasswordForm disabled={decisionBusy} onPendingChange={setPasswordBusy} /></div>
    <EstimateWriteForm target={target} onCancel={onCancel} onSaved={record => onSaved(record.id)} disabled={passwordBusy} onPendingChange={setDecisionBusy} />
  </EditorSheet>;
}

function ProjectionEditor({ target, onCancel, onSaved }: { target: ProjectionTarget; onCancel: () => void; onSaved: (id: string) => void }) {
  const [passwordBusy, setPasswordBusy] = useState(false); const [decisionBusy, setDecisionBusy] = useState(false);
  return <EditorSheet title="記錄股息投影" onClose={onCancel}>
    <div className="mb-6"><PasswordForm disabled={decisionBusy} onPendingChange={setPasswordBusy} /></div>
    <ProjectionWriteForm target={target} onCancel={onCancel} onSaved={record => onSaved(record.id)} disabled={passwordBusy} onPendingChange={setDecisionBusy} />
  </EditorSheet>;
}

function ReceiptEditor({ target, onCancel, onSaved }: { target: ReceiptTarget; onCancel: () => void; onSaved: (id: string) => void }) {
  const [passwordBusy, setPasswordBusy] = useState(false); const [decisionBusy, setDecisionBusy] = useState(false);
  return <EditorSheet title="記錄股息收款" onClose={onCancel}>
    <div className="mb-6"><PasswordForm disabled={decisionBusy} onPendingChange={setPasswordBusy} /></div>
    <ReceiptWriteForm target={target} onCancel={onCancel} onSaved={record => onSaved(record.id)} disabled={passwordBusy} onPendingChange={setDecisionBusy} />
  </EditorSheet>;
}

function ReceiptVoidEditor({ target, onCancel, onSaved }: { target: ReceiptVoidTarget; onCancel: () => void; onSaved: (id: string) => void }) {
  const [passwordBusy, setPasswordBusy] = useState(false); const [decisionBusy, setDecisionBusy] = useState(false);
  return <EditorSheet title="作廢股息收款" onClose={onCancel}>
    <div className="mb-6"><PasswordForm disabled={decisionBusy} onPendingChange={setPasswordBusy} /></div>
    <ReceiptVoidForm target={target} onCancel={onCancel} onSaved={record => onSaved(record.id)} disabled={passwordBusy} onPendingChange={setDecisionBusy} />
  </EditorSheet>;
}

const initialFilters = { security_id: "", workflow_status: "" as "" | Event["workflow_status"], from_date: "", to_date: "" };
export function DividendEventsPage() {
  const cache = useQueryClient();
  const [editor, setEditor] = useState<Editor | null>(null);
  const [estimateRefresh, setEstimateRefresh] = useState(0);
  const [projectionRefresh, setProjectionRefresh] = useState(0);
  const [receiptRefresh, setReceiptRefresh] = useState(0);
  const [notice, setNotice] = useState<{ id: string; mode: Editor["mode"] } | null>(null);
  const page = usePageState("dividends:events");
  const [filters, setFilters] = useViewState("filters:dividends:events", initialFilters);
  const result = useQuery({ queryKey: ["dividends", "events", filters, page.q, page.cursor], staleTime: 0,
    queryFn: async ({ signal }) => checked(await api.GET("/api/v1/dividends/events", { params: { query: {
      security_id: filters.security_id || undefined, workflow_status: filters.workflow_status || undefined,
      from_date: filters.from_date || undefined, to_date: filters.to_date || undefined, q: page.q, cursor: page.cursor, limit: 25,
    } }, signal })),
  });
  function filter(change: Partial<typeof initialFilters>) { setFilters(previous => ({ ...previous, ...change })); page.search(page.q); }
  const editing = editor !== null;
  const eventsReady = !!result.data && !result.isFetching && !result.isError && !result.isPaused;
  function closeEditor() {
    if (editor?.mode === "estimate") setEstimateRefresh(value => value + 1);
    if (editor?.mode === "projection") setProjectionRefresh(value => value + 1);
    if (editor?.mode === "receipt" || editor?.mode === "receipt-void") setReceiptRefresh(value => value + 1);
    setEditor(null); void cache.invalidateQueries({ queryKey: ["dividends"] });
  }
  return <div className="grid min-w-0 gap-6"><header className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-3xl"><h1 className="text-2xl font-semibold">股息事件</h1>
    <p className="mt-2 text-sm leading-7 text-secondary">查看已記錄的事件決定、來源與歷史；人工接受不是文件驗證或持倉資格，這裡不確認應收或實收。未知不會補零。</p>
    <div className="flex flex-wrap gap-x-5"><Link to="/dividends/candidates" className="inline-flex min-h-11 items-center text-sm text-primary underline">查看股息候選資料</Link>
      <Link to="/data-center/dividends" className="inline-flex min-h-11 items-center text-sm text-primary underline">查看股息來源觀測</Link></div></div>
    <Button data-editor-create disabled={editing} onClick={() => { setNotice(null); setEditor({ mode: "create" }); }}>新增人工股息事件</Button></header>
    {notice && <p role="status" className="panel break-all text-sm leading-7">伺服器已回覆{notice.mode === "receipt-void" ? "收款作廢記錄" : notice.mode === "receipt" ? "收款記錄" : notice.mode === "projection" ? "帳戶投影" : notice.mode === "estimate" ? "帳戶估算" : notice.mode === "reject" ? "拒絕決定" : notice.mode === "accept" ? "人工接受決定" : notice.mode === "withdraw" ? "撤回決定" : notice.mode === "override" ? "人工覆寫決定" : notice.mode === "match" ? "候選關聯決定" : "事件決定"} {notice.id}；可能是原鍵回覆，請重新載入核對{notice.mode === "receipt" || notice.mode === "receipt-void" ? "收款及現金歷史；這不是已確認付款、持倉資格或收入。" : notice.mode === "projection" ? "投影歷史及目前事件；這不是已確認持倉資格、應收或實收。" : notice.mode === "estimate" ? "估算歷史、事件版本及拒絕狀態；這不是已確認扣稅、扣費、持倉資格或實收。" : "目前版本及拒絕狀態；這不是文件驗證、持倉資格或實收。"}</p>}
    {editor && (editor.mode === "receipt-void" ? <ReceiptVoidEditor target={editor.target} onCancel={closeEditor} onSaved={id => { setNotice({ id, mode: "receipt-void" }); void refreshLedger(cache); closeEditor(); }} />
      : editor.mode === "receipt" ? <ReceiptEditor target={editor.target} onCancel={closeEditor} onSaved={id => { setNotice({ id, mode: "receipt" }); void refreshLedger(cache); closeEditor(); }} />
      : editor.mode === "projection" ? <ProjectionEditor target={editor.target} onCancel={closeEditor} onSaved={id => { setNotice({ id, mode: "projection" }); closeEditor(); }} />
      : editor.mode === "estimate" ? <EstimateEditor target={editor.target} onCancel={closeEditor} onSaved={id => { setNotice({ id, mode: "estimate" }); closeEditor(); }} />
        : <EventEditor editor={editor} onCancel={closeEditor} onSaved={record => { setNotice({ id: record.id, mode: editor.mode }); closeEditor(); }} />)}
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
    ]}>{record => <EventCard record={record} disabled={editing} associationDisabled={editing || !eventsReady} estimateRefresh={estimateRefresh} projectionRefresh={projectionRefresh} receiptRefresh={receiptRefresh} onOpen={value => { setNotice(null); setEditor(value); }} />}</RecordList>
  </div>;
}
