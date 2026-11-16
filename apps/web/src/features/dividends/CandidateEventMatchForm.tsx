import { useContext, useId, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { SecurityName } from "../ledger/ReferenceName";
import { RecordList } from "../settings/RecordList";
import { useFormAction, usePageState } from "../settings/data";
import { candidateReason, checkedCandidateEvent, validCandidate, validCandidateForEvent } from "./candidate-validation";
import { basisLabels, eventTypeLabels, statuses, termLabels } from "./event-labels";
import { validCandidateEventTarget, type RevisionTarget } from "./event-validation";

type Schemas = components["schemas"];
type Candidate = Schemas["CandidateRecord"];
type Event = Schemas["DividendEventRecord"];
type Props = { target: RevisionTarget; onSaved: (record: Event) => void; onCancel: () => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void };

function candidatePage<T extends { items: Candidate[]; next_cursor?: string | null }>(page: T, security: string): T {
  const ids = new Set<string>(); const revisions = new Set<string>();
  if (!page || !Array.isArray(page.items) || page.items.length > 25
    || (page.next_cursor != null && typeof page.next_cursor !== "string") || page.items.some(record => {
      if (!validCandidate(record) || record.terms.security_id !== security || record.state !== "OPEN"
        || ids.has(record.id) || revisions.has(record.revision_id)) return true;
      ids.add(record.id); revisions.add(record.revision_id); return false;
    })) throw new Error("候選清單無法核對；請重新載入或檢查伺服器狀態及有效備份。");
  return page;
}

const schema = z.object({ reason: candidateReason, confirm_match: z.boolean(), confirm_rejection: z.boolean(), acknowledged: z.boolean() });
function decisionSchema(target: RevisionTarget) {
  return schema.superRefine((value, context) => {
    if (!value.confirm_match) context.addIssue({ code: "custom", path: ["confirm_match"], message: "請確認關聯所示候選與事件。" });
    if (target.rejection && !value.confirm_rejection) context.addIssue({ code: "custom", path: ["confirm_rejection"], message: "請確認明確處理所示拒絕。" });
    if (!value.acknowledged) context.addIssue({ code: "custom", path: ["acknowledged"], message: "請確認這不是文件驗證或財務資格。" });
  });
}

function MatchDecision({ target, candidate, onSaved, onCancel, disabled = false, onPendingChange }: Props & { candidate: Candidate }) {
  const action = useFormAction(onSaved);
  const submitted = useRef<string | null>(null);
  const closeEditor = useContext(EditorCloseContext);
  const confirmationId = useId();
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(decisionSchema(target)), defaultValues: { reason: "", confirm_match: false, confirm_rejection: false, acknowledged: false },
  });
  const terms = candidate.terms;
  return <FormFrame title="關聯候選至現有事件" submitLabel="記錄候選關聯" error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在記錄…"} onCancel={closeEditor ?? onCancel} onSubmit={event => {
      if (disabled) { event.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        const body: Schemas["CandidateEventRevision"] = { candidate_id: candidate.id, candidate_revision_id: candidate.revision_id,
          candidate_version: candidate.version, revision_id: target.event.current_revision_id, resolve_rejection_id: target.rejection?.id ?? null,
          confirmation: "MATCH_EXISTING_EVENT", reason: values.reason };
        const intent = JSON.stringify({ version: target.event.version, body });
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (!validCandidateEventTarget(target) || !validCandidateForEvent(candidate) || candidate.terms.security_id !== target.event.security_id)
            throw new Error("固定事件、拒絕或候選範圍無法核對；請關閉並重新載入。");
          const header = { ...await mutationHeaders(key), "If-Match": `"${target.event.version}"` };
          const result = checked(await api.POST("/api/v1/dividends/events/{event_id}/from-candidate", {
            params: { path: { event_id: target.event.id }, header }, body,
          }));
          return checkedCandidateEvent(result, candidate, values.reason, target.event);
        });
      })(event).finally(() => onPendingChange?.(false));
    }}>
    <div className="grid min-w-0 gap-3 text-sm leading-7 sm:col-span-2"><p>已選候選固定為開啟本決定時的實體修訂；這會在所示穩定事件新增版本，舊事件與候選歷史均保留。</p>
      <p className="break-all">候選 {candidate.id}；版本 {candidate.version}；實體修訂 {candidate.revision_id}。</p>
      <p>新候選條款：除淨日 {terms.ex_date}；每股 {terms.currency} {terms.dps}；事件類型 {eventTypeLabels[terms.event_type]}；金額口徑 {basisLabels[terms.adjustment_basis]}。</p>
      <p>其他日期：宣告 {terms.announce_date ?? "未知"}；記錄 {terms.record_date ?? "未知"}；付款 {terms.pay_date ?? "未知"}。來源比較 {candidate.assessment.status}，不是文件驗證或收入資格。</p>
      <p>伺服器會重新核對事件、拒絕、候選及來源基準。新版本不會複製舊事件引用，舊引用會保留在歷史。已有應收或投影歷史時將拒絕新版本；不會改寫實收、現金或歷史匯率。</p>
      <p>回應未確認時請保留視窗，以相同內容及操作識別碼重試；若關閉，請先核對事件歷史，不要假定未曾儲存。</p></div>
    <Field label="候選關聯依據" maxLength={2002} {...register("reason")} error={errors.reason?.message} help="至少 10 個字元；說明為何將此候選明確關聯至所示事件。" />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.confirm_match} aria-describedby={errors.confirm_match ? `${confirmationId}-match` : undefined} {...register("confirm_match")} />我確認將所示候選關聯至此事件，並以候選條款及來源建立新版本；舊引用保留在歷史。</label>
      {errors.confirm_match && <p id={`${confirmationId}-match`} role="alert" className="text-sm text-danger">{errors.confirm_match.message}</p>}</div>
    {target.rejection && <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.confirm_rejection} aria-describedby={errors.confirm_rejection ? `${confirmationId}-rejection` : undefined} {...register("confirm_rejection")} />我確認以此關聯明確處理所示拒絕；舊拒絕及其引用會保留。</label>
      {errors.confirm_rejection && <p id={`${confirmationId}-rejection`} role="alert" className="text-sm text-danger">{errors.confirm_rejection.message}</p>}</div>}
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.acknowledged} aria-describedby={errors.acknowledged ? `${confirmationId}-acknowledged` : undefined} {...register("acknowledged")} />我明白這不是文件驗證、持倉資格、應收或實收，也不改寫既有財務紀錄。</label>
      {errors.acknowledged && <p id={`${confirmationId}-acknowledged`} role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}

export function CandidateEventMatchForm({ target: observed, onSaved, onCancel, disabled = false, onPendingChange }: Props) {
  const [target] = useState(() => structuredClone(observed));
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const closeEditor = useContext(EditorCloseContext);
  const page = usePageState();
  const valid = validCandidateEventTarget(target);
  const security = valid ? target.event.security_id : "";
  const result = useQuery({ queryKey: ["dividends", "candidate-picker", security, page.cursor], staleTime: 0, enabled: valid && !candidate,
    queryFn: async ({ signal }) => candidatePage(checked(await api.GET("/api/v1/dividends/candidates", { params: { query: {
      security_id: security, state: "OPEN", cursor: page.cursor, limit: 25,
    } }, signal })), security),
  });
  if (!valid) return <p role="alert" className="text-sm leading-7 text-danger">固定事件或拒絕狀態無法核對、事件已鎖定或撤回；請關閉並重新載入。</p>;
  const event = target.event;
  const ready = !!result.data && !result.isFetching && !result.isError && !result.isPaused && !disabled;
  return <div className="grid min-w-0 gap-6"><section aria-label="固定現有事件" className="grid min-w-0 gap-3 text-sm leading-7">
    <h3 className="font-semibold">固定現有事件，不代表財務資格</h3>
    <p className="break-all">事件 {event.id}；版本 {event.version}；實體修訂 {event.current_revision_id}。{target.rejection && ` 已觀察拒絕 ${target.rejection.id}。`}</p>
    <div>固定證券：<SecurityName id={event.security_id} /><span className="break-all text-secondary">{event.security_id}</span></div>
    <p>目前條款：除淨日 {event.ex_date}；每股 {event.currency} {event.dps}；事件類型 {eventTypeLabels[event.event_type]}；金額口徑 {basisLabels[event.adjustment_basis]}；狀態 {statuses[event.workflow_status]}。</p>
    <p>其他日期：宣告 {event.announce_date ?? "未知"}；記錄 {event.record_date ?? "未知"}；付款 {event.pay_date ?? "未知"}。</p>
    {target.rejection && <p className="break-all">所示拒絕依據：{target.rejection.reason}；拒絕引用：{target.rejection.reference?.citation ?? "未提供"}。原引用網址不會複製。</p>}
    <p>事件、拒絕與候選分開讀取，不是同一快照；伺服器會重新核對各固定版本。</p></section>
    {candidate ? <MatchDecision target={target} candidate={candidate} onSaved={onSaved} onCancel={onCancel} disabled={disabled} onPendingChange={onPendingChange} />
      : <><p className="text-sm leading-7 text-secondary">請明確選擇同一證券的候選；候選回應標記不是目前或過往事件關聯查詢，不會自動合併。</p>
        <RecordList title="可選候選" page={page} result={result} searchable={false} emptyMessage="此頁沒有待審核候選；這不代表沒有來源或股息。">
          {record => <article aria-label={`可選候選 ${record.id}`} className="grid min-w-0 gap-3 text-sm leading-7">
            <p className="break-all">候選 {record.id}；版本 {record.version}；實體修訂 {record.revision_id}。</p>
            <p>除淨日 {record.terms.ex_date ?? "未知"}；每股 {record.terms.currency ?? "幣別未知"} {record.terms.dps ?? "未知"}；類型 {eventTypeLabels[record.terms.event_type]}；口徑 {basisLabels[record.terms.adjustment_basis]}；歷史比較 {record.assessment.status}。</p>
            <details className="rounded-md border border-border p-3"><summary className="flex min-h-11 cursor-pointer items-center font-medium">核對候選來源及歷史比較</summary>
              <div className="grid gap-3"><p>這些是已記錄的關聯與比較，不代表文件真偽或目前基準。</p>
                {record.matches.length === 0 ? <p>此版本未記錄來源關聯；不推定不存在來源。</p>
                  : <ul className="grid gap-3">{record.matches.map(match => <li key={match.observation_id} className="break-all">觀測 {match.observation_id}；原始來源雜湊 {match.source_sha256}；基準聲明 {match.basis_assertion_id ?? "未記錄"}；關聯依據 {match.reason}。</li>)}</ul>}
                <p>衝突欄位：{record.assessment.conflicting_fields.map(field => termLabels[field]).join("、") || "未列明"}；未完整原因：{record.assessment.incomplete_reasons.join("、") || "未列明"}。</p>
                {record.assessment.checks.length > 0 && <ul className="grid gap-3">{record.assessment.checks.map(check => <li key={check.observation_id} className="break-all">觀測 {check.observation_id}；比較金額 {check.comparison_dps ?? "未知"}；口徑 {basisLabels[check.comparison_basis]}；支持 {check.supporting_fields.map(field => termLabels[field]).join("、") || "未列明"}；衝突 {check.conflicting_fields.map(field => termLabels[field]).join("、") || "未列明"}；未完整原因 {check.incomplete_reasons.join("、") || "未列明"}。</li>)}</ul>}</div>
            </details>
            {validCandidateForEvent(record) ? <div><Button variant="outline" disabled={!ready} onClick={() => setCandidate(structuredClone(record))}>選擇此候選</Button></div>
              : <p className="text-secondary">候選欠缺明確除淨日、每股金額或幣別，或來源比較無法核對；目前不能關聯。</p>}
          </article>}
        </RecordList><div><Button variant="outline" onClick={closeEditor ?? onCancel}>取消</Button></div></>}
  </div>;
}
