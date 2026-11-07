import { useQuery } from "@tanstack/react-query";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Link } from "react-router";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, mutationHeaders } from "../../lib/api";
import { date, evidenceUrl, optional, price, ratio, reason } from "../ledger/inputs";
import { useFormAction, usePageState } from "../settings/data";
import { SourceSecurities } from "./SourceInputs";
import { citationTitle, providerNames, type ObservationKind } from "./data";
import { SourceReadState } from "./details";

type Schemas = components["schemas"];
export type CitedResult = Schemas["CitedSourceCreated"];
const providers = ["hkex_evidence", "sec_evidence", "manual"] as const;
type CitedProvider = typeof providers[number];
const kinds = { UNKNOWN: "未知，不推定一般股息", REGULAR: "引用稱一般股息", SPECIAL: "特別股息", INTERIM: "中期股息", FINAL: "末期股息", SUPPLEMENTAL: "補充股息", ETF_DISTRIBUTION: "ETF 分派", REIT_DISTRIBUTION: "REIT 分派" } as const;
const periods = ["ANNUAL", "QUARTERLY", "MONTHLY", "WEEKLY", "INTERIM", "FINAL", "SPECIAL", "SUPPLEMENTAL", "OTHER"] as const;
const officialHosts = { hkex_evidence: ["hkexnews.hk", "www.hkexnews.hk", "www1.hkexnews.hk"], sec_evidence: ["sec.gov", "www.sec.gov"] };
function schema(mode: ObservationKind) {
  return z.object({
    security_ids: z.array(z.uuid()).length(1, "請明確加入一個穩定證券身分。"),
    provider_id: z.enum(providers, { error: "請明確選擇人工引用來源。" }),
    provider_symbol: z.string().trim().min(1, "請明確提供事件日期的來源代號。").max(64).regex(/^[A-Za-z0-9^][A-Za-z0-9.^=_-]*$/, "請保留原始前導零、點及有效符號；不接受網址或空白。"),
    day: date, reference: z.string().trim().min(1, "請提供文件或本機引用參考。").max(300, "引用參考最多 300 個字元。").refine(value => Array.from(value).every(character => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127), "引用參考不能包含控制字元。"),
    reason: reason.refine(value => !value.includes("\0"), "引用說明不能包含 NUL 字元。"),
    url: optional(evidenceUrl.refine(value => !value.includes("\x7f"), "引用網址不能包含控制字元。")),
    dps: optional(price), currency: z.enum(["", "HKD", "USD"]), event_type: z.enum(Object.keys(kinds) as [keyof typeof kinds, ...(keyof typeof kinds)[]]), adjustment_basis: z.enum(["UNKNOWN", "AS_DECLARED", "SPLIT_ADJUSTED"]),
    announce_date: optional(date), record_date: optional(date), pay_date: optional(date),
    source_currency: z.string().regex(/^$|^[A-Z]{3}$/, "來源幣別標籤只接受 3 個大寫英文字母，或留空。"), source_period: z.enum(["", ...periods]),
    source_frequency: z.string().refine(value => value === "" || (/^(0|[1-9][0-9]{0,2})$/.test(value) && BigInt(value) <= 365n), "請提供 0–365 的整數頻率標籤，或留空。"),
    ratio_numerator: optional(ratio), ratio_denominator: optional(ratio), source_adjustment_type: z.enum(["", "FORWARD_SPLIT", "REVERSE_SPLIT", "STOCK_DIVIDEND"]),
    acknowledged: z.boolean().refine(Boolean, "請確認只保存來源引用，不是文件驗證或財務採納。"),
  }).superRefine((values, context) => {
    if (mode === "splits") for (const key of ["ratio_numerator", "ratio_denominator"] as const) if (!values[key]) context.addIssue({ code: "custom", path: [key], message: "請明確提供正整數比例，不會假設 1:1。" });
    if (values.provider_id !== "manual") {
      let valid = false;
      try { const url = new URL(values.url); valid = url.protocol === "https:" && officialHosts[values.provider_id].includes(url.hostname) && (url.port === "" || url.port === "443"); } catch { /* Invalid attribution remains a field error, never a document request. */ }
      if (!valid) context.addIssue({ code: "custom", path: ["url"], message: "官方引用需要該來源的精確 HTTPS 文件網域及預設連接埠；這不是內容查核。" });
    }
  });
}
function AliasReference({ securityId, provider, day, onChoose }: { securityId: string; provider?: CitedProvider; day: string; onChoose: (symbol: string) => void }) {
  const page = usePageState();
  const result = useQuery({ queryKey: ["security", securityId, "provider-symbols", page.q, page.cursor], enabled: !!securityId, queryFn: async ({ signal }) => checked(await api.GET("/api/v1/securities/{id}/provider-symbols", { params: { path: { id: securityId }, query: { q: page.q, cursor: page.cursor, limit: 25 } }, signal })) });
  if (!securityId) return <p className="text-sm text-secondary sm:col-span-2">加入證券後可查閱已保存的來源代號；不會由顯示代號猜測。</p>;
  const rows = result.data?.items.filter(row => row.provider_id === provider) ?? [];
  return <details className="rounded-md border border-border p-4 sm:col-span-2"><summary className="flex min-h-11 cursor-pointer items-center font-medium">查閱已保存代號（不會自動填入）</summary><div className="mt-4 grid gap-4">
    <Field label="搜尋引用代號參考" type="search" maxLength={100} value={page.q} onChange={event => page.search(event.target.value)} help="只改候選查閱，不會更動已輸入代號或未確認寫入的識別碼。" />
    <SourceReadState result={result} label="代號參考" />
    <ul className="grid gap-3" aria-label="當頁引用代號參考">{rows.map(row => <li key={row.id} className="grid gap-2 rounded-md border border-border p-3 text-sm leading-7"><p className="break-all">{row.provider_symbol}：{row.effective_from} 至 {row.effective_to ?? "未結束"}；版本 {row.version}／{row.verified ? "已人工標記，非文件驗證" : "未核實映射"}</p><div><Button type="button" variant="outline" disabled={!date.safeParse(day).success || day < row.effective_from || !!row.effective_to && day > row.effective_to} onClick={() => onChoose(row.provider_symbol)}>使用此引用代號 {row.provider_symbol}</Button></div></li>)}</ul>
    <p className="text-sm leading-7 text-secondary">僅列本頁所選來源；空白、失敗或過時參考不會取消原鍵重試。新引用由伺服器重查日期／代號及權限；已完成的原鍵回覆仍須目前登入及 CSRF。</p>
    <div className="flex flex-wrap gap-3"><Button type="button" variant="outline" onClick={() => void result.refetch()} disabled={result.isFetching}>重新載入代號參考</Button><Button type="button" variant="outline" disabled={page.number === 1} onClick={page.previous}>上一頁代號參考</Button><Button type="button" variant="outline" disabled={!result.data?.next_cursor} onClick={() => page.next(result.data!.next_cursor!)}>下一頁代號參考</Button></div>
    <Link className="inline-flex min-h-11 items-center text-sm text-primary underline" to={`/settings/securities/${securityId}`} target="_blank" rel="noopener noreferrer">另開分頁核對證券及代號期間</Link>
  </div></details>;
}

export function CitedForm({ mode, onSaved, onCancel }: { mode: ObservationKind; onSaved: (record: CitedResult) => void; onCancel: () => void }) {
  const action = useFormAction(onSaved);
  const { register, control, setValue, getValues, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<ReturnType<typeof schema>>>({ resolver: zodResolver(schema(mode)), defaultValues: {
    security_ids: [], provider_id: undefined, provider_symbol: "", day: "", reference: "", reason: "", url: "", dps: "", currency: "", event_type: "UNKNOWN", adjustment_basis: "UNKNOWN", announce_date: "", record_date: "", pay_date: "", source_currency: "", source_period: "", source_frequency: "", ratio_numerator: "", ratio_denominator: "", source_adjustment_type: "", acknowledged: false,
  } });
  const [ids, provider, day] = useWatch({ control, name: ["security_ids", "provider_id", "day"] });
  const change = { onChange: action.changed };
  return <FormFrame title={citationTitle(mode)} submitLabel="儲存引用，不採納財務" pending={isSubmitting} error={action.error} onCancel={onCancel} onSubmit={handleSubmit(values => action.run(async key => {
    const common = { security_id: values.security_ids[0]!, provider_id: values.provider_id, provider_symbol: values.provider_symbol, citation: { citation: values.reference, reason: values.reason, url: values.url || null } };
    const body = mode === "dividends" ? { ...common, operation: "DIVIDENDS" as const, ex_date: values.day, dps: values.dps || null, currency: values.currency || null, event_type: values.event_type, adjustment_basis: values.adjustment_basis, announce_date: values.announce_date || null, record_date: values.record_date || null, pay_date: values.pay_date || null, source_currency: values.source_currency || null, source_period: values.source_period || null,
      // A bounded source frequency label is metadata, never a financial Number conversion.
      source_frequency: values.source_frequency === "" ? null : Number(values.source_frequency) }
      : { ...common, operation: "SPLITS" as const, effective_date: values.day, ratio_numerator: values.ratio_numerator, ratio_denominator: values.ratio_denominator, source_adjustment_type: values.source_adjustment_type || null };
    return checked(await api.POST("/api/v1/providers/cited-observations", { params: { header: await mutationHeaders(key) }, body }));
  }))}>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">只保存你引用的來源條款與說明；不抓取文件、不驗證內容、不建立 canonical 股息／實收／企業行動。新的確認意圖會新增引用，原紀錄不會改寫；未確認的重試保留原操作識別碼。</p>
    <SourceSecurities label="引用證券" limit={1} ids={ids} onChange={next => { setValue("security_ids", next); setValue("provider_symbol", ""); action.changed(); }} error={errors.security_ids?.message} />
    <SelectField label="引用來源" {...register("provider_id", { onChange: () => { setValue("provider_symbol", ""); action.changed(); } })} error={errors.provider_id?.message}><option value="">請明確選擇引用來源</option>{providers.map(id => <option key={id} value={id}>{providerNames[id]}</option>)}</SelectField>
    <Field label={mode === "dividends" ? "引用除淨日期" : "引用因子生效日期"} type="date" {...register("day", { onChange: () => { setValue("provider_symbol", ""); action.changed(); } })} error={errors.day?.message} help="明確的事件當地日期；不使用今天、擷取時間或 NAS 時區代替。" />
    <Field label="引用來源代號" maxLength={65} {...register("provider_symbol", change)} error={errors.provider_symbol?.message} help="原樣輸入有效期內的明確代號，或在下方明確選取；不猜 ticker、不移除前導零或點。" />
    <AliasReference key={`${ids[0] ?? ""}:${provider ?? ""}`} securityId={ids[0] ?? ""} provider={provider} day={day} onChoose={symbol => { if (symbol !== getValues("provider_symbol")) { setValue("provider_symbol", symbol); action.changed(); } }} />
    <p className="text-sm leading-7 text-secondary sm:col-span-2">來源需要既有的明確存取／權利政策，但不需要 API 金鑰或網路配額。<Link className="text-primary underline" to="/settings/providers" target="_blank" rel="noopener noreferrer">另開分頁管理來源政策</Link>；診斷／代號重查失敗不會禁止原鍵重試，也不會自動啟用來源。</p>
    {mode === "dividends" ? <>
      <Field label="引用每股金額（未知留空）" inputMode="decimal" maxLength={81} {...register("dps", change)} error={errors.dps?.message} help="最多 8 位小數，原字串送出；空白是未知，0 是明確的已知零，不是收入確認。" />
      <SelectField label="引用股息幣別" {...register("currency", change)}><option value="">未知，不從報價幣別推定</option><option value="HKD">HKD（引用明示）</option><option value="USD">USD（引用明示）</option></SelectField>
      <SelectField label="引用股息類型" {...register("event_type", change)}>{Object.entries(kinds).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</SelectField>
      <SelectField label="引用金額口徑" {...register("adjustment_basis", change)}><option value="UNKNOWN">UNKNOWN，口徑未知、待核對</option><option value="AS_DECLARED">AS_DECLARED，引用稱宣告條款，不是驗證</option><option value="SPLIT_ADJUSTED">SPLIT_ADJUSTED，拆股調整後、未採納</option></SelectField>
      {([ ["announce_date", "引用宣告日期"], ["record_date", "引用記錄日期"], ["pay_date", "引用付款日期"] ] as const).map(([key, label]) => <Field key={key} label={`${label}（選填）`} type="date" {...register(key, change)} error={errors[key]?.message} />)}
      <Field label="來源幣別標籤（選填）" maxLength={4} {...register("source_currency", change)} error={errors.source_currency?.message} help="例如 EUR；只保留原標籤，不補入股息幣別或作換匯。" />
      <SelectField label="來源期間標籤（選填）" {...register("source_period", change)}><option value="">未提供，不推定一般股息</option>{periods.map(value => <option key={value} value={value}>{value}</option>)}</SelectField>
      <Field label="來源頻率標籤（選填）" inputMode="numeric" maxLength={4} {...register("source_frequency", change)} error={errors.source_frequency?.message} help="0–365 的來源整數標籤；期間／頻率不會推定 REGULAR。" />
    </> : <>
      <Field label="引用比例分子" inputMode="numeric" maxLength={20} {...register("ratio_numerator", change)} error={errors.ratio_numerator?.message} help="正 INT64 整數字串；不以 Number 計算或猜測 1:1。" />
      <Field label="引用比例分母" inputMode="numeric" maxLength={20} {...register("ratio_denominator", change)} error={errors.ratio_denominator?.message} help="完整保留每一位，1:3 也只是 PROVIDER_ADJUSTMENT_ONLY。" />
      <SelectField label="來源調整類型（選填）" {...register("source_adjustment_type", change)}><option value="">未提供，不推定公司行動</option><option value="FORWARD_SPLIT">引用標記拆股</option><option value="REVERSE_SPLIT">引用標記合股</option><option value="STOCK_DIVIDEND">引用標記股票股息，仍未採納</option></SelectField>
    </>}
    <Field label="引用文件或本機參考" maxLength={301} {...register("reference", change)} error={errors.reference?.message} help="例如文件名稱及頁碼；不把官方網址當成內容驗證。" />
    <Field label="引用網址（人工來源可留空）" maxLength={2049} {...register("url", change)} error={errors.url?.message} help="官方引用須對應精確 HTTPS 網域；查詢／片段連結在讀取畫面會隱藏，原引用及雜湊保留。不要輸入帳密或金鑰。" />
    <Field label="引用說明" maxLength={1001} {...register("reason", change)} error={errors.reason?.message} help="至少 10 個字元，說明引用依據；不是宣稱文件已核實或股息已入帳。" />
    <div className="grid content-start gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" {...register("acknowledged")} />我明白只保存 SOURCE_ONLY／NOT_ADOPTED 引用，不代表文件驗證、另一供應商票、收入或實收。</label>{errors.acknowledged && <p role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
