import { useContext, useId, useRef, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { date, money, sameMoney } from "../ledger/inputs";
import { useFormAction } from "../settings/data";
import { checkedHistory, estimateRate, validEstimateTarget, type EstimateTarget } from "./estimate-validation";
import { decisionReason } from "./event-validation";

export type { EstimateTarget } from "./estimate-validation";
type Schemas = components["schemas"];
type Setting = Schemas["TaxSettingRecord"] | Schemas["FeeSettingRecord"];
type Input = Schemas["TaxSettingWrite"] | Schemas["FeeSettingWrite"];
const reason = decisionReason.refine(value => Array.from(value).every(character => {
  const point = character.codePointAt(0)!; return point < 0xd800 || point > 0xdfff;
}), "估算依據含有無效的 Unicode 字元。");
const schema = z.object({ effective_from: date, status: z.enum(["", "ESTIMATED", "UNKNOWN"]), amount: z.string(),
  currency: z.enum(["", "UNKNOWN", "HKD", "USD"]), reason, acknowledged: z.boolean() });
function decisionSchema(kind: EstimateTarget["kind"]) {
  return schema.superRefine((value, context) => {
    if (!value.status) context.addIssue({ code: "custom", path: ["status"], message: "請明確選擇估算或未知。" });
    if (value.status === "ESTIMATED" && !(kind === "tax" ? estimateRate : money.max(50)).safeParse(value.amount).success)
      context.addIssue({ code: "custom", path: ["amount"], message: kind === "tax" ? "請輸入 0 至 1 的精確稅率，最多 6 位小數。" : "請輸入有效的非負金額，最多 6 位小數。" });
    if (kind === "fee" && (!value.currency || (value.status === "ESTIMATED" && value.currency === "UNKNOWN")))
      context.addIssue({ code: "custom", path: ["currency"], message: "請明確選擇幣別；估算金額需要已知幣別。" });
    if (!value.acknowledged) context.addIssue({ code: "custom", path: ["acknowledged"], message: "請確認這不是已確認扣款或收入。" });
  });
}
function amountIntent(value: string | null): string | null {
  if (value === null) return null;
  const [whole, fraction = ""] = value.split(".");
  return `${whole!.replace(/^0+(?=\d)/, "")}.${fraction.padEnd(6, "0")}`;
}
function checkedSaved(record: Setting, target: EstimateTarget, body: Input): Setting {
  let valid: boolean;
  try {
    checkedHistory({ items: [record] }, target.event, target.account, target.kind);
    const selection = target.selection;
    valid = record.portfolio_id === body.portfolio_id && record.broker_id === body.broker_id
      && record.account_version === body.account_version && record.broker_version === body.broker_version
      && record.setting_no === (BigInt(selection.head_no) + 1n).toString() && record.previous_id === body.expected_head_id
      && record.effective_from === body.effective_from && record.reason === body.reason
      && ("rate" in body && "tax_status" in record
        ? record.tax_status === (body.rate === null ? "UNKNOWN" : "ESTIMATED")
          && (body.rate === null ? record.rate === null : record.rate !== null && sameMoney(record.rate, body.rate))
        : "fee_status" in body && "fee_status" in record && record.fee_status === body.fee_status
          && record.fee_currency === body.fee_currency
          && (body.fee === null ? record.fee === null : record.fee !== null && sameMoney(record.fee, body.fee)));
  } catch { valid = false; }
  if (!valid) throw new Error("伺服器回覆與本次估算不符；請保留輸入並核對歷史，不會假定未儲存。");
  return record;
}

export function EstimateWriteForm({ target: observed, onSaved, onCancel, disabled = false, onPendingChange }: {
  target: EstimateTarget; onSaved: (record: Setting) => void; onCancel: () => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void;
}) {
  const [target] = useState(() => structuredClone(observed));
  const action = useFormAction(onSaved); const submitted = useRef<string | null>(null);
  const close = useContext(EditorCloseContext); const confirmationId = useId();
  const { register, control, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(decisionSchema(target.kind)), defaultValues: { effective_from: "", status: "", amount: "", currency: "", reason: "", acknowledged: false },
  });
  const status = useWatch({ control, name: "status" });
  if (!validEstimateTarget(target)) return <p role="alert" className="text-sm text-danger">固定估算範圍無法安全核對；請關閉並重新載入事件、帳戶及拒絕狀態。</p>;
  const tax = target.kind === "tax";
  return <FormFrame title={tax ? "記錄稅率估算" : "記錄費用估算"} submitLabel="記錄估算" error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在記錄…"} onCancel={close ?? onCancel} onSubmit={event => {
      if (disabled) { event.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        const selection = target.selection;
        const base = { revision_id: target.event.current_revision_id, portfolio_id: selection.portfolio_id, broker_id: selection.broker_id,
          account_version: selection.account_version, broker_version: selection.broker_version, expected_head_no: selection.head_no,
          expected_head_id: selection.head_id, effective_from: values.effective_from, reason: values.reason };
        const value = values.status === "ESTIMATED" ? values.amount : null;
        const taxBody: Schemas["TaxSettingWrite"] = { ...base, rate: value };
        const feeBody: Schemas["FeeSettingWrite"] = { ...base, fee_status: values.status === "ESTIMATED" ? "ESTIMATED" : "UNKNOWN", fee: value,
          fee_currency: values.currency === "HKD" || values.currency === "USD" ? values.currency : null };
        const body = tax ? taxBody : feeBody;
        // Match decimal-equivalent retries without changing the submitted wire value.
        const intent = JSON.stringify({ kind: target.kind, version: target.event.version, body: tax
          ? { ...taxBody, rate: amountIntent(taxBody.rate) } : { ...feeBody, fee: amountIntent(feeBody.fee) } });
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (!validEstimateTarget(target)) throw new Error("固定估算範圍無法安全核對；請關閉並重新載入。");
          const header = { ...await mutationHeaders(key), "If-Match": `"${target.event.version}"` };
          const path = { event_id: target.event.id, account_id: target.account.id };
          const result = tax ? checked(await api.POST("/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings", { params: { path, header }, body: taxBody }))
            : checked(await api.POST("/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings", { params: { path, header }, body: feeBody }));
          return checkedSaved(result, target, body);
        });
      })(event).finally(() => onPendingChange?.(false));
    }}>
    <div className="grid min-w-0 gap-2 text-sm leading-7 text-secondary sm:col-span-2"><p>本次固定開啟時的事件、帳戶和已觀察設定頂端；伺服器會再次核對獨立拒絕狀態及所有版本，並追加紀錄而非修改歷史。</p>
      <p className="break-all">事件 {target.event.id}；版本 {target.event.version}；實體修訂 {target.event.current_revision_id}；帳戶 {target.account.name} / {target.account.id}。</p>
      <p className="break-all">組合 {target.selection.portfolio_id}；券商 {target.selection.broker_id}；帳戶版本 {target.selection.account_version}；券商版本 {target.selection.broker_version}；設定頂端 {target.selection.head_no} / {target.selection.head_id ?? "沒有設定"}。</p>
      <p>上次明確查閱日 {target.selection.effective_on}；這不是本次生效日，請另行輸入。讀取時未記錄拒絕不代表事件已接受或具財務資格。</p>
      <p>回應未確認時請保留視窗，以相同內容及操作識別碼重試；成功回覆可能是歷史重播。關閉後請核對歷史，不要假定未曾記錄。</p></div>
    <Field label="估算生效日" type="date" {...register("effective_from")} error={errors.effective_from?.message} help="請依實際依據明確輸入，不以查閱日、除淨日或今天推定。" />
    <SelectField label={tax ? "稅率估算狀態" : "費用估算狀態"} {...register("status")} error={errors.status?.message}>
      <option value="">請選擇</option><option value="ESTIMATED">明確估算</option><option value="UNKNOWN">明確未知，不是零</option></SelectField>
    {status === "ESTIMATED" && <Field label={tax ? "稅率估算小數" : "費用估算金額"} inputMode="decimal" maxLength={51} {...register("amount")} error={errors.amount?.message}
      help={tax ? "輸入 0 至 1；1 代表 100%，明確零不會當作未知。" : "以選定幣別輸入非負金額；明確零不會當作未知。"} />}
    {!tax && <SelectField label="費用估算幣別" {...register("currency")} error={errors.currency?.message} help="不從股息或帳戶幣別推定；未知金額可有明確幣別。">
      <option value="">請選擇</option><option value="UNKNOWN">明確未知</option><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>}
    <Field label="估算依據" maxLength={2002} {...register("reason")} error={errors.reason?.message} help="至少 10 個字元；不要輸入密碼或金鑰。" />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.acknowledged} aria-describedby={errors.acknowledged ? confirmationId : undefined} {...register("acknowledged")} />我明白這只是擁有人估算，不是已確認扣稅或扣費、持倉資格、收入或實收。</label>
      {errors.acknowledged && <p id={confirmationId} role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
