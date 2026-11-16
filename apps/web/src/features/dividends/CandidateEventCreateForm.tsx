import { useContext, useId, useRef, useState } from "react";
import { replaceEqualDeep } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Link } from "react-router";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { samePrice } from "../ledger/inputs";
import { SecurityName } from "../ledger/ReferenceName";
import { useFormAction } from "../settings/data";
import { candidateReason, validCandidateForEvent } from "./candidate-validation";
import { basisLabels, eventTypeLabels } from "./event-labels";

type Schemas = components["schemas"];
type Candidate = Schemas["CandidateRecord"];
type Event = Schemas["DividendEventRecord"];
type Props = { target: Candidate; onSaved: (record: Event) => void; onCancel: () => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void };
const schema = z.object({ reason: candidateReason,
  confirm_distinct: z.boolean().refine(Boolean, "請確認建立獨立事件。"),
  acknowledged: z.boolean().refine(Boolean, "請確認這不是文件驗證或財務資格。") });

function checkedDecision(record: Event, target: Candidate, reason: string): Event {
  const terms = target.terms;
  const evidence = target.assessment.checks.map(check => ({ observation_id: check.observation_id, fields: check.supporting_fields }));
  const valid = validCandidateForEvent(target) && record && typeof record === "object" && z.uuid().safeParse(record.id).success
    && z.uuid().safeParse(record.current_revision_id).success && record.version === 1 && record.security_id === terms.security_id
    && record.event_type === terms.event_type && record.ex_date === terms.ex_date && record.announce_date === (terms.announce_date ?? null)
    && record.record_date === (terms.record_date ?? null) && record.pay_date === (terms.pay_date ?? null)
    && terms.dps != null && samePrice(record.dps, terms.dps) && record.currency === terms.currency && record.adjustment_basis === terms.adjustment_basis
    && record.workflow_status === target.assessment.status && record.decision_kind === "CANDIDATE" && record.candidate_revision_id === target.revision_id
    && record.manual_locked === false && record.manual_override === false && record.reference === null && record.reason === reason
    && record.independent_origin_groups === target.assessment.independent_origins.length
    && replaceEqualDeep(target.assessment, record.assessment) === target.assessment && replaceEqualDeep(evidence, record.evidence) === evidence
    && record.scope === "OWNER_EVENT_DECISION_ONLY" && record.current_use_scope === "REVALIDATION_REQUIRED"
    && record.document_verification === "NOT_ESTABLISHED" && record.eligibility_scope === "NOT_ESTABLISHED";
  if (!valid) throw new Error("伺服器回覆與固定候選不符；請保留輸入並核對事件歷史，不會假定未儲存。");
  return record;
}

export function CandidateEventCreateForm({ target: observed, onSaved, onCancel, disabled = false, onPendingChange }: Props) {
  const [target] = useState(() => structuredClone(observed));
  const action = useFormAction(onSaved);
  const submitted = useRef<string | null>(null);
  const closeEditor = useContext(EditorCloseContext);
  const distinctId = useId(); const acknowledgmentId = useId();
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema), defaultValues: { reason: "", confirm_distinct: false, acknowledged: false },
  });
  if (!validCandidateForEvent(target)) return <p role="alert" className="text-sm leading-7 text-danger">固定候選無法核對、已排除、欠缺明確除淨日、每股金額或幣別，或來源比較不一致；請關閉並重新載入。</p>;
  const terms = target.terms;
  return <FormFrame title="由候選建立獨立股息事件" submitLabel="建立獨立事件" error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在建立…"} onCancel={closeEditor ?? onCancel} onSubmit={event => {
      if (disabled) { event.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        const body: Schemas["CandidateEventCreate"] = { candidate_id: target.id, candidate_revision_id: target.revision_id,
          candidate_version: target.version, confirmation: "CREATE_DISTINCT_EVENT", reason: values.reason };
        const intent = JSON.stringify(body);
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (!validCandidateForEvent(target)) throw new Error("固定候選無法核對；請關閉並重新載入。");
          const header = await mutationHeaders(key);
          const result = checked(await api.POST("/api/v1/dividends/events/from-candidate", { params: { header }, body }));
          return checkedDecision(result, target, values.reason);
        });
      })(event).finally(() => onPendingChange?.(false));
    }}>
    <div className="grid min-w-0 gap-3 text-sm leading-7 sm:col-span-2"><p>本次固定使用開啟時的候選與實體版本，建立新的穩定事件；伺服器會重新核對候選、來源及基準。這不是修訂現有事件。</p>
      <p className="break-all">候選 {target.id}；版本 {target.version}；實體修訂 {target.revision_id}。</p>
      <div>固定證券：<SecurityName id={terms.security_id} /><span className="break-all text-secondary">{terms.security_id}</span></div>
      <p>除淨日 {terms.ex_date}；每股 {terms.currency} {terms.dps}；事件類型 {eventTypeLabels[terms.event_type]}；金額口徑 {basisLabels[terms.adjustment_basis]}。</p>
      <p>其他日期：宣告 {terms.announce_date ?? "未知"}；記錄 {terms.record_date ?? "未知"}；付款 {terms.pay_date ?? "未知"}。來源比較 {target.assessment.status}，不是文件驗證或收入資格。</p>
      <p>候選回應標記不是目前或過往事件關聯查詢；請先核對既有事件。伺服器會阻止同一候選被關聯至另一事件，不會憑相同日期合併。</p>
      <Link to="/dividends" target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 items-center text-primary underline">另開分頁核對股息事件</Link>
      <p>回應未確認時請保留視窗，以相同內容及操作識別碼重試；若關閉，請先核對事件歷史，不要假定未曾儲存。</p></div>
    <Field label="獨立事件決定依據" maxLength={2002} {...register("reason")} error={errors.reason?.message} help="至少 10 個字元；說明為何明確建立獨立事件。" />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.confirm_distinct} aria-describedby={errors.confirm_distinct ? distinctId : undefined} {...register("confirm_distinct")} />我確認建立獨立股息事件，而不是關聯或覆寫現有事件。</label>
      {errors.confirm_distinct && <p id={distinctId} role="alert" className="text-sm text-danger">{errors.confirm_distinct.message}</p>}</div>
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.acknowledged} aria-describedby={errors.acknowledged ? acknowledgmentId : undefined} {...register("acknowledged")} />我明白這不是文件驗證、持倉資格、應收或實收，也不改寫既有財務紀錄。</label>
      {errors.acknowledged && <p id={acknowledgmentId} role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
