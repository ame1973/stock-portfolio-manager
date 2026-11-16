import { useContext, useId, useRef, useState } from "react";
import { useFieldArray, useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Link } from "react-router";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { date, optional, price, samePrice } from "../ledger/inputs";
import { SecurityName } from "../ledger/ReferenceName";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { useFormAction } from "../settings/data";
import { CandidateSourcePicker } from "./CandidateSourcePicker";
import { candidateReason, validCandidate } from "./candidate-validation";
import { eventTypeLabels } from "./event-labels";
import { eventTypes } from "./event-validation";

type Schemas = components["schemas"];
type Candidate = Schemas["CandidateRecord"];
type Input = Schemas["CandidateWrite"];
type Props = { onSaved: (record: Candidate) => void; onCancel: () => void };
const identifier = z.string().transform(value => value.trim().toLowerCase()).pipe(z.uuid("請填寫有效的識別碼。"));
const optionalIdentifier = z.string().transform(value => value.trim().toLowerCase()).pipe(z.union([z.literal(""), z.uuid("請填寫有效的基準聲明識別碼。")]));
const matchSchema = z.object({ observation_id: identifier, source_sha256: z.string().regex(/^[a-f0-9]{64}$/, "請填寫 64 位小寫來源雜湊。"),
  basis_assertion_id: optionalIdentifier, reason: candidateReason });
const schema = z.object({
  security_id: z.uuid("請選擇候選證券。"), state: z.enum(["OPEN", "DISMISSED"]), event_type: z.enum(eventTypes),
  ex_date: optional(date), announce_date: optional(date), record_date: optional(date), pay_date: optional(date),
  dps: optional(price.max(50, "每股金額最多 50 個字元。")), currency: z.enum(["", "HKD", "USD"]), adjustment_basis: z.enum(["UNKNOWN", "AS_DECLARED", "SPLIT_ADJUSTED"]),
  matches: z.array(matchSchema).max(100, "最多記錄 100 個來源關聯。"), change_reason: candidateReason,
  acknowledged: z.boolean(), confirm_empty: z.boolean(),
}).superRefine((value, context) => {
  if (!value.acknowledged) context.addIssue({ code: "custom", path: ["acknowledged"], message: "請先確認本次候選只作審閱。" });
  if (!value.matches.length && !value.confirm_empty) context.addIssue({ code: "custom", path: ["confirm_empty"], message: "請確認本次明確不列來源關聯。" });
  const ids = new Set<string>(); const basisIds = new Set<string>();
  value.matches.forEach((item, index) => {
    if (ids.has(item.observation_id)) context.addIssue({ code: "custom", path: ["matches", index, "observation_id"], message: "同一觀測不可重複關聯。" });
    ids.add(item.observation_id);
    if (item.basis_assertion_id && basisIds.has(item.basis_assertion_id)) context.addIssue({ code: "custom", path: ["matches", index, "basis_assertion_id"], message: "同一基準聲明不可重複關聯。" });
    if (item.basis_assertion_id) basisIds.add(item.basis_assertion_id);
  });
  const bytes = new TextEncoder().encode(JSON.stringify(value.matches.map(item => ({ ...item, basis_assertion_id: item.basis_assertion_id || null })))).length;
  if (bytes > 131_072) context.addIssue({ code: "custom", path: ["matches"], message: "來源關聯內容超過 128 KiB，請縮短依據或減少來源。" });
});

function sameOptionalPrice(left: string | null | undefined, right: string | null | undefined) {
  return left == null || right == null ? left == null && right == null : samePrice(left, right);
}
function priceIntent(value: string | null | undefined) {
  if (value == null) return null;
  const [whole, fraction = ""] = value.split(".");
  return `${whole!.replace(/^0+(?=\d)/, "")}.${fraction.padEnd(8, "0")}`;
}
function compareMatches(left: Schemas["CandidateMatch"][], right: Schemas["CandidateMatch"][]) {
  const sort = (items: Schemas["CandidateMatch"][]) => [...items].sort((a, b) => a.observation_id < b.observation_id ? -1 : a.observation_id > b.observation_id ? 1 : 0);
  const first = sort(left); const second = sort(right);
  return first.length === second.length && first.every((item, index) => {
    const expected = second[index]!;
    return item.observation_id === expected.observation_id && item.source_sha256 === expected.source_sha256
      && (item.basis_assertion_id ?? null) === (expected.basis_assertion_id ?? null) && item.reason === expected.reason;
  });
}
function checkedDecision(record: Candidate, body: Input, target?: Candidate): Candidate {
  const terms = body.terms;
  const valid = validCandidate(record) && (target ? record.id === target.id && record.version === target.version + 1 && record.revision_id !== target.revision_id : record.version === 1)
    && record.terms.security_id === terms.security_id && record.state === body.state && record.change_reason === body.change_reason
    && record.terms.event_type === terms.event_type && record.terms.adjustment_basis === terms.adjustment_basis
    && (record.terms.ex_date ?? null) === (terms.ex_date ?? null) && (record.terms.announce_date ?? null) === (terms.announce_date ?? null)
    && (record.terms.record_date ?? null) === (terms.record_date ?? null) && (record.terms.pay_date ?? null) === (terms.pay_date ?? null)
    && (record.terms.currency ?? null) === (terms.currency ?? null) && sameOptionalPrice(record.terms.dps, terms.dps)
    && compareMatches(record.matches, body.matches);
  if (!valid) throw new Error("伺服器回覆與本次候選決定不符；請保留輸入並核對歷史，不會假定未儲存。");
  return record;
}

function CandidateForm({ onSaved, onCancel, observed }: Props & { observed?: Candidate }) {
  const [target] = useState(() => observed ? structuredClone(observed) : undefined);
  const [showSources, setShowSources] = useState(false);
  const usable = !target || (validCandidate(target) && target.version < 2_147_483_647);
  const previous = usable ? target : undefined;
  const action = useFormAction(onSaved);
  const submitted = useRef<string | null>(null);
  const closeEditor = useContext(EditorCloseContext);
  const confirmationId = useId();
  const { register, control, setValue, getValues, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema), defaultValues: {
      security_id: previous?.terms.security_id ?? "", state: previous?.state ?? "OPEN", event_type: previous?.terms.event_type ?? "UNKNOWN",
      ex_date: previous?.terms.ex_date ?? "", announce_date: previous?.terms.announce_date ?? "", record_date: previous?.terms.record_date ?? "", pay_date: previous?.terms.pay_date ?? "",
      dps: previous?.terms.dps ?? "", currency: previous?.terms.currency ?? "", adjustment_basis: previous?.terms.adjustment_basis ?? "UNKNOWN",
      matches: previous?.matches.map(item => ({ ...item, basis_assertion_id: item.basis_assertion_id ?? "" })) ?? [],
      change_reason: "", acknowledged: false, confirm_empty: false,
    },
  });
  const { fields, append, remove } = useFieldArray({ control, name: "matches" });
  const securityId = useWatch({ control, name: "security_id" });
  const matches = useWatch({ control, name: "matches" });
  const selectedIds = matches.map(item => item.observation_id.trim().toLowerCase());
  const validSecurity = z.uuid().safeParse(securityId).success;
  if (!usable) return <p role="alert" className="text-sm leading-7 text-danger">固定候選範圍無法核對或版本已達上限；請關閉並重新載入。</p>;
  return <FormFrame title={target ? "修訂股息候選" : "新增股息候選"} submitLabel={target ? "儲存候選修訂" : "儲存候選資料"} error={action.error} pending={isSubmitting}
    onCancel={closeEditor ?? onCancel} onSubmit={event => void handleSubmit(values => {
      const matches = values.matches.map(item => ({ ...item, basis_assertion_id: item.basis_assertion_id || null }))
        .sort((a, b) => a.observation_id < b.observation_id ? -1 : a.observation_id > b.observation_id ? 1 : 0);
      const body: Input = { state: values.state, terms: { security_id: values.security_id, event_type: values.event_type,
        ex_date: values.ex_date || null, announce_date: values.announce_date || null, record_date: values.record_date || null, pay_date: values.pay_date || null,
        dps: values.dps || null, currency: values.currency || null, adjustment_basis: values.adjustment_basis }, matches, change_reason: values.change_reason };
      // Compare exact normalized values for idempotency; send the user's original decimal string.
      const intent = JSON.stringify({ version: target?.version ?? null, body: { ...body, terms: { ...body.terms, dps: priceIntent(body.terms.dps) } } });
      if (submitted.current !== null && submitted.current !== intent) action.changed();
      submitted.current = intent;
      return action.run(async key => {
        if (target && (!validCandidate(target) || target.version >= 2_147_483_647)) throw new Error("固定候選範圍不符；請關閉並重新載入。");
        const header = await mutationHeaders(key);
        const result = target
          ? checked(await api.PATCH("/api/v1/dividends/candidates/{id}", { params: { path: { id: target.id }, header: { ...header, "If-Match": `"${target.version}"` } }, body }))
          : checked(await api.POST("/api/v1/dividends/candidates", { params: { header }, body }));
        return checkedDecision(result, body, target);
      });
    })(event)}>
    {target ? <div className="grid min-w-0 gap-2 text-sm leading-7 sm:col-span-2"><p>本次修訂固定顯示開啟時的候選與實體修訂；伺服器以所示版本檢查並追加紀錄，不改寫歷史。</p>
      <p className="break-all">候選 {target.id}；版本 {target.version}；實體修訂 {target.revision_id}。</p>
      <p>原有來源與基準識別碼只代表已記錄歷史，不保證目前有效；請逐項核對，必要時移除或修改。已失效的基準可能阻擋儲存。</p>
      <div>固定證券：<SecurityName id={target.terms.security_id} /><span className="break-all text-secondary">{target.terms.security_id}</span></div></div>
      : <p className="text-sm leading-7 text-secondary sm:col-span-2">每次明確新增都建立獨立候選，即使同一證券或除淨日相同。候選不是股息事件、文件驗證、持倉資格或收入。</p>}
    <p className="text-sm leading-7 text-secondary sm:col-span-2">回應未確認時請保留視窗，以相同內容及操作識別碼重試；關閉前先核對歷史，不要假定未曾儲存。</p>
    {!target && <ReferenceSelect kind="securities" label="候選證券" activeOnly={false} value={securityId} registration={register("security_id")} error={errors.security_id?.message} clearSelection={() => setValue("security_id", "")} />}
    <SelectField label="候選狀態" {...register("state")}><option value="OPEN">待審核</option><option value="DISMISSED">標記排除，保留歷史</option></SelectField>
    <SelectField label="候選事件類型" {...register("event_type")}>{eventTypes.map(type => <option key={type} value={type}>{eventTypeLabels[type]}</option>)}</SelectField>
    <Field label="候選除淨日（選填）" type="date" {...register("ex_date")} error={errors.ex_date?.message} help="留空是未知，不會用今天或擷取日代替。" />
    <Field label="候選每股金額（選填）" inputMode="decimal" maxLength={51} {...register("dps")} error={errors.dps?.message} help="留空是未知；只有明確填入零才記錄零。最多 8 位小數。" />
    <SelectField label="候選股息幣別" {...register("currency")} help="未知時留空；不從證券報價或帳戶幣別推定。"><option value="">未知</option><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>
    <SelectField label="候選金額口徑" {...register("adjustment_basis")}><option value="UNKNOWN">未知，待核對</option><option value="AS_DECLARED">記錄為宣告口徑，非文件驗證</option><option value="SPLIT_ADJUSTED">拆股調整後，待核對</option></SelectField>
    {([ ["announce_date", "候選宣告日（選填）"], ["record_date", "候選記錄日（選填）"], ["pay_date", "候選付款日（選填）"] ] as const).map(([key, label]) => <Field key={key} label={label} type="date" {...register(key)} error={errors[key]?.message} />)}
    <section aria-label="來源關聯輸入" className="grid min-w-0 gap-4 rounded-md border border-border p-4 sm:col-span-2"><h4 className="font-medium">明確來源關聯</h4>
      <p className="text-sm leading-7 text-secondary">可瀏覽同一證券的已記錄來源，或手動輸入原始觀測識別碼及雜湊；每項都須自行說明關聯依據。伺服器重新核對來源與基準，不會根據相同日期自動合併或驗證文件。</p>
      <Link to="/data-center/dividends" target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 items-center text-sm text-primary underline">另開分頁核對來源觀測</Link>
      <div><Button type="button" variant="outline" disabled={!validSecurity} aria-expanded={showSources && validSecurity} onClick={() => setShowSources(value => !value)}>{showSources && validSecurity ? "收合來源觀測" : "瀏覽來源觀測"}</Button></div>
      {showSources && validSecurity && <CandidateSourcePicker key={securityId} securityId={securityId} selectedIds={selectedIds} full={fields.length >= 100} onSelect={pin => {
        const current = getValues("matches");
        if (getValues("security_id") !== securityId || current.length >= 100 || current.some(item => item.observation_id.trim().toLowerCase() === pin.observation_id.toLowerCase())) return;
        append({ ...pin, basis_assertion_id: "", reason: "" });
      }} />}
      {fields.map((field, index) => <fieldset key={field.id} className="grid min-w-0 gap-4 rounded-md border border-border p-4 sm:grid-cols-2"><legend className="px-1 text-sm font-medium">來源 {index + 1}</legend>
        <Field label={`來源 ${index + 1} 觀測識別碼`} maxLength={64} {...register(`matches.${index}.observation_id`)} error={errors.matches?.[index]?.observation_id?.message} />
        <Field label={`來源 ${index + 1} 原始雜湊`} maxLength={64} {...register(`matches.${index}.source_sha256`)} error={errors.matches?.[index]?.source_sha256?.message} />
        <Field label={`來源 ${index + 1} 基準聲明識別碼（選填）`} maxLength={64} {...register(`matches.${index}.basis_assertion_id`)} error={errors.matches?.[index]?.basis_assertion_id?.message} />
        <Field label={`來源 ${index + 1} 關聯依據`} maxLength={2002} {...register(`matches.${index}.reason`)} error={errors.matches?.[index]?.reason?.message} />
        <div className="sm:col-span-2"><Button type="button" variant="outline" onClick={() => { if (fields.length === 1) setValue("confirm_empty", false); remove(index); }} aria-label={`移除來源 ${index + 1} 關聯`}>移除此來源關聯</Button></div></fieldset>)}
      {errors.matches?.root?.message && <p role="alert" className="text-sm text-danger">{errors.matches.root.message}</p>}
      <div><Button type="button" variant="outline" disabled={fields.length >= 100} onClick={() => append({ observation_id: "", source_sha256: "", basis_assertion_id: "", reason: "" })}>新增來源關聯</Button></div>
      <p className="text-sm text-secondary">目前列出 {fields.length} / 100 個關聯；不會自動加入來源。</p>
      {fields.length === 0 && <div><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.confirm_empty} aria-describedby={errors.confirm_empty ? `${confirmationId}-empty` : undefined} {...register("confirm_empty")} />我確認本次明確不列來源關聯；這不代表沒有來源。</label>
        {errors.confirm_empty && <p id={`${confirmationId}-empty`} role="alert" className="text-sm text-danger">{errors.confirm_empty.message}</p>}</div>}
    </section>
    <Field label="候選變更依據" maxLength={2002} {...register("change_reason")} error={errors.change_reason?.message} help="至少 10 個字元，請說明本次審閱決定。" />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.acknowledged} aria-describedby={errors.acknowledged ? `${confirmationId}-acknowledged` : undefined} {...register("acknowledged")} />我已核對本次候選條款與來源關聯，明白這只是審閱，不證明文件、事件關聯、持倉資格或實收。</label>
      {errors.acknowledged && <p id={`${confirmationId}-acknowledged`} role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}

export function CandidateCreateForm(props: Props) { return <CandidateForm {...props} />; }
export function CandidateRevisionForm({ target, ...props }: Props & { target: Candidate }) { return <CandidateForm {...props} observed={target} />; }
