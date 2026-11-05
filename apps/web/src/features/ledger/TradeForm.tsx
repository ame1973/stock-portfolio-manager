import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, failureMessage, mutationHeaders } from "../../lib/api";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { useFormAction } from "../settings/data";
import { date, fx, money, note, optional, price, quantity, reason, signedMoney } from "./inputs";
import { useSecurity } from "./data";

export type Trade = components["schemas"]["TradeRecord"];
const tradeSchema = z.object({
  portfolio_id: z.uuid("請選擇投資組合。"), account_id: z.uuid("請選擇有效帳戶。"), security_id: z.uuid("請選擇證券。"),
  side: z.enum(["BUY", "SELL"]), trade_date: date, executed_at_utc: z.string().max(40, "時間字串過長。"),
  quantity, price, fee: money, tax: money, fx_rate_hkd: optional(fx),
  fx_source: z.string().trim().min(1, "請說明匯率來源。").max(100),
  actual_settlement: optional(signedMoney), actual_hkd: optional(signedMoney), override_reason: note, note,
  reason: z.string().max(1000),
}).superRefine((value, context) => {
  if ((value.actual_settlement !== "" || value.actual_hkd !== "") && value.override_reason.trim().length < 10) context.addIssue({ code: "custom", path: ["override_reason"], message: "實際交收覆寫需要至少 10 個字元的依據。" });
});

export function TradeForm({ record, onSaved, onCancel }: {
  record?: Trade; onSaved: (record: Trade) => void; onCancel: () => void;
}) {
  const action = useFormAction(onSaved);
  const schema = tradeSchema.superRefine((value, context) => {
    if (record && !reason.safeParse(value.reason).success) context.addIssue({ code: "custom", path: ["reason"], message: "更正需要至少 10 個字元的具體依據。" });
  });
  const { register, control, setValue, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof tradeSchema>>({
    resolver: zodResolver(schema), defaultValues: {
      portfolio_id: record?.portfolio_id ?? "", account_id: record?.account_id ?? "", security_id: record?.security_id ?? "",
      side: record?.side ?? "BUY", trade_date: record?.trade_date ?? "", executed_at_utc: record?.executed_at_utc ?? "",
      quantity: record?.quantity ?? "", price: record?.price ?? "", fee: record?.fee ?? "0", tax: record?.tax ?? "0",
      fx_rate_hkd: record?.fx_rate_hkd ?? "", fx_source: record?.fx_source ?? "MANUAL",
      actual_settlement: record?.settlement_override ? record.settlement : "",
      actual_hkd: record?.hkd_override ? record.booked_hkd ?? "" : "", override_reason: record?.override_reason ?? "",
      note: record?.note ?? "", reason: "",
    },
  });
  const [portfolioId, accountId, securityId] = useWatch({ control, name: ["portfolio_id", "account_id", "security_id"] });
  const security = useSecurity(securityId);
  const clearAccount = () => setValue("account_id", "");
  return <FormFrame title={record ? "更正交易" : "新增交易"} error={action.error} pending={isSubmitting} onChange={action.changed} onCancel={onCancel}
    onSubmit={handleSubmit(values => action.run(async key => {
      if (!security.data || security.data.id !== values.security_id) throw new Error("證券幣別尚未確認；請等候載入或重試，不會猜測結算幣別。");
      const header = await mutationHeaders(key);
      const body = {
        portfolio_id: values.portfolio_id, account_id: values.account_id, security_id: values.security_id,
        side: values.side, trade_date: values.trade_date, executed_at_utc: values.executed_at_utc || null,
        quantity: values.quantity, price: values.price, fee: values.fee, tax: values.tax,
        currency: security.data.quote_currency, fx_rate_hkd: security.data.quote_currency === "USD" ? values.fx_rate_hkd || null : null,
        fx_source: values.fx_source, actual_settlement: values.actual_settlement || null, actual_hkd: values.actual_hkd || null,
        override_reason: values.override_reason, note: values.note,
      } satisfies components["schemas"]["TradeCreate"];
      return record
        ? checked(await api.PATCH("/api/v1/trades/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body: { ...body, reason: values.reason.trim() } }))
        : checked(await api.POST("/api/v1/trades", { params: { header }, body }));
    }))}>
    <ReferenceSelect kind="portfolios" label="投資組合" autoFocus value={portfolioId} registration={{ ...register("portfolio_id"), onChange: event => {
      void register("portfolio_id").onChange(event); clearAccount();
    } }} error={errors.portfolio_id?.message} clearSelection={() => { setValue("portfolio_id", ""); clearAccount(); }} />
    <ReferenceSelect key={portfolioId} kind="accounts" label="交易帳戶" portfolioId={portfolioId || undefined} value={accountId} registration={register("account_id")} error={errors.account_id?.message} clearSelection={clearAccount} />
    <ReferenceSelect kind="securities" label="交易證券" value={securityId} registration={{ ...register("security_id"), onChange: event => {
      void register("security_id").onChange(event); setValue("fx_rate_hkd", "");
    } }} error={errors.security_id?.message} clearSelection={() => { setValue("security_id", ""); setValue("fx_rate_hkd", ""); }} />
    <SelectField label="買賣方向" {...register("side")}><option value="BUY">買入</option><option value="SELL">賣出</option></SelectField>
    <Field label="交易日期" type="date" {...register("trade_date")} error={errors.trade_date?.message} help="使用證券交易所的當地日期；未來日期只儲存為未入帳草稿。" />
    <Field label="實際成交時間（選填）" {...register("executed_at_utc")} error={errors.executed_at_utc?.message} help="僅填入已知的 RFC3339 時間與時區，例如 2026-01-02T15:00:00Z；留空不會猜測時間。" />
    <Field label="交易股數" inputMode="decimal" {...register("quantity")} error={errors.quantity?.message} help="必須大於零，最多 8 位小數。" />
    <Field label="每股成交價" inputMode="decimal" {...register("price")} error={errors.price?.message} help="以證券報價幣別填寫，最多 8 位小數。" />
    <Field label="交易費用" inputMode="decimal" {...register("fee")} error={errors.fee?.message} help="已包含於交收及成本；不要再登記為營運費用。" />
    <Field label="交易稅費" inputMode="decimal" {...register("tax")} error={errors.tax?.message} />
    <div className="text-sm leading-7 text-secondary">
      {security.data ? <p>報價及結算幣別：{security.data.quote_currency}；{security.data.quote_currency === "HKD" ? "HKD 使用 1:1 記帳。" : "歷史 USD 匯率須明確填寫，不會使用現價或預設值。"}</p> : <p>請選擇證券，結算幣別不由帳戶預設幣別推算。</p>}
      {security.isError && <div role="alert"><p>{failureMessage(security.error)}</p><button type="button" className="min-h-11 underline" onClick={() => void security.refetch()}>重試確認證券幣別</button></div>}
    </div>
    {security.data?.quote_currency !== "HKD" && <Field label="歷史匯率（1 USD = HKD）" inputMode="decimal" {...register("fx_rate_hkd")} error={errors.fx_rate_hkd?.message} help="當日及過去交易必填；未來草稿可以未知，之後須明確更正才會入帳。" />}
    <Field label="歷史匯率來源說明" {...register("fx_source")} error={errors.fx_source?.message} help="人工填寫的來源不代表已獲外部核實。" />
    <Field label="備註" {...register("note")} maxLength={1000} error={errors.note?.message} />
    <div className="rounded-md border border-border p-4 sm:col-span-2">
      <h4 className="text-sm font-medium">使用券商實際交收金額（選填）</h4>
      <div className="mt-5 grid gap-5 sm:grid-cols-2">
        <Field label="實際原幣交收額" inputMode="decimal" {...register("actual_settlement")} error={errors.actual_settlement?.message} help="留空由伺服器按分計算；實際數值最多 6 位小數，不會改變原幣種類。" />
        <Field label="實際 HKD 入帳額" inputMode="decimal" {...register("actual_hkd")} error={errors.actual_hkd?.message} help="仍須保留歷史匯率；不是現價估值。" />
        <Field label="實際金額覆寫依據" {...register("override_reason")} maxLength={1000} error={errors.override_reason?.message} />
      </div>
    </div>
    {record && <><Field label="交易更正依據" {...register("reason")} maxLength={1000} error={errors.reason?.message} /><p className="text-sm leading-7 text-secondary">會建立新修訂及精確沖銷，保留原始股數、金額與 FX。舊版本或超額賣出不會覆蓋現有紀錄。</p></>}
    <p className="text-sm leading-7 text-secondary sm:col-span-2">此表單只記錄已核對的財務事實，不會向券商下單，也不會自動補足現金或換匯。交收與入帳結果以伺服器回應為準。</p>
  </FormFrame>;
}
