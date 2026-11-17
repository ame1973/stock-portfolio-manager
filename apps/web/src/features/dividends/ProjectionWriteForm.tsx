import { useContext, useId, useRef, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { fx } from "../ledger/inputs";
import { useFormAction } from "../settings/data";
import { normalizeDecisionText } from "./event-validation";
import { checkedProjectionSaved, fxIntent, validProjectionTarget, type ProjectionTarget } from "./projection-input-validation";

type Schemas = components["schemas"];
const text = z.string().transform(normalizeDecisionText);
const validText = (value: string, min: number, max: number) => {
  const characters = Array.from(value);
  return characters.length >= min && characters.length <= max && !value.includes("\0")
    && characters.every(character => { const point = character.codePointAt(0)!; return point < 0xd800 || point > 0xdfff; });
};
const schema = z.object({ hkdMode: z.enum(["", "UNKNOWN", "PROVIDED"]), hkdRate: z.string(), hkdSource: text,
  feeMode: z.enum(["", "UNKNOWN", "PROVIDED"]), feeRate: z.string(), feeSource: text, reason: text,
  acknowledged: z.boolean(), fence: z.boolean() });
function formSchema(target: ProjectionTarget) {
  const hkdChoice = target.event.currency !== "HKD";
  const feeCurrency = target.fee.selected?.fee_currency ?? null;
  const feeChoice = feeCurrency !== null && feeCurrency !== target.event.currency;
  return schema.superRefine((value, context) => {
    if (!validText(value.reason, 10, 1000)) context.addIssue({ code: "custom", path: ["reason"], message: "請提供 10 至 1000 個有效字元的依據。" });
    if (!value.acknowledged) context.addIssue({ code: "custom", path: ["acknowledged"], message: "請確認投影不是財務資格或實收。" });
    if (!value.fence) context.addIssue({ code: "custom", path: ["fence"], message: "請明確確認目前的事件修訂限制。" });
    if (hkdChoice && !value.hkdMode) context.addIssue({ code: "custom", path: ["hkdMode"], message: "請明確選擇是否提供港幣匯率。" });
    if (feeChoice && !value.feeMode) context.addIssue({ code: "custom", path: ["feeMode"], message: "請明確選擇是否提供費用換算率。" });
    for (const [selected, rate, source, ratePath, sourcePath] of [
      [hkdChoice && value.hkdMode === "PROVIDED", value.hkdRate, value.hkdSource, "hkdRate", "hkdSource"],
      [feeChoice && value.feeMode === "PROVIDED", value.feeRate, value.feeSource, "feeRate", "feeSource"],
    ] as const) {
      if (!selected) continue;
      if (!fx.max(50).safeParse(rate).success) context.addIssue({ code: "custom", path: [ratePath], message: "請輸入有效正數，最多 12 位小數，不會自動捨入。" });
      if (!validText(source, 1, 100) || source === "IDENTITY") context.addIssue({ code: "custom", path: [sourcePath], message: "請提供 1 至 100 個有效字元的實際匯率來源，不能使用 IDENTITY。" });
    }
  });
}

export function ProjectionWriteForm({ target: observed, onSaved, onCancel, disabled = false, onPendingChange }: {
  target: ProjectionTarget; onSaved: (record: Schemas["ProjectionRecord"]) => void; onCancel: () => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void;
}) {
  const [target] = useState(() => structuredClone(observed)); const action = useFormAction(onSaved); const submitted = useRef<string | null>(null);
  const close = useContext(EditorCloseContext); const acknowledgmentId = useId(); const fenceId = useId(); const valid = validProjectionTarget(target);
  const { register, control, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({ resolver: zodResolver(valid ? formSchema(target) : schema),
    defaultValues: { hkdMode: "", hkdRate: "", hkdSource: "", feeMode: "", feeRate: "", feeSource: "", reason: "", acknowledged: false, fence: false } });
  const hkdMode = useWatch({ control, name: "hkdMode" }); const feeMode = useWatch({ control, name: "feeMode" });
  if (!valid) return <p role="alert" className="text-sm text-danger">固定投影範圍無法安全核對；請關閉並重新載入事件、帳戶及輸入。</p>;
  const { event, account, inputs, tax, fee, query } = target;
  const feeCurrency = fee.selected?.fee_currency ?? null; const hkdChoice = event.currency !== "HKD";
  const feeChoice = feeCurrency !== null && feeCurrency !== event.currency;
  return <FormFrame title="記錄股息投影" submitLabel="記錄投影" error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在記錄…"} onCancel={close ?? onCancel} onSubmit={submit => {
      if (disabled) { submit.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        const body: Schemas["ProjectionWrite"] = { revision_id: inputs.revision_id, portfolio_id: inputs.portfolio_id, broker_id: inputs.broker_id,
          account_version: inputs.account_version, broker_version: inputs.broker_version, expected_data_revision: inputs.portfolio_data_revision,
          expected_projection_no: inputs.projection_head_no, expected_projection_id: inputs.projection_head_id, as_of: query.as_of, eligibility_rule: query.eligibility_rule,
          tax_effective_on: inputs.tax.effective_on, expected_tax_head_no: inputs.tax.head_no, expected_tax_head_id: inputs.tax.head_id, expected_tax_selected_id: inputs.tax.selected_id,
          fee_effective_on: inputs.fee.effective_on, expected_fee_head_no: inputs.fee.head_no, expected_fee_head_id: inputs.fee.head_id, expected_fee_selected_id: inputs.fee.selected_id,
          fx_rate_hkd: hkdChoice && values.hkdMode === "PROVIDED" ? values.hkdRate : null, fx_source: hkdChoice && values.hkdMode === "PROVIDED" ? values.hkdSource : null,
          fee_rate_event: feeChoice && values.feeMode === "PROVIDED" ? values.feeRate : null, fee_rate_source: feeChoice && values.feeMode === "PROVIDED" ? values.feeSource : null,
          confirmation: "RECORDED_PROJECTION_ONLY", confirm_revision_fence: true, reason: values.reason };
        // Decimal-equivalent spellings preserve the original uncertain operation key.
        const intent = JSON.stringify({ version: event.version, body: { ...body, fx_rate_hkd: fxIntent(body.fx_rate_hkd), fee_rate_event: fxIntent(body.fee_rate_event) } });
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (!validProjectionTarget(target)) throw new Error("固定投影範圍無法安全核對；請關閉並重新載入。");
          const header = { ...await mutationHeaders(key), "If-Match": `"${event.version}"` };
          const result = checked(await api.POST("/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections", {
            params: { path: { event_id: event.id, account_id: account.id }, header }, body,
          }));
          return checkedProjectionSaved(result, target, body);
        });
      })(submit).finally(() => onPendingChange?.(false));
    }}>
    <div className="grid min-w-0 gap-2 text-sm leading-7 text-secondary sm:col-span-2">
      <p>本次固定已核對的事件及帳戶。伺服器會在寫入時重新核對來源、拒絕狀態、持倉記錄、稅費及版本；它不會確認實際持有或收入。</p>
      <p className="break-all">事件 {event.id}；版本 {event.version}／{inputs.revision_id}；除淨日 {event.ex_date}；每股 {event.currency} {event.dps}；口徑 {event.adjustment_basis}；狀態 {event.workflow_status}。</p>
      <p className="break-all">帳戶 {account.name}／{account.id}；組合 {inputs.portfolio_id}；券商 {inputs.broker_id}；帳戶版本 {inputs.account_version}；券商版本 {inputs.broker_version}；組合資料版本 {inputs.portfolio_data_revision}。</p>
      <p className="break-all">查閱日 {query.as_of}；規則 {query.eligibility_rule === "ORDINARY_PRE_EX_DATE" ? "一般除淨日前記錄" : "需要另行核對"}；投影頂端 {inputs.projection_head_no}／{inputs.projection_head_id ?? "沒有"}。</p>
      <p className="break-all">稅率生效日 {inputs.tax.effective_on}；{tax.tax_status === "MISSING" ? "未設定，不是零" : tax.tax_status === "UNKNOWN" ? "未知，不是零" : `估算 ${tax.selected?.rate}，不是已確認扣稅`}；頂端 {inputs.tax.head_no}／{inputs.tax.head_id ?? "沒有"}；選定 {inputs.tax.selected_id ?? "沒有"}。</p>
      <p className="break-all">費用生效日 {inputs.fee.effective_on}；{fee.fee_status === "MISSING" ? "未設定，不是零" : fee.fee_status === "UNKNOWN" ? `金額未知，幣別 ${feeCurrency ?? "未知"}` : `估算 ${feeCurrency} ${fee.selected?.fee}，不是已確認扣費`}；頂端 {inputs.fee.head_no}／{inputs.fee.head_id ?? "沒有"}；選定 {inputs.fee.selected_id ?? "沒有"}。</p>
      <p>若回應未確認，請保留視窗並以相同內容重試；成功回覆可能是歷史重播。關閉後請核對歷史，不要假定未曾記錄。</p>
      <p className="font-semibold text-danger">重要：任何已記錄投影，即使需要核對或沒有已記錄持倉，目前都會阻止此事件的修訂、撤回及拒絕。紀錄不能刪除，限制要待原子對帳功能可用後才能處理。</p>
    </div>
    {hkdChoice ? <><SelectField label="港幣估值匯率" {...register("hkdMode")} error={errors.hkdMode?.message} help="不提供表示未知，不會套用預設匯率。">
      <option value="">請選擇</option><option value="UNKNOWN">不提供，匯率未知</option><option value="PROVIDED">明確輸入匯率及來源</option></SelectField>
      {hkdMode === "PROVIDED" && <><Field label={`${event.currency} 兌 HKD 匯率`} inputMode="decimal" maxLength={51} {...register("hkdRate")} error={errors.hkdRate?.message} help={`1 ${event.currency} 換算多少 HKD；請輸入實際依據。`} />
        <Field label="港幣匯率來源" maxLength={202} {...register("hkdSource")} error={errors.hkdSource?.message} /></>}</>
      : <p className="text-sm leading-7 text-secondary sm:col-span-2">事件幣別是 HKD；港幣換算使用同幣別恆等匯率 1，不從外部推定匯率。</p>}
    {feeChoice ? <><SelectField label="費用兌事件幣別匯率" {...register("feeMode")} error={errors.feeMode?.message} help="此匯率與港幣估值匯率獨立；不提供表示未知。">
      <option value="">請選擇</option><option value="UNKNOWN">不提供，匯率未知</option><option value="PROVIDED">明確輸入匯率及來源</option></SelectField>
      {feeMode === "PROVIDED" && <><Field label={`${feeCurrency} 兌 ${event.currency} 匯率`} inputMode="decimal" maxLength={51} {...register("feeRate")} error={errors.feeRate?.message} help={`1 ${feeCurrency} 換算多少 ${event.currency}；與港幣估值匯率分開輸入。`} />
        <Field label="費用匯率來源" maxLength={202} {...register("feeSource")} error={errors.feeSource?.message} /></>}</>
      : <p className="text-sm leading-7 text-secondary sm:col-span-2">{feeCurrency === null ? "費用幣別未知或未設定；不會推定費用換算率。" : `費用與事件同為 ${event.currency}；換算使用恆等匯率 1。`}</p>}
    <Field label="投影依據" maxLength={2002} {...register("reason")} error={errors.reason?.message} help="至少 10 個字元；不要輸入密碼或金鑰。" />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.acknowledged} aria-describedby={errors.acknowledged ? acknowledgmentId : undefined} {...register("acknowledged")} />我明白這只是未驗證的記錄型投影，不是已確認持倉資格、應收、收入或實收。</label>
      {errors.acknowledged && <p id={acknowledgmentId} role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.fence} aria-describedby={errors.fence ? fenceId : undefined} {...register("fence")} />我明白記錄投影後，目前無法修訂、撤回或拒絕此事件，且不能刪除紀錄來解除限制；原子對帳尚未完成。</label>
      {errors.fence && <p id={fenceId} role="alert" className="text-sm text-danger">{errors.fence.message}</p>}</div>
  </FormFrame>;
}
