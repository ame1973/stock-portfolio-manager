import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { ReferenceSelect } from "./ReferenceSelect";
import { useExchanges, useFormAction, type Account, type Broker, type Portfolio, type Security } from "./data";

const name = z.string().trim().min(1, "請填寫名稱。").max(100, "名稱最多 100 個字元。");
const money = z.string().regex(/^[0-9]+(\.[0-9]{1,6})?$/, "請輸入非負十進位字串，最多 6 位小數。").max(50);
const fx = z.string().regex(/^[0-9]+(\.[0-9]{1,12})?$/, "匯率最多 12 位小數，不能使用科學記號。").refine(value => /[1-9]/.test(value), "匯率必須大於零。");
const completeness = z.enum(["UNKNOWN", "PARTIAL", "COMPLETE"]);
const currency = z.enum(["HKD", "USD"]);
const cashOptions = <><option value="UNKNOWN">未知，尚未對帳</option><option value="PARTIAL">只有部分現金紀錄</option><option value="COMPLETE">全期現金帳已完整對帳</option></>;
const currencyOptions = <><option value="HKD">HKD 港元</option><option value="USD">USD 美元</option></>;
const completenessHelp = "只有完整且已對帳的現金歷史才能計算完整淨值；不要因已有交易就選擇完整。";
type Saved<T> = { record?: T; onSaved: (record: T) => void; onCancel: () => void };

const portfolioSchema = z.object({ name, cash_completeness: completeness });
export function PortfolioForm({ record, onSaved, onCancel }: Saved<Portfolio>) {
  const action = useFormAction(onSaved);
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof portfolioSchema>>({
    resolver: zodResolver(portfolioSchema), defaultValues: { name: record?.name ?? "", cash_completeness: record?.cash_completeness ?? "UNKNOWN" },
  });
  return <FormFrame title={record ? "編輯投資組合" : "新增投資組合"} error={action.error} pending={isSubmitting} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => {
      const header = await mutationHeaders(key);
      return record
        ? checked(await api.PATCH("/api/v1/portfolios/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body: values }))
        : checked(await api.POST("/api/v1/portfolios", { params: { header }, body: { name: values.name, base_currency: "HKD" } }));
    }))}>
    <Field label="組合名稱" {...register("name")} error={errors.name?.message} autoFocus />
    {record ? <SelectField label="現金帳完整性" {...register("cash_completeness")} error={errors.cash_completeness?.message} help={completenessHelp}>{cashOptions}</SelectField>
      : <p className="text-sm leading-7 text-secondary">報告幣別固定為 HKD。新組合的現金帳完整性預設為未知。</p>}
  </FormFrame>;
}

const brokerSchema = z.object({ name, dividend_fee: money, dividend_fee_currency: currency });
export function BrokerForm({ record, onSaved, onCancel }: Saved<Broker>) {
  const action = useFormAction(onSaved);
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof brokerSchema>>({
    resolver: zodResolver(brokerSchema), defaultValues: { name: record?.name ?? "", dividend_fee: record?.dividend_fee ?? "0.000000", dividend_fee_currency: record?.dividend_fee_currency ?? "HKD" },
  });
  return <FormFrame title={record ? "編輯券商" : "新增券商"} error={action.error} pending={isSubmitting} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => {
      const header = await mutationHeaders(key);
      return record
        ? checked(await api.PATCH("/api/v1/brokers/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body: values }))
        : checked(await api.POST("/api/v1/brokers", { params: { header }, body: values }));
    }))}>
    <Field label="券商名稱" {...register("name")} error={errors.name?.message} autoFocus />
    <Field label="每次股息估計代收費" inputMode="decimal" {...register("dividend_fee")} error={errors.dividend_fee?.message} />
    <SelectField label="代收費幣別" {...register("dividend_fee_currency")} error={errors.dividend_fee_currency?.message}>{currencyOptions}</SelectField>
    <p className="text-sm leading-7 text-secondary">費用設定只供之後的估計使用；既有實收、稅費與歷史匯率不會自動改寫。</p>
  </FormFrame>;
}

const accountSchema = z.object({
  name, portfolio_id: z.uuid("請選擇投資組合。"), broker_id: z.uuid("請選擇券商。"), default_currency: currency,
  cash_completeness: completeness, opening_cash: z.union([money, z.literal("")]),
  opening_cash_date: z.string(), opening_fx_rate_hkd: z.union([fx, z.literal("")]),
}).superRefine((value, context) => {
  if (value.opening_cash === "" && (value.opening_cash_date || value.opening_fx_rate_hkd)) context.addIssue({ code: "custom", path: ["opening_cash"], message: "日期或匯率必須配合明確的起始餘額；未知餘額請全部留空。" });
  if (value.opening_cash !== "" && !value.opening_cash_date) context.addIssue({ code: "custom", path: ["opening_cash_date"], message: "起始餘額必須有明確日期。" });
  if (value.opening_cash !== "" && value.default_currency === "USD" && !value.opening_fx_rate_hkd) context.addIssue({ code: "custom", path: ["opening_fx_rate_hkd"], message: "USD 起始餘額必須有當時鎖定的歷史匯率。" });
});
export function AccountForm({ record, onSaved, onCancel }: Saved<Account>) {
  const action = useFormAction(onSaved);
  const { register, handleSubmit, control, setValue, formState: { errors, isSubmitting } } = useForm<z.infer<typeof accountSchema>>({
    resolver: zodResolver(accountSchema), defaultValues: {
      name: record?.name ?? "", portfolio_id: record?.portfolio_id ?? "", broker_id: record?.broker_id ?? "",
      default_currency: record?.default_currency ?? "HKD", cash_completeness: record?.cash_completeness ?? "UNKNOWN",
      opening_cash: "", opening_cash_date: "", opening_fx_rate_hkd: "",
    },
  });
  const selectedCurrency = useWatch({ control, name: "default_currency" });
  const [selectedPortfolio, selectedBroker] = useWatch({ control, name: ["portfolio_id", "broker_id"] });
  return <FormFrame title={record ? "編輯帳戶" : "新增帳戶"} error={action.error} pending={isSubmitting} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => {
      const header = await mutationHeaders(key);
      if (record) return checked(await api.PATCH("/api/v1/accounts/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body: { name: values.name, cash_completeness: values.cash_completeness } }));
      return checked(await api.POST("/api/v1/accounts", { params: { header }, body: {
        name: values.name, portfolio_id: values.portfolio_id, broker_id: values.broker_id,
        default_currency: values.default_currency, cash_completeness: values.cash_completeness,
        ...(values.opening_cash === "" ? {} : { opening_cash: values.opening_cash, opening_cash_date: values.opening_cash_date,
          ...(values.default_currency === "USD" ? { opening_fx_rate_hkd: values.opening_fx_rate_hkd } : {}) }),
      } }));
    }))}>
    <Field label="帳戶名稱" {...register("name")} error={errors.name?.message} autoFocus />
    {!record ? <>
      <ReferenceSelect kind="portfolios" label="投資組合" registration={register("portfolio_id")} error={errors.portfolio_id?.message} value={selectedPortfolio} clearSelection={() => setValue("portfolio_id", "")} />
      <ReferenceSelect kind="brokers" label="券商" registration={register("broker_id")} error={errors.broker_id?.message} value={selectedBroker} clearSelection={() => setValue("broker_id", "")} />
      <SelectField label="帳戶預設幣別" {...register("default_currency")}>{currencyOptions}</SelectField>
    </> : <div className="text-sm leading-7 text-secondary">
      <input type="hidden" {...register("portfolio_id")} /><input type="hidden" {...register("broker_id")} /><input type="hidden" {...register("default_currency")} />
      <p>幣別：{record.default_currency}；組合及券商歸屬不能在此更換。</p>
      <p>起始餘額：{record.opening_cash ?? "未設定"}{record.opening_cash_date ? `（${record.opening_cash_date}）` : ""}。請使用帳戶的「現金與期初」頁面更正，不會直接改寫歷史。</p>
    </div>}
    <SelectField label="現金帳完整性" {...register("cash_completeness")} help={completenessHelp}>{cashOptions}</SelectField>
    {!record && <>
      <Field label="起始餘額（選填）" inputMode="decimal" {...register("opening_cash")} error={errors.opening_cash?.message} help="留空代表未知，不是零；這是起點餘額，不是外部存款。" />
      <Field label="起始餘額日期" type="date" {...register("opening_cash_date")} error={errors.opening_cash_date?.message} />
      {selectedCurrency === "USD" && <Field label="起始歷史匯率（1 USD = HKD）" inputMode="decimal" {...register("opening_fx_rate_hkd")} error={errors.opening_fx_rate_hkd?.message} help="必須使用當時實際／已核對匯率，不會自動填入現價或 7.8。" />}
    </>}
  </FormFrame>;
}

const securityBase = z.object({
  name, exchange_code: z.string().min(1, "請選擇交易所。"), display_ticker: z.string().trim().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/, "代碼僅接受字母、數字、點、底線及連字號。"),
  quote_currency: currency, security_type: z.enum(["EQUITY", "ETF", "REIT", "ADR", "FUND", "OTHER"]),
  status: z.enum(["ACTIVE", "SUSPENDED", "DELISTED", "UNKNOWN"]), verified: z.boolean(), reason: z.string().max(1000), identifier_effective_from: z.string(),
});
export function SecurityForm({ record, onSaved, onCancel }: Saved<Security>) {
  const action = useFormAction(onSaved);
  const exchanges = useExchanges();
  const schema = securityBase.superRefine((value, context) => {
    if (value.verified && !record?.verified && value.reason.trim().length < 10) context.addIssue({ code: "custom", path: ["reason"], message: "人工核實需要至少 10 個字元的依據說明。" });
  });
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof securityBase>>({
    resolver: zodResolver(schema), defaultValues: {
      name: record?.name ?? "", exchange_code: record?.exchange_code ?? "HKG", display_ticker: record?.display_ticker ?? "", quote_currency: record?.quote_currency ?? "HKD",
      security_type: (record?.security_type as z.infer<typeof securityBase>["security_type"]) ?? "EQUITY", status: record?.status ?? "ACTIVE", verified: record?.verified ?? false, reason: "", identifier_effective_from: "",
    },
  });
  return <FormFrame title={record ? "編輯證券" : "新增證券"} error={action.error} pending={isSubmitting} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => {
      const header = await mutationHeaders(key);
      if (record) return checked(await api.PATCH("/api/v1/securities/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body: {
        name: values.name, status: values.status, reason: values.reason, ...(values.verified !== record.verified ? { verified: values.verified } : {}),
      } }));
      return checked(await api.POST("/api/v1/securities", { params: { header }, body: {
        name: values.name, exchange_code: values.exchange_code, display_ticker: values.display_ticker, quote_currency: values.quote_currency, security_type: values.security_type,
        ...(values.identifier_effective_from ? { identifier_effective_from: values.identifier_effective_from } : {}),
      } }));
    }))}>
    <Field label="證券名稱" {...register("name")} error={errors.name?.message} autoFocus />
    {!record ? <>
      <Field label="證券代碼" {...register("display_ticker")} error={errors.display_ticker?.message} help="保留前導零與特殊字元，例如 0005、BRK.B；不同交易所不會合併。" />
      <SelectField label="交易所" {...register("exchange_code")} error={errors.exchange_code?.message} disabled={!exchanges.data}>
        {exchanges.data?.items.map(exchange => <option key={exchange.code} value={exchange.code}>{exchange.code} — {exchange.display_name}</option>)}
      </SelectField>
      <SelectField label="報價幣別" {...register("quote_currency")}>{currencyOptions}</SelectField>
      <SelectField label="證券類型" {...register("security_type")}><option value="EQUITY">股票</option><option value="ETF">ETF</option><option value="REIT">REIT</option><option value="ADR">ADR</option><option value="FUND">基金</option><option value="OTHER">其他</option></SelectField>
      <Field label="代碼有效起日（選填）" type="date" {...register("identifier_effective_from")} help="留空只從建立當日開始，不宣稱更早的有效性。" />
      <p className="text-sm leading-7 text-secondary">手動新增的證券預設未核實。不會自動猜測供應商代碼或宣稱外部資料覆蓋。</p>
    </> : <>
      <input type="hidden" {...register("display_ticker")} /><input type="hidden" {...register("exchange_code")} /><input type="hidden" {...register("quote_currency")} /><input type="hidden" {...register("security_type")} />
      <p className="text-sm leading-7 text-secondary">{record.display_ticker}／{record.exchange_code}／{record.quote_currency}；身分與幣別不會因改名而改寫。</p>
      <SelectField label="證券狀態" {...register("status")}><option value="ACTIVE">有效</option><option value="SUSPENDED">暫停交易</option><option value="DELISTED">已退市</option><option value="UNKNOWN">未知</option></SelectField>
      <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" {...register("verified")} />已人工核實證券身分</label>
      <Field label="人工核實依據" {...register("reason")} error={errors.reason?.message} />
    </>}
    {exchanges.isError && !record && <div role="alert" className="text-sm text-danger"><p>交易所載入失敗：{exchanges.error.message}</p><button type="button" className="min-h-11 underline" onClick={() => void exchanges.refetch()}>重試載入交易所</button></div>}
  </FormFrame>;
}
