import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { networkProviders, providerNames } from "../sources/data";
import { useFormAction } from "./data";
import { acceptsCredential, networkProfile, type ProviderPolicy, type ProviderProfile } from "./provider-data";

const reason = z.string().trim().min(10, "請提供至少 10 個字元的具體依據。").max(1000, "依據最多 1000 個字元。").refine(value => !value.includes("\0"), "依據不能包含 NUL 字元。");
const budget = z.string().refine(value => value === "" || (/^[1-9][0-9]{0,9}$/.test(value) && BigInt(value) <= 2147483647n), "請輸入 1–2147483647 的整數，不接受補零、小數或科學記號。");
const exactCredential = /^[\x21-\x7e]{1,256}$/;
function policySchema(record: ProviderProfile | undefined) {
  return z.object({
    provider_id: z.enum([...networkProviders, "hkex_evidence", "sec_evidence", "manual"], { error: "請明確選擇來源。" }),
    enabled: z.boolean(), terms_acknowledged: z.boolean(),
    terms_reference: z.string().max(1000, "參考最多 1000 個字元。").refine(value => !value.includes("\0"), "參考不能包含 NUL 字元。"),
    terms_reason: z.union([z.literal(""), reason]), requests_per_minute: budget, requests_per_day: budget,
    reason, clear_credential: z.boolean(), acknowledged: z.boolean(),
  }).superRefine((values, context) => {
    const issue = (path: keyof typeof values, message: string) => context.addIssue({ code: "custom", path: [path], message });
    if (values.terms_acknowledged && (!values.terms_reference.trim() || !values.terms_reason)) issue("terms_reference", "授權確認需要非空白參考與具體依據，不能以金鑰代替。");
    if (!networkProfile(values.provider_id) && (values.requests_per_minute || values.requests_per_day)) issue("requests_per_minute", "人工引用不能宣稱網路配額。");
    if (values.enabled) {
      if (!record || values.provider_id !== record.policy.provider_id) issue("enabled", "初次政策必須停用，修訂不能移動來源身分。");
      if (!values.terms_acknowledged) issue("terms_acknowledged", "啟用前請明確確認資料權利。");
      if (networkProfile(values.provider_id) && (!values.requests_per_minute || !values.requests_per_day)) issue("requests_per_minute", "啟用網路來源需要明確的每分鐘及每日軟體上限。");
      if (acceptsCredential(values.provider_id) && (!record?.credential_reference_present || values.clear_credential)) issue("enabled", "請先另行匯入私密金鑰；清除參考的政策必須停用。");
      if (!values.acknowledged) issue("acknowledged", "請確認來源存取授權不代表健康、完整覆蓋或收入採納。");
    }
  });
}

export function ProviderPolicyForm({ record, onSaved, onCancel }: { record?: ProviderProfile; onSaved: (record: ProviderProfile) => void; onCancel: () => void }) {
  const action = useFormAction(onSaved);
  const policy = record?.policy;
  const { register, control, setValue, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<ReturnType<typeof policySchema>>>({
    resolver: zodResolver(policySchema(record)), defaultValues: { provider_id: policy?.provider_id, enabled: policy?.enabled ?? false,
      terms_acknowledged: policy?.terms_acknowledged ?? false, terms_reference: policy?.terms_reference ?? "", terms_reason: policy?.terms_reason ?? "",
      requests_per_minute: policy?.requests_per_minute == null ? "" : String(policy.requests_per_minute), requests_per_day: policy?.requests_per_day == null ? "" : String(policy.requests_per_day),
      reason: "", clear_credential: false, acknowledged: false },
  });
  const [provider, enabled] = useWatch({ control, name: ["provider_id", "enabled"] });
  return <FormFrame title={record ? "修訂來源政策" : "建立停用來源政策"} submitLabel={record ? "儲存政策修訂" : "儲存停用政策"} error={action.error} pending={isSubmitting} onCancel={onCancel}
    onSubmit={handleSubmit(values => action.run(async key => {
      const policy: ProviderPolicy = { provider_id: values.provider_id, enabled: record ? values.enabled : false, terms_acknowledged: values.terms_acknowledged,
        terms_reference: values.terms_reference, terms_reason: values.terms_reason || null, raw_payload_storage: false,
        // These bounded integers are software request limits, never financial values or billing quotas.
        requests_per_minute: values.requests_per_minute === "" ? null : Number(values.requests_per_minute), requests_per_day: values.requests_per_day === "" ? null : Number(values.requests_per_day) };
      const body = { policy, reason: values.reason } satisfies components["schemas"]["ProviderProfileCreate"];
      return record ? checked(await api.PATCH("/api/v1/provider-profiles/{provider_id}", { params: { path: { provider_id: record.policy.provider_id }, header: { ...await mutationHeaders(key), "If-Match": `"${record.version}"` } }, body: { ...body, clear_credential: values.clear_credential } }))
        : checked(await api.POST("/api/v1/provider-profiles", { params: { header: await mutationHeaders(key) }, body }));
    }))}>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">個人政策會取代這個來源的安裝層回退，包括停用、清除或檔案不可用的決定。建立及金鑰匯入一律停用；不會恢復舊金鑰、探測供應商或自動排隊。</p>
    {record ? <Field label="政策來源" value={providerNames[record.policy.provider_id]} readOnly /> : <SelectField label="政策來源" {...register("provider_id", { onChange: () => { for (const field of ["terms_reference", "terms_reason", "requests_per_minute", "requests_per_day"] as const) setValue(field, ""); setValue("terms_acknowledged", false); action.changed(); } })} error={errors.provider_id?.message}>
      <option value="">請明確選擇來源</option>{Object.entries(providerNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
    </SelectField>}
    {provider && networkProfile(provider) && <>
      <Field label="每分鐘軟體要求上限" inputMode="numeric" maxLength={10} {...register("requests_per_minute", { onChange: action.changed })} error={errors.requests_per_minute?.message} help="沒有預設值；Yahoo／yfinance 共用同一上游，採適用政策的較低上限。" />
      <Field label="滾動 24 小時軟體要求上限" inputMode="numeric" maxLength={10} {...register("requests_per_day", { onChange: action.changed })} error={errors.requests_per_day?.message} help="不是供應商帳單剩餘配額；不猜測每日重設時區。" />
    </>}
    {provider && !networkProfile(provider) && <p className="text-sm leading-7 text-secondary">人工／官方引用不接受網路配額或 API 金鑰。保存政策不等於文件已驗證；引用寫入另有工作流程。</p>}
    <Field label="資料權利參考" maxLength={1000} {...register("terms_reference", { onChange: action.changed })} error={errors.terms_reference?.message} help="只放非秘密的授權／文件參考；不要貼上金鑰、密碼或含秘密的網址。" />
    <Field label="資料權利依據" maxLength={1000} {...register("terms_reason", { onChange: action.changed })} error={errors.terms_reason?.message} help="若確認權利，請提供至少 10 個字元；這是你的明確聲明，不是獨立授權查核。" />
    <label className="flex min-h-11 items-start gap-3 text-sm leading-7 sm:col-span-2"><input type="checkbox" className="mt-2 shrink-0" {...register("terms_acknowledged", { onChange: action.changed })} />我已核對此來源的資料權利及使用條件。</label>
    {errors.terms_acknowledged && <p role="alert" className="text-sm text-danger sm:col-span-2">{errors.terms_acknowledged.message}</p>}
    {record && <>
      <label className="flex min-h-11 items-start gap-3 text-sm leading-7 sm:col-span-2"><input type="checkbox" className="mt-2 shrink-0" {...register("enabled", { onChange: () => { setValue("acknowledged", false); action.changed(); } })} />啟用此個人來源政策，允許未來明確工作的來源存取。</label>
      {errors.enabled && <p role="alert" className="text-sm text-danger sm:col-span-2">{errors.enabled.message}</p>}
      {enabled && <div className="sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input type="checkbox" className="mt-2 shrink-0" {...register("acknowledged")} />我明白這是來源存取授權，不代表金鑰有效、完整覆蓋、已驗證股息或實收。</label>{errors.acknowledged && <p role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>}
      {acceptsCredential(record.policy.provider_id) && record.credential_reference_present && <label className="flex min-h-11 items-start gap-3 text-sm leading-7 sm:col-span-2"><input type="checkbox" className="mt-2 shrink-0" {...register("clear_credential", { onChange: event => { if (event.target.checked) { setValue("enabled", false); setValue("acknowledged", false); } action.changed(); } })} />清除目前金鑰參考並停用；保留舊檔案、歷史與已完成證據，不取消工作。</label>}
    </>}
    <Field label="政策變更依據" maxLength={1000} {...register("reason", { onChange: action.changed })} error={errors.reason?.message} help={record ? `只使用開啟時的版本 ${record.version}；412 會保留輸入，不覆蓋新版。` : "至少 10 個字元；新政策即使已填權利及上限仍保持停用。"} />
  </FormFrame>;
}

export function ProviderCredentialForm({ record, onSaved, onCancel }: { record: ProviderProfile; onSaved: (record: ProviderProfile) => void; onCancel: () => void }) {
  const action = useFormAction(onSaved);
  const { register, handleSubmit, setError, setValue, formState: { errors, isSubmitting } } = useForm({ resolver: zodResolver(z.object({ credential: z.string().regex(exactCredential, "金鑰必須是 1–256 個不含空白的 ASCII 字元；不會裁切或修正。"), reason })), defaultValues: { credential: "", reason: "" } });
  if (!acceptsCredential(record.policy.provider_id)) return <p role="alert">這個來源不接受 API 金鑰。</p>;
  return <FormFrame title="匯入私密金鑰" submitLabel="匯入並儲存停用版本" error={action.error} pending={isSubmitting} onCancel={onCancel}
    onSubmit={handleSubmit(values => action.run(async key => {
      // Only component-scoped form memory and this request hold the value; no mutation/query cache.
      return checked(await api.POST("/api/v1/provider-profiles/{provider_id}/credential", { params: { path: { provider_id: record.policy.provider_id }, header: { ...await mutationHeaders(key), "If-Match": `"${record.version}"` } }, body: values }));
    }))}>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">{providerNames[record.policy.provider_id]}：匯入會建立新的私密版本及停用政策，不會啟用來源。原始值不會回傳；檔案與資料庫不能跨資源原子提交，失敗可能留下未選取檔案，請勿自行刪除或採用。</p>
    <Field label="新私密金鑰" type="password" autoComplete="off" autoCapitalize="none" autoCorrect="off" spellCheck={false} maxLength={257} {...register("credential", { onChange: action.changed })} error={errors.credential?.message} help="原樣送出，不放進網址、快取或離線儲存；未確認的重試保留同一操作識別碼。"
      onPaste={event => { if (!exactCredential.test(event.clipboardData.getData("text/plain"))) { event.preventDefault(); setValue("credential", ""); action.changed(); setError("credential", { message: "貼上的金鑰格式不符；已清空先前輸入，阻止換行移除或截短，請核對原始值。" }); } }} />
    <Field label="金鑰匯入依據" maxLength={1000} {...register("reason", { onChange: action.changed })} error={errors.reason?.message} help={`使用開啟時的版本 ${record.version}；不要在依據中重複金鑰。`} />
  </FormFrame>;
}
