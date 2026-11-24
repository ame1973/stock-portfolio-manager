import { useContext, useId, useRef, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { date, fx, money } from "../ledger/inputs";
import { useFormAction } from "../settings/data";
import { normalizeDecisionText, validDecisionText } from "./event-validation";
import { fxIntent } from "./projection-input-validation";
import { checkedReceiptSaved, receiptMoneyIntent, reversibleMoney, validReceiptTarget, type ReceiptTarget } from "./receipt-input-validation";

type Schemas = components["schemas"];
const text = z.string().transform(normalizeDecisionText);
const schema = z.object({ receivedDate: date, currency: z.enum(["", "HKD", "USD"]), received: z.string(),
  taxMode: z.enum(["", "UNKNOWN", "PROVIDED"]), tax: z.string(), feeMode: z.enum(["", "UNKNOWN", "PROVIDED"]), fee: z.string(),
  fxMode: z.enum(["", "UNKNOWN", "PROVIDED"]), fxRate: z.string(), fxSource: text, override: z.boolean(), actualHkd: z.string(), actualReason: text,
  note: z.string(), reason: text, acknowledged: z.boolean() });
const formSchema = schema.superRefine((value, context) => {
  if (!value.currency) context.addIssue({ code: "custom", path: ["currency"], message: "請明確選擇實收幣別。" });
  if (!reversibleMoney.safeParse(value.received).success) context.addIssue({ code: "custom", path: ["received"], message: "請輸入可沖銷的精確金額，最多 6 位小數。" });
  for (const [mode, amount, modePath, amountPath] of [
    [value.taxMode, value.tax, "taxMode", "tax"], [value.feeMode, value.fee, "feeMode", "fee"],
  ] as const) {
    if (!mode) context.addIssue({ code: "custom", path: [modePath], message: "請明確選擇未知或輸入金額。" });
    if (mode === "PROVIDED" && !money.max(50).safeParse(amount).success) context.addIssue({ code: "custom", path: [amountPath], message: "請輸入非負精確金額，最多 6 位小數。" });
  }
  if (value.currency === "USD") {
    if (!value.fxMode) context.addIssue({ code: "custom", path: ["fxMode"], message: "請明確選擇是否提供港幣匯率。" });
    if (value.fxMode === "PROVIDED") {
      if (!fx.max(50).safeParse(value.fxRate).success) context.addIssue({ code: "custom", path: ["fxRate"], message: "請輸入正數匯率，最多 12 位小數。" });
      if (!validDecisionText(value.fxSource, 1, 100) || value.fxSource === "IDENTITY") context.addIssue({ code: "custom", path: ["fxSource"], message: "請輸入 1 至 100 個有效字元的歷史匯率來源。" });
    }
  }
  if (value.override && (value.currency === "HKD" || (value.currency === "USD" && value.fxMode === "PROVIDED"))) {
    if (!reversibleMoney.safeParse(value.actualHkd).success) context.addIssue({ code: "custom", path: ["actualHkd"], message: "請輸入可沖銷的實際港幣金額，最多 6 位小數。" });
    else if (reversibleMoney.safeParse(value.received).success) {
      const received = BigInt(receiptMoneyIntent(value.received)!); const actual = BigInt(receiptMoneyIntent(value.actualHkd)!);
      if (value.currency === "HKD" && received !== actual)
        context.addIssue({ code: "custom", path: ["actualHkd"], message: "港幣實收與實際港幣金額必須相同。" });
      else if ((received === 0n && actual !== 0n) || (received > 0n && actual < 0n) || (received < 0n && actual > 0n))
        context.addIssue({ code: "custom", path: ["actualHkd"], message: "實際港幣金額的正負號須與原幣一致；原幣為零時港幣亦須為零。" });
    }
    if (!validDecisionText(value.actualReason, 10, 1000)) context.addIssue({ code: "custom", path: ["actualReason"], message: "請提供 10 至 1000 個有效字元的實際金額依據。" });
  }
  if (!validDecisionText(value.note, 0, 1000)) context.addIssue({ code: "custom", path: ["note"], message: "備註最多 1000 個有效字元，不能包含 NUL。" });
  if (!validDecisionText(value.reason, 10, 1000)) context.addIssue({ code: "custom", path: ["reason"], message: "請提供 10 至 1000 個有效字元的收款依據。" });
  if (!value.acknowledged) context.addIssue({ code: "custom", path: ["acknowledged"], message: "請確認收款記錄不證明付款或資格。" });
});

export function ReceiptWriteForm({ target: observed, onSaved, onCancel, disabled = false, onPendingChange }: {
  target: ReceiptTarget; onSaved: (record: Schemas["ReceiptCreated"]) => void; onCancel: () => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void;
}) {
  const [target] = useState(() => structuredClone(observed)); const [receiptId] = useState(() => crypto.randomUUID());
  const action = useFormAction(onSaved); const submitted = useRef<string | null>(null);
  const close = useContext(EditorCloseContext); const confirmationId = useId();
  const { register, control, setValue, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(formSchema), defaultValues: { receivedDate: "", currency: "", received: "", taxMode: "", tax: "", feeMode: "", fee: "",
      fxMode: "", fxRate: "", fxSource: "", override: false, actualHkd: "", actualReason: "", note: "", reason: "", acknowledged: false },
  });
  const currency = useWatch({ control, name: "currency" }); const fxMode = useWatch({ control, name: "fxMode" });
  const taxMode = useWatch({ control, name: "taxMode" }); const feeMode = useWatch({ control, name: "feeMode" }); const override = useWatch({ control, name: "override" });
  if (!validReceiptTarget(target)) return <p role="alert" className="text-sm text-danger">固定收款範圍無法安全核對；請關閉並重新載入事件、帳戶及收款依據。</p>;
  const { event, account, inputs, basis } = target;
  const currencyField = register("currency"); const fxField = register("fxMode");
  function clearFx() { setValue("fxMode", ""); setValue("fxRate", ""); setValue("fxSource", ""); setValue("override", false); setValue("actualHkd", ""); setValue("actualReason", ""); }
  return <FormFrame title="記錄股息收款" submitLabel="記錄收款" error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在記錄…"} onCancel={close ?? onCancel} onSubmit={submit => {
      if (disabled) { submit.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        if (!values.currency) return;
        const provided = values.currency === "USD" && values.fxMode === "PROVIDED";
        const actualOverride = (values.currency === "HKD" || provided) && values.override;
        const body: Schemas["ReceiptCreate"] = { receipt_id: receiptId, event_id: event.id, revision_id: inputs.revision_id, entitlement_id: basis.id,
          account_id: account.id, portfolio_id: inputs.portfolio_id, broker_id: inputs.broker_id, account_version: inputs.account_version,
          portfolio_version: inputs.portfolio_version, broker_version: inputs.broker_version, expected_data_revision: inputs.portfolio_data_revision,
          received_date: values.receivedDate, received_amount: values.received, currency: values.currency,
          tax: values.taxMode === "PROVIDED" ? values.tax : null, fee: values.feeMode === "PROVIDED" ? values.fee : null,
          fx_rate_hkd: provided ? values.fxRate : null, fx_source: provided ? values.fxSource : null,
          actual_hkd: actualOverride ? values.actualHkd : null, actual_hkd_reason: actualOverride ? values.actualReason : null,
          note: values.note, reason: values.reason, confirmation: "RECORDED_CASH_ONLY" };
        // Keep the receipt identity even when a changed uncertain intent needs a new key.
        const intent = JSON.stringify({ version: event.version, body: { ...body, received_amount: receiptMoneyIntent(body.received_amount),
          tax: receiptMoneyIntent(body.tax), fee: receiptMoneyIntent(body.fee), fx_rate_hkd: fxIntent(body.fx_rate_hkd), actual_hkd: receiptMoneyIntent(body.actual_hkd) } });
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (!validReceiptTarget(target)) throw new Error("固定收款範圍無法安全核對；請關閉並重新載入。");
          const header = { ...await mutationHeaders(key), "If-Match": `"${event.version}"` };
          const result = checked(await api.POST("/api/v1/dividends/receipts", { params: { header }, body }));
          return checkedReceiptSaved(result, target, body);
        });
      })(submit).finally(() => onPendingChange?.(false));
    }}>
    <div className="grid min-w-0 gap-2 text-sm leading-7 text-secondary sm:col-span-2">
      <p>本次固定事件、帳戶和選定的舊應收；這些舊資料未驗證，不代表持倉資格、收入或實際付款。</p>
      <p className="break-all">事件 {event.id}；目前修訂 {event.version}／{inputs.revision_id}；帳戶 {account.name}／{account.id}。</p>
      <p className="break-all">舊應收 {basis.id}；原修訂 {basis.revision_no}／{basis.revision_id}；記錄股數 {basis.recorded_shares}；舊預計淨額 {basis.currency} {basis.recorded_expected_net}，不會自動帶入實收。</p>
      <p className="break-all">組合 {inputs.portfolio_id}；券商 {inputs.broker_id}；帳戶／組合／券商版本 {inputs.account_version}／{inputs.portfolio_version}／{inputs.broker_version}；資料版本 {inputs.portfolio_data_revision}。</p>
      <p className="font-semibold text-danger">記錄會新增現金流水；付款日不會自動入賬。稅費是說明欄位，不會再次從淨收款扣除。</p>
      <p>回應未確認時請保留視窗，以相同內容重試；即使修改內容，本次收款識別碼不變，可避免重複入賬。關閉後請先核對歷史，不要假定未曾記錄。</p>
    </div>
    <Field label="實收日期" type="date" {...register("receivedDate")} error={errors.receivedDate?.message} help="請輸入實際日期；伺服器不接受新增未來收款。" />
    <SelectField label="實收幣別" {...currencyField} error={errors.currency?.message} onChange={change => { void currencyField.onChange(change); clearFx(); }} help="請依實際現金記錄選擇，不從事件或帳戶推定。">
      <option value="">請選擇</option><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>
    <Field label="實收淨額" inputMode="decimal" maxLength={51} {...register("received")} error={errors.received?.message} help="最多 6 位小數；保留正負號，不會自動捨入或再扣稅費。" />
    <SelectField label="實際稅額" {...register("taxMode")} error={errors.taxMode?.message}><option value="">請選擇</option><option value="UNKNOWN">未知，不是零</option><option value="PROVIDED">明確輸入金額</option></SelectField>
    {taxMode === "PROVIDED" && <Field label="已記錄稅額" inputMode="decimal" maxLength={51} {...register("tax")} error={errors.tax?.message} help="使用實收幣別，零是明確數值。" />}
    <SelectField label="實際費用" {...register("feeMode")} error={errors.feeMode?.message}><option value="">請選擇</option><option value="UNKNOWN">未知，不是零</option><option value="PROVIDED">明確輸入金額</option></SelectField>
    {feeMode === "PROVIDED" && <Field label="已記錄費用" inputMode="decimal" maxLength={51} {...register("fee")} error={errors.fee?.message} help="使用實收幣別，零是明確數值。" />}
    {currency === "USD" && <><SelectField label="港幣歷史匯率" {...fxField} error={errors.fxMode?.message} onChange={change => { void fxField.onChange(change); setValue("fxRate", ""); setValue("fxSource", ""); setValue("override", false); setValue("actualHkd", ""); setValue("actualReason", ""); }}>
      <option value="">請選擇</option><option value="UNKNOWN">未知，不是預設匯率</option><option value="PROVIDED">明確輸入匯率及來源</option></SelectField>
      {fxMode === "PROVIDED" && <><Field label="USD 兌 HKD 匯率" inputMode="decimal" maxLength={51} {...register("fxRate")} error={errors.fxRate?.message} help="1 USD 換算多少 HKD；最多 12 位小數。" />
        <Field label="港幣匯率來源" maxLength={202} {...register("fxSource")} error={errors.fxSource?.message} /></>}
    </>}
    {currency === "HKD" && <p className="text-sm leading-7 text-secondary sm:col-span-2">實收幣別是 HKD；港幣換算使用同幣恆等匯率 1，不會推定外部匯率。</p>}
    {(currency === "HKD" || (currency === "USD" && fxMode === "PROVIDED")) && <><label className="flex min-h-11 items-start gap-3 text-sm leading-7 sm:col-span-2"><input className="mt-2 shrink-0" type="checkbox" {...register("override")} />另行指定有依據的實際港幣金額；港幣實收時金額須相同。</label>
      {override && <><Field label="實際港幣金額" inputMode="decimal" maxLength={51} {...register("actualHkd")} error={errors.actualHkd?.message} />
        <Field label="實際港幣金額依據" maxLength={2002} {...register("actualReason")} error={errors.actualReason?.message} /></>}</>}
    <Field label="收款備註" maxLength={2002} {...register("note")} error={errors.note?.message} help="可留空；不要輸入密碼或金鑰。" />
    <Field label="收款依據" maxLength={2002} {...register("reason")} error={errors.reason?.message} help="至少 10 個字元；不要輸入密碼或金鑰。" />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.acknowledged} aria-describedby={errors.acknowledged ? confirmationId : undefined} {...register("acknowledged")} />我明白這只是擁有人記錄的現金，不證明實際付款、持倉資格或收入。</label>
      {errors.acknowledged && <p id={confirmationId} role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
