import { useContext, useId, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { useFormAction } from "../settings/data";
import { normalizeDecisionText, validDecisionText } from "./event-validation";
import { sameId } from "./receipt-validation";
import { checkedReceiptVoided, validReceiptVoidTarget, type ReceiptVoidTarget } from "./receipt-void-validation";

type Schemas = components["schemas"];
const schema = z.object({
  reason: z.string().transform(normalizeDecisionText).refine(value => validDecisionText(value, 10, 1000), "請提供 10 至 1000 個有效字元的作廢依據。"),
  acknowledged: z.boolean().refine(value => value, "請確認作廢會新增反向現金流水。"),
});

export function ReceiptVoidForm({ target: observed, onSaved, onCancel, disabled = false, onPendingChange }: {
  target: ReceiptVoidTarget; onSaved: (record: Schemas["ReceiptVoided"]) => void; onCancel: () => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void;
}) {
  const [target] = useState(() => structuredClone(observed));
  const action = useFormAction(onSaved); const submitted = useRef<string | null>(null);
  const close = useContext(EditorCloseContext); const confirmationId = useId();
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema), defaultValues: { reason: "", acknowledged: false },
  });
  const { event, account, receipt, inputs } = target; const cash = inputs.cash_entry_id; const origin = inputs.origin_portfolio_id;
  if (!validReceiptVoidTarget(target) || cash === null || origin === null)
    return <p role="alert" className="text-sm text-danger">固定作廢範圍無法安全核對；請關閉並重新載入事件、帳戶及收款紀錄。</p>;
  return <FormFrame title="作廢股息收款" submitLabel="作廢收款並沖銷現金" submitClassName="bg-danger text-surface" error={action.error}
    pending={isSubmitting || disabled} pendingLabel={disabled ? "等候密碼確認…" : "正在作廢…"} onCancel={close ?? onCancel} onSubmit={submit => {
      if (disabled) { submit.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        const body: Schemas["ReceiptVoidWrite"] = { event_id: event.id, observed_revision_id: inputs.observed_revision_id,
          observed_revision_no: inputs.observed_revision_no, entitlement_id: inputs.entitlement_id, original_revision_id: inputs.original_revision_id,
          account_id: account.id, portfolio_id: inputs.portfolio_id, broker_id: inputs.broker_id, account_version: inputs.account_version,
          portfolio_version: inputs.portfolio_version, broker_version: inputs.broker_version, expected_data_revision: inputs.portfolio_data_revision,
          cash_entry_id: cash, reason: values.reason, confirmation: "VOID_RECORDED_CASH" };
        const intent = JSON.stringify({ receipt_id: receipt.id, receipt_version: inputs.receipt_version, body });
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (!validReceiptVoidTarget(target)) throw new Error("固定作廢範圍無法安全核對；請關閉並重新載入。");
          const header = { ...await mutationHeaders(key), "If-Match": `"${inputs.receipt_version}"` };
          const result = checked(await api.POST("/api/v1/dividends/receipts/{id}/void", { params: { path: { id: receipt.id }, header }, body }));
          return checkedReceiptVoided(result, target, body);
        });
      })(submit).finally(() => onPendingChange?.(false));
    }}>
    <div className="grid min-w-0 gap-2 text-sm leading-7 text-secondary sm:col-span-2">
      <p>這次只處理已選收款，不證明實際付款、持倉資格或收入。作廢不會改寫原有金額或歷史匯率；伺服器會新增一筆相反的現金流水。</p>
      <p>原收款日 {inputs.received_date}；原幣金額 {inputs.currency} {inputs.received_amount}；{inputs.received_hkd === null ? "港幣金額未知" : `港幣金額 ${inputs.received_hkd}`}。</p>
      <p>{inputs.fx_rate_hkd === null ? "原記錄港幣匯率未知" : `原記錄港幣匯率 ${inputs.fx_rate_hkd}`}；{inputs.tax === null ? "稅額未知" : `稅額 ${inputs.tax}`}；{inputs.fee === null ? "費用未知" : `費用 ${inputs.fee}`}。</p>
      <p className="break-all">收款 {receipt.id}；版本 {inputs.receipt_version}；原現金流水 {cash}；舊應收 {inputs.entitlement_id}；原修訂 {inputs.original_revision_id}。</p>
      <p className="break-all">事件 {event.id}；目前修訂 {inputs.observed_revision_no}／{inputs.observed_revision_id}；帳戶 {account.name}／{account.id}；目前組合 {inputs.portfolio_id}；券商 {inputs.broker_id}。</p>
      <p className="break-all">帳戶／組合／券商版本 {inputs.account_version}／{inputs.portfolio_version}／{inputs.broker_version}；資料版本 {inputs.portfolio_data_revision}。</p>
      {inputs.account_status === "ARCHIVED" && <p role="status">此帳戶已封存；仍可更正已核對來源的收款。</p>}
      {!sameId(origin, inputs.portfolio_id) && <p role="status" className="break-all text-danger">原組合與目前組合不同；原組合 {origin}、目前組合 {inputs.portfolio_id} 均會更新。</p>}
      <p>回應未確認時請保留視窗，以相同內容重試；若修改依據會使用新識別碼。關閉後請先核對收款及現金歷史，不要假定未曾作廢。</p>
    </div>
    <Field label="作廢依據" maxLength={2002} {...register("reason")} error={errors.reason?.message} help="至少 10 個字元；不要輸入密碼或金鑰。" />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.acknowledged}
      aria-describedby={errors.acknowledged ? confirmationId : undefined} {...register("acknowledged")} />我已核對原收款，確認作廢會新增反向現金流水；我明白這不證明付款、持倉資格或收入。</label>
      {errors.acknowledged && <p id={confirmationId} role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
