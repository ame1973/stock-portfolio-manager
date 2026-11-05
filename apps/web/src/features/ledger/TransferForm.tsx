import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { useFormAction } from "../settings/data";
import { isCashTransfer, type Transfer } from "./data";
import { date, fx, money, note, optional, quantity, reason, sameMoney } from "./inputs";

const currency = z.enum(["HKD", "USD"]);
const principal = money.refine(value => /[1-9]/.test(value), "本金必須大於零。");
const base = z.object({
  portfolio_id: z.uuid("請選擇投資組合。"), source_account_id: z.uuid("請選擇有效來源帳戶。"), target_account_id: z.uuid("請選擇有效目標帳戶。"),
  transfer_date: date, note, reason: z.string().max(1000),
  source_currency: z.union([currency, z.literal("")]), target_currency: z.union([currency, z.literal("")]),
  source_amount: optional(principal), target_amount: optional(principal), fx_rate_hkd: optional(fx), actual_hkd: optional(principal),
  security_id: z.string(), quantity: optional(quantity),
});

export function TransferForm({ mode, record, onSaved, onCancel }: { mode: "cash" | "security"; record?: Transfer; onSaved: (record: Transfer) => void; onCancel: () => void }) {
  const action = useFormAction(onSaved);
  const cash = record && isCashTransfer(record) ? record : undefined;
  const stock = record && !isCashTransfer(record) ? record : undefined;
  const schema = base.superRefine((value, context) => {
    if ((mode === "cash" || record) && !reason.safeParse(value.reason).success) context.addIssue({ code: "custom", path: ["reason"], message: "現金轉帳或任何更正都需要至少 10 個字元的依據。" });
    if (mode === "cash") {
      if (!value.source_currency) context.addIssue({ code: "custom", path: ["source_currency"], message: "請選擇來源實際原幣。" });
      if (!value.target_currency) context.addIssue({ code: "custom", path: ["target_currency"], message: "請選擇目標實際原幣。" });
      if (!value.source_amount) context.addIssue({ code: "custom", path: ["source_amount"], message: "請明確填寫正數來源本金。" });
      if (!value.target_amount) context.addIssue({ code: "custom", path: ["target_amount"], message: "請明確填寫正數目標本金。" });
      if (value.source_currency && value.source_currency === value.target_currency) {
        if (principal.safeParse(value.source_amount).success && principal.safeParse(value.target_amount).success && !sameMoney(value.source_amount, value.target_amount)) context.addIssue({ code: "custom", path: ["target_amount"], message: "同幣本金必須相等；真實費用請另行記錄，不會隱藏在差額中。" });
        if (value.source_account_id === value.target_account_id) context.addIssue({ code: "custom", path: ["target_account_id"], message: "同帳戶只允許明確不同原幣的兌換。" });
      }
      if (value.actual_hkd && value.source_currency === "HKD" && value.target_currency === "HKD") context.addIssue({ code: "custom", path: ["actual_hkd"], message: "HKD 恆等腿不使用 USD 的指定金額。" });
      if (value.actual_hkd && !value.fx_rate_hkd) context.addIssue({ code: "custom", path: ["fx_rate_hkd"], message: "指定 USD 腿的 HKD 仍需明確歷史 FX。" });
    } else {
      if (value.source_account_id === value.target_account_id) context.addIssue({ code: "custom", path: ["target_account_id"], message: "股票轉倉需要不同的來源與目標帳戶。" });
      if (!z.uuid().safeParse(value.security_id).success) context.addIssue({ code: "custom", path: ["security_id"], message: "請選擇轉倉證券。" });
      if (!value.quantity) context.addIssue({ code: "custom", path: ["quantity"], message: "請明確填寫正數轉倉股數。" });
    }
  });
  const { register, control, setValue, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof base>>({ resolver: zodResolver(schema), defaultValues: {
    portfolio_id: record?.portfolio_id ?? "", source_account_id: record?.source_account_id ?? "", target_account_id: record?.target_account_id ?? "", transfer_date: record?.transfer_date ?? "", note: record?.note ?? "", reason: "",
    source_currency: cash?.source_currency ?? "", target_currency: cash?.target_currency ?? "", source_amount: cash?.source_amount ?? "", target_amount: cash?.target_amount ?? "", fx_rate_hkd: cash?.fx_rate_hkd ?? "", actual_hkd: cash?.actual_hkd ?? "",
    security_id: stock?.security_id ?? "", quantity: stock?.quantity ?? "",
  } });
  const [portfolioId, sourceId, targetId, securityId, sourceCurrency, targetCurrency] = useWatch({ control, name: ["portfolio_id", "source_account_id", "target_account_id", "security_id", "source_currency", "target_currency"] });
  const hasUSD = sourceCurrency === "USD" || targetCurrency === "USD";
  function clearAccounts() { setValue("source_account_id", ""); setValue("target_account_id", ""); }
  function clearFx() { setValue("fx_rate_hkd", ""); setValue("actual_hkd", ""); }
  return <FormFrame title={`${record ? "更正" : "新增"}${mode === "cash" ? "成對現金轉帳" : "股票轉倉"}`} pending={isSubmitting} error={action.error} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => {
      const header = await mutationHeaders(key);
      const common = { portfolio_id: values.portfolio_id, source_account_id: values.source_account_id, target_account_id: values.target_account_id, transfer_date: values.transfer_date, note: values.note };
      if (mode === "cash") {
        const body = { ...common, source_currency: currency.parse(values.source_currency), target_currency: currency.parse(values.target_currency), source_amount: values.source_amount, target_amount: values.target_amount,
          fx_rate_hkd: values.source_currency === "USD" || values.target_currency === "USD" ? values.fx_rate_hkd || null : null, actual_hkd: values.source_currency === "USD" || values.target_currency === "USD" ? values.actual_hkd || null : null, reason: reason.parse(values.reason) } satisfies components["schemas"]["CashTransferCreate"];
        return record
          ? checked(await api.PATCH("/api/v1/cash-transfers/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body }))
          : checked(await api.POST("/api/v1/cash-transfers", { params: { header }, body }));
      }
      const body = { ...common, security_id: values.security_id, quantity: values.quantity } satisfies components["schemas"]["SecurityTransferCreate"];
      return record
        ? checked(await api.PATCH("/api/v1/security-transfers/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body: { ...body, reason: reason.parse(values.reason) } }))
        : checked(await api.POST("/api/v1/security-transfers", { params: { header }, body }));
    }))}>
    <ReferenceSelect kind="portfolios" label="轉倉組合" value={portfolioId} registration={{ ...register("portfolio_id"), onChange: event => { void register("portfolio_id").onChange(event); clearAccounts(); } }} clearSelection={() => { setValue("portfolio_id", ""); clearAccounts(); }} error={errors.portfolio_id?.message} autoFocus />
    <Field label="轉倉生效日期" type="date" {...register("transfer_date")} error={errors.transfer_date?.message} help={mode === "cash" ? "只記錄已發生的成對現金移動，不接受未來計畫。" : "使用證券交易所當地日期；未來轉倉不會建立成自動生效草稿。"} />
    <ReferenceSelect key={`source-${portfolioId}`} kind="accounts" portfolioId={portfolioId || undefined} label="來源帳戶" value={sourceId} registration={register("source_account_id")} clearSelection={() => setValue("source_account_id", "")} error={errors.source_account_id?.message} />
    <ReferenceSelect key={`target-${portfolioId}`} kind="accounts" portfolioId={portfolioId || undefined} label="目標帳戶" value={targetId} registration={register("target_account_id")} clearSelection={() => setValue("target_account_id", "")} error={errors.target_account_id?.message} />
    {mode === "cash" ? <>
      <SelectField label="來源實際幣別" {...register("source_currency", { onChange: clearFx })} error={errors.source_currency?.message}><option value="">請選擇來源幣別</option><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>
      <SelectField label="目標實際幣別" {...register("target_currency", { onChange: clearFx })} error={errors.target_currency?.message}><option value="">請選擇目標幣別</option><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>
      <Field label="來源本金" inputMode="decimal" {...register("source_amount", { onChange: () => setValue("actual_hkd", "") })} error={errors.source_amount?.message} help="正數實際本金，最多 6 位小數；不含另記費用。" />
      <Field label="目標本金" inputMode="decimal" {...register("target_amount", { onChange: () => setValue("actual_hkd", "") })} error={errors.target_amount?.message} help="同幣本金須相等；跨幣實際金額不會被拿來反推 FX。" />
      {hasUSD ? <><Field label="轉帳歷史匯率（1 USD = HKD）" inputMode="decimal" {...register("fx_rate_hkd", { onChange: () => setValue("actual_hkd", "") })} error={errors.fx_rate_hkd?.message} help="可明確未知並保留 MISSING_FX；不會使用現價或 7.8 預設。" />
        <Field label="USD 腿指定 HKD（選填）" inputMode="decimal" {...register("actual_hkd")} error={errors.actual_hkd?.message} help="正數指定值僅用於 USD 腿，仍須歷史 FX 與依據；改本金／FX／幣別會清除舊指定值。" /></>
        : <p className="text-sm leading-7 text-secondary">HKD 腿使用 1:1 數學恆等值，不會把它當作 USD 歷史匯率。</p>}
    </> : <><ReferenceSelect kind="securities" label="轉倉證券" value={securityId} registration={register("security_id")} clearSelection={() => setValue("security_id", "")} error={errors.security_id?.message} />
      <Field label="轉倉股數" inputMode="decimal" {...register("quantity")} error={errors.quantity?.message} help="正數，最多 8 位小數；不以日期結束持倉或猜測的成交時間借入股份。" />
      <p className="text-sm leading-7 text-secondary sm:col-span-2">成本由伺服器按轉倉事件之前的帳戶獨立移動平均計算，保留總成本及既有已實現損益。這不會新增現金、費用或獲利；建立時快照不是最新回放成本。</p></>}
    {(mode === "cash" || record) && <Field label="轉倉依據" {...register("reason")} error={errors.reason?.message} maxLength={1000} />}
    <Field label="轉倉備註" {...register("note")} error={errors.note?.message} maxLength={1000} />
    <p className="text-sm leading-7 text-secondary sm:col-span-2">僅限同一所選組合內的有效自有帳戶。{mode === "cash" ? "兩腿一併提交或沖銷，都是內部非外部現金流；歷史 HKD 差額不是匯兌損益。費用請另外記錄，不會自動扣收。" : "後續賣出或轉倉有依賴時，無法以更正／作廢繞過超賣檢查。"} 原值與修訂鏈不會覆寫。</p>
  </FormFrame>;
}
