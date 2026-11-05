import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { useFormAction, type Account } from "../settings/data";
import { date, fx, money, note, optional, reason } from "./inputs";
import type { Opening } from "./data";

const openingSchema = z.object({ opening_cash: money, opening_cash_date: date, opening_fx_rate_hkd: optional(fx), reason, note });

export function OpeningForm({ record, onSaved, onCancel }: { record: Opening; onSaved: (record: Opening) => void; onCancel: () => void }) {
  const action = useFormAction(onSaved);
  const schema = openingSchema.superRefine((value, context) => {
    if (record.currency === "USD" && !value.opening_fx_rate_hkd) context.addIssue({ code: "custom", path: ["opening_fx_rate_hkd"], message: "USD 期初斷言必須有明確歷史 FX，即使金額為零。" });
  });
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof openingSchema>>({
    resolver: zodResolver(schema), defaultValues: {
      opening_cash: record.opening_cash ?? "", opening_cash_date: record.opening_cash_date ?? "",
      opening_fx_rate_hkd: record.quality === "OK" ? record.fx_rate_hkd ?? "" : "", reason: "", note: "",
    },
  });
  return <FormFrame title="設定／更正期初斷言" error={action.error} pending={isSubmitting} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => checked(await api.PATCH("/api/v1/accounts/{id}/opening-cash", {
      params: { path: { id: record.account_id }, header: { ...await mutationHeaders(key), "If-Match": `"${record.version}"` } },
      body: { ...values, opening_fx_rate_hkd: record.currency === "USD" ? values.opening_fx_rate_hkd : null },
    }))))}>
    <Field label={`期初餘額（${record.currency}）`} inputMode="decimal" {...register("opening_cash")} error={errors.opening_cash?.message} autoFocus help="必須明確填寫；0 是已知零，空白不能儲存，也不會撤回現有斷言。" />
    <Field label="期初生效日期" type="date" {...register("opening_cash_date")} error={errors.opening_cash_date?.message} help="這是當日開始時的餘額斷言，不是外部存款。" />
    {record.currency === "USD" ? <Field label="期初歷史匯率（1 USD = HKD）" inputMode="decimal" {...register("opening_fx_rate_hkd")} error={errors.opening_fx_rate_hkd?.message} help="即使金額為零也必須明確提供；不沿用有問題的來源或預設匯率。" /> : <p className="text-sm leading-7 text-secondary">期初幣別固定為帳戶預設 HKD，以 1:1 記帳；不會替其他原幣錢包補足現金。</p>}
    <Field label="期初更正依據" {...register("reason")} maxLength={1000} error={errors.reason?.message} />
    <Field label="期初備註" {...register("note")} maxLength={1000} error={errors.note?.message} />
    <p className="text-sm leading-7 text-secondary sm:col-span-2">會精確沖銷目前斷言及其有效期初調整，再按新斷言的 FX 建立更正。原始交易與現金不會覆寫。重新設定零或金額不會自動宣稱完整對帳；撤回請使用獨立的確認流程。</p>
  </FormFrame>;
}

const completenessSchema = z.object({ cash_completeness: z.enum(["UNKNOWN", "PARTIAL", "COMPLETE"]), confirmed: z.boolean() }).superRefine((value, context) => {
  if (value.cash_completeness === "COMPLETE" && !value.confirmed) context.addIssue({ code: "custom", path: ["confirmed"], message: "必須明確確認已核對所有原幣、現金歷史與期初來源。" });
});

export function CompletenessForm({ record, onSaved, onCancel }: { record: Account; onSaved: (record: Account) => void; onCancel: () => void }) {
  const action = useFormAction(onSaved);
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof completenessSchema>>({
    resolver: zodResolver(completenessSchema), defaultValues: { cash_completeness: record.cash_completeness, confirmed: false },
  });
  return <FormFrame title="設定現金完整性" error={action.error} pending={isSubmitting} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => checked(await api.PATCH("/api/v1/accounts/{id}", {
      params: { path: { id: record.id }, header: { ...await mutationHeaders(key), "If-Match": `"${record.version}"` } }, body: { cash_completeness: values.cash_completeness },
    }))))}>
    <SelectField label="帳戶現金完整性" {...register("cash_completeness")} error={errors.cash_completeness?.message}><option value="UNKNOWN">未知，尚未對帳</option><option value="PARTIAL">只有部分紀錄</option><option value="COMPLETE">全期現金已完整核對</option></SelectField>
    <div className="grid content-start gap-2"><label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" {...register("confirmed")} />我已核對所有原幣、現金歷史及期初來源</label>
      {errors.confirmed && <p role="alert" className="text-sm text-danger">{errors.confirmed.message}</p>}</div>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">完整性是你的明確斷言，不會因新增紀錄、補 FX 或重新設定期初而自動成立。COMPLETE 仍不能把缺少匯率或行情的數值當作完整 HKD 估值。</p>
  </FormFrame>;
}
