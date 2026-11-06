import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { date, optional } from "../ledger/inputs";
import { useFormAction } from "../settings/data";
import { SourceOperations, SourceProviders, SourceSecurities } from "./SourceInputs";
import { cadenceLabels, networkProviders, useProviders, weekdays, type Schedule, type SchedulePolicy } from "./data";

function schema(edit: boolean) {
  return z.object({
    name: z.string().trim().min(1, "請提供排程名稱。").max(100, "名稱最多 100 個字元。"),
    market: z.enum(["HK", "US"], { error: "請明確選擇市場。" }),
    security_ids: z.array(z.uuid()).min(1, "請至少加入一個證券。").max(100, "每個排程最多 100 個證券。"),
    provider_ids: z.array(z.enum([...networkProviders, "hkex_evidence", "sec_evidence", "manual"])).min(1, "請明確勾選至少一個來源。")
      .refine(ids => ids.every(value => networkProviders.some(id => id === value)), "自動排程不能捏造人工引用；請選擇明確網路來源。"),
    operations: z.array(z.enum(["DIVIDENDS", "SPLITS"])).min(1, "請選擇至少一項來源操作。"),
    first_date: date, backfill_from_date: optional(date),
    local_run_time: z.string().regex(/^(1[6-9]|2[0-3]):[0-5][0-9]$/, "請選擇 16:00–23:59 的市場當地分鐘。"),
    cadence: z.enum(["DAILY", "WEEKDAYS", "WEEKLY"], { error: "請明確選擇週期規則。" }),
    weekday: z.string(), window_unit: z.enum(["DAYS", "MONTHS"], { error: "請選擇日或曆月窗口。" }),
    window_size: z.string().regex(/^[1-9][0-9]{0,3}$/, "請輸入正整數，不接受小數、科學記號或補零。"),
    enabled: z.boolean(), reason: z.string().trim().max(1000, "依據最多 1000 個字元。"), acknowledged: z.boolean(),
  }).superRefine((values, context) => {
    if (Number(values.window_size) > (values.window_unit === "DAYS" ? 3660 : 120)) context.addIssue({ code: "custom", path: ["window_size"], message: "窗口最多 3660 日或 120 個曆月。" });
    if (values.cadence === "WEEKLY" && !/^[0-6]$/.test(values.weekday)) context.addIssue({ code: "custom", path: ["weekday"], message: "請明確選擇每週執行星期。" });
    if (values.backfill_from_date && values.backfill_from_date > values.first_date) context.addIssue({ code: "custom", path: ["backfill_from_date"], message: "首次回補起日不能晚於最早市場日期。" });
    if (edit && values.reason.length < 10) context.addIssue({ code: "custom", path: ["reason"], message: "請提供至少 10 個字元的具體依據。" });
    if (edit && values.enabled && !values.acknowledged) context.addIssue({ code: "custom", path: ["acknowledged"], message: "請確認自動排程只擷取來源，不會採納金融事實。" });
  });
}

export function ScheduleForm({ record, onSaved, onCancel }: { record?: Schedule; onSaved: (record: Schedule) => void; onCancel: () => void }) {
  const action = useFormAction(onSaved);
  const providers = useProviders();
  const policy = record?.policy;
  const { register, control, setValue, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<ReturnType<typeof schema>>>({
    resolver: zodResolver(schema(!!record)), defaultValues: {
      name: record?.name ?? "", market: policy?.market, security_ids: [...policy?.security_ids ?? []], provider_ids: [...policy?.provider_ids ?? []], operations: [...policy?.operations ?? ["DIVIDENDS"]],
      first_date: policy?.first_date ?? "", backfill_from_date: policy?.backfill_from_date ?? "", local_run_time: policy?.local_run_time ?? "",
      cadence: policy?.cadence, weekday: policy?.weekday == null ? "" : String(policy.weekday),
      window_unit: policy ? policy.lookback_days == null ? "MONTHS" : "DAYS" : undefined,
      window_size: policy ? String(policy.lookback_days ?? policy.lookback_months) : "", enabled: record?.enabled ?? false, reason: "", acknowledged: false,
    },
  });
  const [securityIds, market, cadence, windowUnit, enabled] = useWatch({ control, name: ["security_ids", "market", "cadence", "window_unit", "enabled"] });
  const title = record ? "編輯來源排程" : "建立停用來源排程";
  return <FormFrame title={title} submitLabel={record ? "儲存排程修訂" : "儲存停用排程"} error={action.error} pending={isSubmitting} onCancel={onCancel}
    onSubmit={handleSubmit(values => action.run(async key => {
      if (!record && !providers.data) throw new Error("來源診斷尚未載入；請核對明確來源，不會自動選取。");
      const policy: SchedulePolicy = {
        schema_version: 1, scope: "SOURCE_ONLY", calendar_basis: "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR",
        security_ids: [...values.security_ids].sort(), provider_ids: [...values.provider_ids].sort(), operations: [...values.operations].sort(),
        market: values.market, first_date: values.first_date, local_run_time: values.local_run_time, cadence: values.cadence,
        weekday: values.cadence === "WEEKLY" ? Number(values.weekday) : null,
        lookback_days: values.window_unit === "DAYS" ? Number(values.window_size) : null,
        lookback_months: values.window_unit === "MONTHS" ? Number(values.window_size) : null,
        backfill_from_date: values.backfill_from_date || null,
      };
      const body = { name: values.name, policy } satisfies components["schemas"]["SourceScheduleCreate"];
      return record ? checked(await api.PATCH("/api/v1/source-schedules/{id}", { params: { path: { id: record.id }, header: { ...await mutationHeaders(key), "If-Match": `"${record.version}"` } }, body: { ...body, enabled: values.enabled, reason: values.reason } }))
        : checked(await api.POST("/api/v1/source-schedules", { params: { header: await mutationHeaders(key) }, body }));
    }))}>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">新排程一律停用，另行明確啟用才允許 worker 到期排隊。SOURCE_ONLY / NOT_RUN：不確認股息、實收、金融帳務或官方交易時段。暫停只影響未來評估，已排隊工作須另行取消。</p>
    <Field label="排程名稱" maxLength={100} {...register("name", { onChange: action.changed })} error={errors.name?.message} />
    <SelectField label="排程市場" {...register("market", { onChange: () => { setValue("security_ids", []); action.changed(); } })} error={errors.market?.message} help="以證券交易所目錄核對，不以報價幣別猜市場；換市場會清除已加入證券。"><option value="">請明確選擇市場</option><option value="HK">香港 ／ Asia/Hong_Kong</option><option value="US">美國 ／ America/New_York</option></SelectField>
    <SourceSecurities key={market} label="排程證券" ids={securityIds} error={errors.security_ids?.message} onChange={ids => { setValue("security_ids", ids, { shouldValidate: true }); action.changed(); }} />
    <Field label="最早市場日期" type="date" min="0001-01-01" max="9999-12-31" {...register("first_date", { onChange: action.changed })} error={errors.first_date?.message} help="這是最早可選時段日期。修訂／重新啟用以伺服器時間重算未來時段，不重播舊日積欠。" />
    <Field label="市場當地執行時間" type="time" min="16:00" max="23:59" step={60} {...register("local_run_time", { onChange: action.changed })} error={errors.local_run_time?.message} help={`${market === "HK" ? "Asia/Hong_Kong" : market === "US" ? "America/New_York（隨夏令時間轉 UTC）" : "請明確選擇市場"}；16:00–23:59，不是已驗證收市或競價完成。`} />
    <SelectField label="週期規則" {...register("cadence", { onChange: () => { setValue("weekday", ""); action.changed(); } })} error={errors.cadence?.message}><option value="">請明確選擇週期</option>{Object.entries(cadenceLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</SelectField>
    {cadence === "WEEKLY" && <SelectField label="每週執行星期" {...register("weekday", { onChange: action.changed })} error={errors.weekday?.message}><option value="">請明確選擇星期</option>{weekdays.map((label, value) => <option value={value} key={label}>{label}</option>)}</SelectField>}
    <SelectField label="回看窗口單位" {...register("window_unit", { onChange: () => { setValue("window_size", ""); action.changed(); } })} error={errors.window_unit?.message}><option value="">請選擇日或曆月</option><option value="DAYS">含首尾的日數</option><option value="MONTHS">曆月（保留同日，月底裁切）</option></SelectField>
    <Field label="回看窗口數量" type="text" inputMode="numeric" maxLength={4} {...register("window_size", { onChange: action.changed })} error={errors.window_size?.message} help={windowUnit === "MONTHS" ? "1–120 個曆月，不換成固定 30 日；包含裁切後起日與市場終日。" : "1–3660 日，包含首尾；例如 30 日是市場終日及之前 29 日。"} />
    <Field label="首次回補起日" type="date" min="0001-01-01" max="9999-12-31" readOnly={record?.initial_backfill_queued} {...register("backfill_from_date", { onChange: action.changed })} error={errors.backfill_from_date?.message} help={record?.initial_backfill_queued ? "首次回補已排隊，起日鎖定；失敗或取消不會自動整段重發。" : "可留白。僅首次真正排隊使用此明確起日，不能晚於最早市場日期。"} />
    <SourceProviders result={providers} registration={register("provider_ids", { onChange: action.changed })} error={errors.provider_ids?.message} />
    <SourceOperations registration={register("operations", { onChange: action.changed })} error={errors.operations?.message} />
    {record && <>
      <label className="flex min-h-11 items-center gap-3 text-sm sm:col-span-2"><input type="checkbox" {...register("enabled", { onChange: action.changed })} />啟用這個來源排程，依明確時段允許自動排隊。</label>
      {enabled && <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input type="checkbox" className="mt-2 shrink-0" {...register("acknowledged")} />我明白已啟用排程可使用所選來源配額，但不代表已擷取、已驗證或已收收入。</label>{errors.acknowledged?.message && <p role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>}
      <Field label="排程修訂依據" maxLength={1000} {...register("reason", { onChange: action.changed })} error={errors.reason?.message} help={`正在修訂開啟時觀察的版本 ${record.version}；worker 推進也會使版本失效，不會覆蓋後來資料。`} />
    </>}
  </FormFrame>;
}
