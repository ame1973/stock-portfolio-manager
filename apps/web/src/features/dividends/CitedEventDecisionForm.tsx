import { useContext, useId, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { samePrice } from "../ledger/inputs";
import { SecurityName } from "../ledger/ReferenceName";
import { useFormAction } from "../settings/data";
import { basisLabels, eventTypeLabels, statuses } from "./event-labels";
import { decisionCitation, decisionReason, decisionUrl, matchesDecisionReference, validAcceptanceTarget, validCitedTarget, type RevisionTarget } from "./event-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Mode = "accept" | "withdraw";
type FormProps = { target: RevisionTarget; onSaved: (record: Event) => void; onCancel: () => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void };
const schema = z.object({
  citation: decisionCitation, evidence_url: decisionUrl, reason: decisionReason,
  acknowledged: z.boolean(), confirm_rejection: z.boolean(),
});
function decisionSchema(target: RevisionTarget, mode: Mode) {
  return schema.superRefine((value, context) => {
    if (!value.acknowledged) context.addIssue({ code: "custom", path: ["acknowledged"], message: mode === "accept" ? "請確認人工接受所示條款及其限制。" : "請確認撤回所示事件及其限制。" });
    if (target.rejection && !value.confirm_rejection) context.addIssue({ code: "custom", path: ["confirm_rejection"], message: "請確認明確處理所示拒絕。" });
  });
}

function checkedDecision(record: Event, target: RevisionTarget, citation: string, reason: string, source: string | null, mode: Mode): Event {
  const previous = target.event;
  const valid = !!record && typeof record === "object" && z.uuid().safeParse(record.id).success
    && z.uuid().safeParse(record.current_revision_id).success && record.id === previous.id
    && record.version === previous.version + 1 && record.current_revision_id !== previous.current_revision_id
    && record.security_id === previous.security_id && record.event_type === previous.event_type && record.ex_date === previous.ex_date
    && record.announce_date === previous.announce_date && record.record_date === previous.record_date && record.pay_date === previous.pay_date
    && samePrice(record.dps, previous.dps) && record.currency === previous.currency && record.adjustment_basis === previous.adjustment_basis
    && record.workflow_status === (mode === "accept" ? "VERIFIED" : "WITHDRAWN") && record.decision_kind === (mode === "accept" ? "VERIFY" : "WITHDRAW")
    && record.manual_locked === (mode === "accept" ? true : previous.manual_locked) && record.manual_override === false
    && record.candidate_revision_id === null && record.independent_origin_groups === null && record.assessment === null && Array.isArray(record.evidence) && record.evidence.length === 0
    && record.reason === reason && matchesDecisionReference(record.reference, citation, source)
    && record.scope === "OWNER_EVENT_DECISION_ONLY" && record.current_use_scope === "REVALIDATION_REQUIRED"
    && record.document_verification === "NOT_ESTABLISHED" && record.eligibility_scope === "NOT_ESTABLISHED";
  if (!valid) throw new Error(`伺服器回覆與本次${mode === "accept" ? "接受" : "撤回"}決定不符；請保留輸入並核對歷史，不會假定未儲存。`);
  return record;
}

function CitedEventDecisionForm({ target: observed, onSaved, onCancel, disabled = false, onPendingChange, mode }: FormProps & { mode: Mode }) {
  const [target] = useState(() => structuredClone(observed));
  const action = useFormAction(onSaved);
  const submitted = useRef<string | null>(null);
  const closeEditor = useContext(EditorCloseContext);
  const confirmationErrorId = useId();
  const rejectionErrorId = useId();
  const withdrawing = mode === "withdraw";
  const verb = withdrawing ? "撤回" : "接受";
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(decisionSchema(target, mode)), defaultValues: { citation: "", evidence_url: "", reason: "", acknowledged: false, confirm_rejection: false },
  });
  return <FormFrame title={withdrawing ? "撤回股息事件" : "人工接受股息事件版本"} submitLabel={withdrawing ? "記錄撤回決定" : "記錄人工接受"} submitClassName={withdrawing ? "bg-danger text-surface" : undefined}
    error={action.error} pending={isSubmitting || disabled} pendingLabel={disabled ? "等候密碼確認…" : "正在記錄…"} onCancel={closeEditor ?? onCancel}
    onSubmit={event => {
      if (disabled) { event.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        const source = values.evidence_url || null;
        const common = { revision_id: target.event.current_revision_id, resolve_rejection_id: target.rejection?.id ?? null, citation: values.citation, reason: values.reason };
        const acceptance: Schemas["AcceptDividendRevision"] = { ...common, decision: "ACCEPT_REVISION", evidence_url: source };
        const withdrawal: Schemas["DividendWithdrawal"] = { ...common, confirmation: "WITHDRAW_EVENT", source_url: source };
        const intent = JSON.stringify({ version: target.event.version, body: withdrawing ? withdrawal : acceptance });
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (!(withdrawing ? validCitedTarget(target) : validAcceptanceTarget(target))) throw new Error("固定的事件或拒絕範圍不符、已撤回或條款未明；請取消並重新載入核對。");
          const header = { ...await mutationHeaders(key), "If-Match": `"${target.event.version}"` };
          const result = withdrawing
            ? checked(await api.POST("/api/v1/dividends/events/{event_id}/withdraw", { params: { path: { event_id: target.event.id }, header }, body: withdrawal }))
            : checked(await api.POST("/api/v1/dividends/{event_id}/verify", { params: { path: { event_id: target.event.id }, header }, body: acceptance }));
          return checkedDecision(result, target, values.citation, values.reason, source, mode);
        });
      })(event).finally(() => onPendingChange?.(false));
    }}>
    <div className="grid gap-3 text-sm leading-7 sm:col-span-2"><p>{withdrawing
      ? "這次撤回固定開啟時的事件與實體版本；伺服器會重新核對拒絕狀態，以相同條款建立撤回版本並保留既有人工鎖定與歷史。"
      : "這次接受固定開啟時的事件與實體版本；伺服器會重新核對來源及拒絕狀態，並以相同條款建立人工鎖定的新版本。"}</p>
      <p className="break-all">事件 {target.event.id}；版本 {target.event.version}；實體修訂 {target.event.current_revision_id}。{target.rejection && ` 已觀察拒絕 ${target.rejection.id}。`}</p>
      <p>除淨日 {target.event.ex_date}；每股 {target.event.currency} {target.event.dps}；這是事件條款，不是收入。</p>
      <p>事件類型：{eventTypeLabels[target.event.event_type] ?? "無法辨識"}；金額口徑：{basisLabels[target.event.adjustment_basis] ?? "無法辨識"}；事件狀態：{statuses[target.event.workflow_status] ?? "無法辨識"}。</p>
      <p>其他日期：宣告：{target.event.announce_date ?? "未知"}；記錄：{target.event.record_date ?? "未知"}；付款：{target.event.pay_date ?? "未知"}。</p>
      <div>固定證券：<SecurityName id={target.event.security_id} /><span className="break-all text-secondary">{target.event.security_id}</span></div>
      {target.rejection && <p className="break-all">所示拒絕依據：{target.rejection.reason}；拒絕引用：{target.rejection.reference?.citation ?? "未提供"}。原引用網址不會複製。</p>}
      {withdrawing ? <p>撤回不會沖銷實收、撤銷現金或修改歷史匯率，也不證明文件已驗證或持倉有資格。新版本只使用本次填寫的引用，不會複製舊網址。</p>
        : <p>來源即使有衝突，人工接受也不代表文件已驗證、持倉有資格或已實收；新版本只使用本次填寫的引用，不會複製舊網址。</p>}
      <p>已有應收或投影歷史時，伺服器會阻止新決定；不會改寫實收、現金或歷史匯率。回應未確認時，請保留視窗以相同內容及操作識別碼重試；若關閉，請核對歷史，不要假定未曾記錄。</p></div>
    <Field label={`${verb}引用參考`} maxLength={602} {...register("citation")} error={errors.citation?.message} help="填寫文件名稱及頁碼等依據；不代表系統已驗證文件。" />
    <Field label={`${verb}引用網址（選填）`} maxLength={4098} {...register("evidence_url")} error={errors.evidence_url?.message} help="不會抓取；含查詢或片段的連結在讀取畫面會隱藏。不要輸入帳密或金鑰。" />
    <Field label={`${verb}決定依據`} maxLength={2002} {...register("reason")} error={errors.reason?.message} help={`至少 10 個字元；請說明${withdrawing ? "撤回此事件" : "接受此版本"}的原因。`} />
    {target.rejection && <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.confirm_rejection} aria-describedby={errors.confirm_rejection ? rejectionErrorId : undefined} {...register("confirm_rejection")} />我確認以本次{verb}決定明確處理所示拒絕；舊拒絕及其引用會保留。</label>
      {errors.confirm_rejection && <p id={rejectionErrorId} role="alert" className="text-sm text-danger">{errors.confirm_rejection.message}</p>}</div>}
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.acknowledged} aria-describedby={errors.acknowledged ? confirmationErrorId : undefined} {...register("acknowledged")} />{withdrawing ? "我確認撤回所示股息事件，明白這不會沖銷實收、撤銷現金或驗證文件。" : "我確認以人工決定接受所示事件條款，明白這不是文件驗證、持倉資格或股息實收。"}</label>
      {errors.acknowledged && <p id={confirmationErrorId} role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}

export function AcceptEventForm(props: FormProps) { return <CitedEventDecisionForm {...props} mode="accept" />; }
export function WithdrawEventForm(props: FormProps) { return <CitedEventDecisionForm {...props} mode="withdraw" />; }
