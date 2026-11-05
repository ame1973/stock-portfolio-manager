import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { ApiRequestError, checked, mutationHeaders } from "../../lib/api";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { useFormAction } from "../settings/data";
import { isCash, type Booking } from "./data";
import { date, fx, note, optional, reason, signedMoney } from "./inputs";

const manualKind = z.enum(["DEPOSIT", "WITHDRAW", "ADJUSTMENT"]);
const base = z.object({
  account_id: z.uuid("請選擇有效帳戶。"), currency: z.enum(["HKD", "USD"], "請選擇實際原幣。"),
  amount: signedMoney.refine(value => /[1-9]/.test(value), "金額不能為零。"), day: date,
  kind: z.union([manualKind, z.literal("")]), category: z.string().trim().max(100),
  fx_rate_hkd: optional(fx), actual_hkd: optional(signedMoney), reason: z.string().max(1000), note,
});

export function BookingForm({ mode, record, onSaved, onCancel }: {
  mode: "cash" | "expense"; record?: Booking; onSaved: (record: Booking) => void; onCancel: () => void;
}) {
  const action = useFormAction(onSaved);
  const cash = record && isCash(record) ? record : undefined;
  const expense = record && !isCash(record) ? record : undefined;
  const parsedKind = cash ? manualKind.safeParse(cash.kind) : undefined;
  const schema = base.superRefine((value, context) => {
    if (mode === "cash") {
      if (!value.kind) context.addIssue({ code: "custom", path: ["kind"], message: "請選擇存入、提款或調整。" });
      if ((value.kind === "DEPOSIT" && value.amount.startsWith("-")) || (value.kind === "WITHDRAW" && !value.amount.startsWith("-"))) context.addIssue({ code: "custom", path: ["amount"], message: "存入填正數，提款填負數；不會自動改變正負號。" });
    } else if (!value.category) context.addIssue({ code: "custom", path: ["category"], message: "請填寫費用分類。" });
    if ((record || value.actual_hkd || (mode === "cash" && value.kind === "ADJUSTMENT") || (mode === "expense" && value.amount.startsWith("-"))) && !reason.safeParse(value.reason).success) context.addIssue({ code: "custom", path: ["reason"], message: "更正、調整、退款或實際 HKD 需要至少 10 個字元的依據。" });
    if (value.actual_hkd && value.currency === "USD" && !value.fx_rate_hkd) context.addIssue({ code: "custom", path: ["fx_rate_hkd"], message: "實際 HKD 入帳仍須保留歷史匯率。" });
  });
  const { register, control, setValue, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.input<typeof base>, unknown, z.output<typeof base>>({
    resolver: zodResolver(schema), defaultValues: {
      account_id: record?.account_id ?? "", currency: record?.currency,
      amount: record?.amount ?? "", day: cash?.effective_date ?? expense?.date ?? "",
      kind: parsedKind?.success ? parsedKind.data : "", category: expense?.category ?? "",
      fx_rate_hkd: record?.fx_rate_hkd ?? "",
      actual_hkd: record?.currency === "USD" ? cash?.booked_hkd ?? expense?.hkd_amount ?? "" : "",
      reason: "", note: record?.note ?? "",
    },
  });
  const [accountId, currency, kind] = useWatch({ control, name: ["account_id", "currency", "kind"] });
  const title = `${record ? "更正" : "新增"}${mode === "cash" ? "現金紀錄" : "費用紀錄"}`;
  return <FormFrame title={title} error={action.error} pending={isSubmitting} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => {
      if (record && ((isCash(record) && !record.editable) || (!isCash(record) && record.status !== "ACTIVE"))) throw new ApiRequestError(isCash(record) ? { code: "SOURCE_MANAGED" } : { code: "INACTIVE_FACT" });
      const header = await mutationHeaders(key);
      const body = {
        account_id: values.account_id, amount: values.amount, currency: values.currency,
        fx_rate_hkd: values.currency === "USD" ? values.fx_rate_hkd || null : null,
        actual_hkd: values.currency === "USD" ? values.actual_hkd || null : null,
        reason: values.reason.trim() || null, note: values.note,
      };
      if (mode === "cash") {
        const kind = manualKind.parse(values.kind);
        const create = { ...body, effective_date: values.day, kind, external_flow: kind !== "ADJUSTMENT" } satisfies components["schemas"]["CashEntryCreate"];
        return record
          ? checked(await api.PATCH("/api/v1/cash-entries/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body: { ...create, reason: reason.parse(values.reason) } }))
          : checked(await api.POST("/api/v1/cash-entries", { params: { header }, body: create }));
      }
      const create = { ...body, date: values.day, category: values.category } satisfies components["schemas"]["ExpenseCreate"];
      return record
        ? checked(await api.PATCH("/api/v1/expenses/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body: { ...create, reason: reason.parse(values.reason) } }))
        : checked(await api.POST("/api/v1/expenses", { params: { header }, body: create }));
    }))}>
    <ReferenceSelect kind="accounts" label="入帳帳戶" value={accountId} registration={register("account_id")} error={errors.account_id?.message} clearSelection={() => setValue("account_id", "")} autoFocus />
    <SelectField label="實際原幣" {...register("currency", { onChange: () => { setValue("fx_rate_hkd", ""); setValue("actual_hkd", ""); } })} error={errors.currency?.message} help="獨立選擇實際現金幣別，不會按帳戶預設幣別自動換匯。"><option value="">請選擇實際幣別</option><option value="HKD">HKD 港元</option><option value="USD">USD 美元</option></SelectField>
    {mode === "cash" ? <SelectField label="現金分類" {...register("kind")} error={errors.kind?.message}><option value="">請選擇現金分類</option><option value="DEPOSIT">外部存入（正數）</option><option value="WITHDRAW">外部提款（負數）</option><option value="ADJUSTMENT">非外部調整（須依據）</option></SelectField>
      : <Field label="費用分類" {...register("category")} error={errors.category?.message} maxLength={100} />}
    <Field label="實際入帳日期" type="date" {...register("day")} error={errors.day?.message} help="只記錄已發生的現金／費用，不接受未來計畫。" />
    <Field label="實際原幣金額" inputMode="decimal" {...register("amount", { onChange: () => setValue("actual_hkd", "") })} error={errors.amount?.message} help={mode === "cash" ? "最多 6 位小數；存入正數、提款負數，調整可正可負但不可為零。" : "費用填正數；退款填負數並附依據。交易手續費已包含在交收，不要重複收費。"} />
    {currency === "USD" ? <Field label="現金歷史匯率（1 USD = HKD）" inputMode="decimal" {...register("fx_rate_hkd", { onChange: () => setValue("actual_hkd", "") })} error={errors.fx_rate_hkd?.message} help="可以未知；留空會保留原幣事實及 MISSING_FX，不會以現價或 7.8 補填。" /> : <p className="text-sm leading-7 text-secondary">{currency === "HKD" ? "HKD 使用 1:1 記帳，不需要外部匯率。" : "先選擇實際原幣，再核對歷史匯率與金額。"}</p>}
    {currency === "USD" && <Field label="沿用／指定 HKD 金額（選填）" inputMode="decimal" {...register("actual_hkd")} error={errors.actual_hkd?.message} help="更正時沿用已記錄 HKD 原值；改原幣金額或 FX 會清除此值。若有實際結單金額，請重新填寫並附依據；留空由伺服器計算。" />}
    <Field label="入帳／更正依據" {...register("reason")} maxLength={1000} error={errors.reason?.message} help="更正、非外部調整、負數退款與實際 HKD 必須說明至少 10 個字元。" />
    <Field label="入帳備註" {...register("note")} maxLength={1000} error={errors.note?.message} />
    <p className="text-sm leading-7 text-secondary sm:col-span-2">{mode === "cash" ? kind === "ADJUSTMENT" ? "這是非外部現金調整，不是外部存提款，也不會直接改寫期初斷言。" : "存入／提款是外部現金流；內部轉帳須使用成對轉帳流程，不能在此新增單一轉帳腿。" : "費用只入帳一次；負數代表有依據的退款，不會自動抵銷或重收既有交易費。"} 儲存後才確認入帳；原幣實際值保留 6 位，不會自動捨入至分。</p>
  </FormFrame>;
}
