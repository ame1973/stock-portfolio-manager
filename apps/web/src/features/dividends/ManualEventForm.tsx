import { useContext, useRef, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { date, optional, price, samePrice } from "../ledger/inputs";
import { SecurityName } from "../ledger/ReferenceName";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { useFormAction } from "../settings/data";
import { decisionCitation, decisionReason, decisionUrl, eventTypes, matchesDecisionReference, validTarget, type RevisionTarget } from "./event-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Input = Schemas["ManualEventCreate"];
type FormProps = { onSaved: (record: Event) => void; onCancel: () => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void };
const typeLabels: Record<typeof eventTypes[number], string> = { UNKNOWN: "未知，不推定一般股息", REGULAR: "一般股息", SPECIAL: "特別股息", INTERIM: "中期股息", FINAL: "末期股息", SUPPLEMENTAL: "補充股息", ETF_DISTRIBUTION: "ETF 分派", REIT_DISTRIBUTION: "REIT 分派" };
const schema = z.object({
  security_id: z.uuid("請選擇人工事件證券。"), event_type: z.enum(eventTypes), ex_date: date,
  announce_date: optional(date), record_date: optional(date), pay_date: optional(date),
  dps: price.max(50, "每股金額最多 50 個字元。"), currency: z.enum(["HKD", "USD"], { error: "請明確選擇事件股息幣別。" }),
  adjustment_basis: z.enum(["UNKNOWN", "AS_DECLARED", "SPLIT_ADJUSTED"]),
  citation: decisionCitation, source_url: decisionUrl, reason: decisionReason,
  acknowledged: z.boolean().refine(Boolean, "請先確認人工決定不證明文件或財務資格。"),
  confirm_unlock: z.boolean(), confirm_reopen: z.boolean(), confirm_rejection: z.boolean(), confirm_reference: z.boolean(),
});
function decisionSchema(target?: RevisionTarget) {
  return schema.superRefine((value, context) => {
    if (target?.event.manual_locked && !value.confirm_unlock) context.addIssue({ code: "custom", path: ["confirm_unlock"], message: "請確認解除人工鎖定。" });
    if (target?.event.workflow_status === "WITHDRAWN" && !value.confirm_reopen) context.addIssue({ code: "custom", path: ["confirm_reopen"], message: "請確認重新開啟已撤回事件。" });
    if (target?.rejection && !value.confirm_rejection) context.addIssue({ code: "custom", path: ["confirm_rejection"], message: "請確認明確處理所示拒絕。" });
    if (target && !value.confirm_reference) context.addIssue({ code: "custom", path: ["confirm_reference"], message: "請確認新版本使用本次填寫的引用。" });
  });
}

function checkedDecision(record: Event, body: Input, target?: RevisionTarget): Event {
  if (!record || typeof record !== "object") throw new Error("伺服器回覆與本次人工事件不符；請保留輸入並核對歷史，不會假定未儲存。");
  const identity = target ? record.id === target.event.id && record.version === target.event.version + 1 && record.current_revision_id !== target.event.current_revision_id : record.version === 1;
  const valid = z.uuid().safeParse(record.id).success && z.uuid().safeParse(record.current_revision_id).success
    && identity && record.security_id === body.security_id && record.event_type === body.event_type
    && record.ex_date === body.ex_date && record.announce_date === (body.announce_date ?? null)
    && record.record_date === (body.record_date ?? null) && record.pay_date === (body.pay_date ?? null)
    && samePrice(record.dps, body.dps) && record.currency === body.currency && record.adjustment_basis === body.adjustment_basis
    && record.workflow_status === (body.adjustment_basis === "AS_DECLARED" ? "PROVISIONAL" : "INCOMPLETE")
    && record.decision_kind === "MANUAL" && !record.manual_locked && !record.manual_override
    && record.candidate_revision_id === null && record.independent_origin_groups === null && record.assessment === null && Array.isArray(record.evidence) && record.evidence.length === 0
    && record.reason === body.reason && matchesDecisionReference(record.reference, body.citation, body.source_url ?? null)
    && record.scope === "OWNER_EVENT_DECISION_ONLY" && record.current_use_scope === "REVALIDATION_REQUIRED"
    && record.document_verification === "NOT_ESTABLISHED" && record.eligibility_scope === "NOT_ESTABLISHED";
  if (!valid) throw new Error("伺服器回覆與本次人工事件不符；請保留輸入並核對歷史，不會假定未儲存。");
  return record;
}

function ManualEventForm({ onSaved, onCancel, disabled = false, onPendingChange, observed }: FormProps & { observed?: RevisionTarget }) {
  const [target] = useState<RevisionTarget | undefined>(() => observed ? {
    event: { ...observed.event, reference: observed.event.reference ? { ...observed.event.reference } : null },
    rejection: observed.rejection ? { ...observed.rejection, reference: { ...observed.rejection.reference } } : null,
  } : undefined);
  const action = useFormAction(onSaved);
  const submitted = useRef<string | null>(null);
  const closeEditor = useContext(EditorCloseContext);
  const { register, control, setValue, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(decisionSchema(target)), defaultValues: {
      security_id: target?.event.security_id ?? "", event_type: target?.event.event_type ?? "UNKNOWN", ex_date: target?.event.ex_date ?? "",
      announce_date: target?.event.announce_date ?? "", record_date: target?.event.record_date ?? "", pay_date: target?.event.pay_date ?? "",
      dps: target?.event.dps ?? "", currency: target?.event.currency, adjustment_basis: target?.event.adjustment_basis ?? "UNKNOWN",
      citation: "", source_url: "", reason: "", acknowledged: false, confirm_unlock: false, confirm_reopen: false, confirm_rejection: false, confirm_reference: false,
    },
  });
  const securityId = useWatch({ control, name: "security_id" });
  return <FormFrame title={target ? "修訂人工股息事件" : "新增人工股息事件"} submitLabel={target ? "儲存人工修訂" : "儲存人工事件"} error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在儲存…"} onCancel={closeEditor ?? onCancel}
    onSubmit={event => {
      if (disabled) { event.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        const body: Input = {
          security_id: values.security_id, event_type: values.event_type, ex_date: values.ex_date,
          announce_date: values.announce_date || null, record_date: values.record_date || null, pay_date: values.pay_date || null,
          dps: values.dps, currency: values.currency, adjustment_basis: values.adjustment_basis,
          citation: values.citation, source_url: values.source_url || null, reason: values.reason,
        };
        const revision: Schemas["ManualEventRevision"] | undefined = target ? {
          ...body, revision_id: target.event.current_revision_id, resolve_rejection_id: target.rejection?.id ?? null,
          confirm_unlock: values.confirm_unlock, confirm_reopen: values.confirm_reopen,
        } : undefined;
        const intent = JSON.stringify({ version: target?.event.version ?? null, body: revision ?? body });
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (target && !validTarget(target)) throw new Error("固定的事件或拒絕範圍不符；請取消並重新載入核對。");
          const header = await mutationHeaders(key);
          const result = target && revision
            ? checked(await api.PATCH("/api/v1/dividends/events/{event_id}", { params: { path: { event_id: target.event.id }, header: { ...header, "If-Match": `"${target.event.version}"` } }, body: revision }))
            : checked(await api.POST("/api/v1/dividends/events", { params: { header }, body }));
          return checkedDecision(result, body, target);
        });
      })(event).finally(() => onPendingChange?.(false));
    }}>
    {target ? <div className="grid gap-3 text-sm leading-7 sm:col-span-2"><p>這次修訂固定使用開啟時的事件版本及實體識別碼；伺服器會在寫入時重新核對，不會改寫舊版本。</p>
      <p className="break-all">事件 {target.event.id}；版本 {target.event.version}；實體修訂 {target.event.current_revision_id}。{target.rejection && ` 已觀察拒絕 ${target.rejection.id}。`}</p>
      <p>先前引用的私密網址不會自動複製；新版本只使用本次重新填寫的引用。</p>
      <p>若已有應收或投影歷史，伺服器會拒絕修訂；不會改寫實收、現金或歷史匯率。</p>
      <div>固定證券：<SecurityName id={target.event.security_id} /><span className="break-all text-secondary">{target.event.security_id}</span></div>
    </div> : <p className="text-sm leading-7 text-secondary sm:col-span-2">每次明確新意圖都會建立不同的穩定事件，即使證券與除淨日相同；請先核對既有事件。這不抓取文件，也不建立持倉資格、應收或實收。</p>}
    <p className="text-sm leading-7 text-secondary sm:col-span-2">回應未確認時請保留視窗，以相同內容及操作識別碼重試；若關閉，請先查核事件歷史，不要假定未曾儲存。</p>
    {!target && <ReferenceSelect kind="securities" label="人工事件證券" activeOnly={false} value={securityId} registration={register("security_id")} error={errors.security_id?.message} clearSelection={() => setValue("security_id", "")} />}
    <SelectField label="事件類型" {...register("event_type")}>{eventTypes.map(value => <option key={value} value={value}>{typeLabels[value]}</option>)}</SelectField>
    <Field label="事件除淨日" type="date" {...register("ex_date")} error={errors.ex_date?.message} help="請提供事件當地日期，不以今天或擷取日代替。" />
    <Field label="事件每股金額" inputMode="decimal" maxLength={51} {...register("dps")} error={errors.dps?.message} help="明確金額，最多 8 位小數；零必須是明確已知值，不會用空白補零。" />
    <SelectField label="事件股息幣別" {...register("currency")} error={errors.currency?.message} help="必須由事件證據明確選擇，不以證券報價幣別或帳戶預設幣別代替。"><option value="">請明確選擇股息幣別</option><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>
    <SelectField label="事件金額口徑" {...register("adjustment_basis")}><option value="UNKNOWN">未知，待核對</option><option value="AS_DECLARED">人工稱宣告條款，不是文件驗證</option><option value="SPLIT_ADJUSTED">拆股調整後，待核對</option></SelectField>
    {([ ["announce_date", "事件宣告日（選填）"], ["record_date", "事件記錄日（選填）"], ["pay_date", "事件付款日（選填）"] ] as const).map(([key, label]) => <Field key={key} label={label} type="date" {...register(key)} error={errors[key]?.message} />)}
    <Field label="事件引用參考" maxLength={602} {...register("citation")} error={errors.citation?.message} help="填寫文件名稱及頁碼等依據；不代表系統已驗證文件。" />
    <Field label="事件引用網址（選填）" maxLength={4098} {...register("source_url")} error={errors.source_url?.message} help="不會抓取；含查詢或片段的連結在讀取畫面會隱藏。不要輸入帳密或金鑰。" />
    <Field label="事件決定依據" maxLength={2002} {...register("reason")} error={errors.reason?.message} help="至少 10 個字元；請說明這項人工決定。" />
    {target && <div className="grid gap-3 sm:col-span-2">
      {target.event.manual_locked && <div><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" {...register("confirm_unlock")} />我確認解除這個版本的人工鎖定，並建立新的人工修訂。</label>{errors.confirm_unlock && <p role="alert" className="text-sm text-danger">{errors.confirm_unlock.message}</p>}</div>}
      {target.event.workflow_status === "WITHDRAWN" && <div><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" {...register("confirm_reopen")} />我確認重新開啟已撤回的事件；舊撤回決定會保留。</label>{errors.confirm_reopen && <p role="alert" className="text-sm text-danger">{errors.confirm_reopen.message}</p>}</div>}
      {target.rejection && <div><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" {...register("confirm_rejection")} />我確認以此修訂明確處理所示拒絕；這不是文件驗證或收入確認。</label>{errors.confirm_rejection && <p role="alert" className="text-sm text-danger">{errors.confirm_rejection.message}</p>}</div>}
      <div><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" {...register("confirm_reference")} />我明白新版本只使用本次填寫的引用，舊引用不會自動複製。</label>{errors.confirm_reference && <p role="alert" className="text-sm text-danger">{errors.confirm_reference.message}</p>}</div>
    </div>}
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" {...register("acknowledged")} />我明白這只記錄人工決定，不證明文件、持倉資格或股息實收。</label>
      {errors.acknowledged && <p role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}

export function ManualEventCreateForm(props: FormProps) {
  return <ManualEventForm {...props} />;
}

export function ManualEventRevisionForm({ target, ...props }: FormProps & { target: RevisionTarget }) {
  return <ManualEventForm {...props} observed={target} />;
}
