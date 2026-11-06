import { useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, mutationHeaders } from "../../lib/api";
import { SecurityName } from "../ledger/ReferenceName";
import { date } from "../ledger/inputs";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { useFormAction } from "../settings/data";
import { networkProviders, providerNames, useProviders, type CreatedJob } from "./data";
import { SourceReadState } from "./details";

const schema = z.object({
  security_ids: z.array(z.uuid()).min(1, "請至少加入一個證券。").max(100, "每個工作最多 100 個證券。"),
  provider_ids: z.array(z.enum(networkProviders)).min(1, "請明確勾選至少一個來源。"),
  from_date: date, to_date: date,
  operations: z.array(z.enum(["DIVIDENDS", "SPLITS"])).min(1, "請選擇至少一項來源操作。"),
  acknowledged: z.boolean().refine(value => value, "請先確認來源觀測不會自動採納為金融事實。"),
}).superRefine((values, context) => {
  if (values.from_date > values.to_date) context.addIssue({ code: "custom", path: ["to_date"], message: "擷取迄日不能早於起日。" });
});

export function SyncForm({ onSaved, onCancel }: { onSaved: (record: CreatedJob) => void; onCancel: () => void }) {
  const action = useFormAction(onSaved);
  const providers = useProviders();
  const [candidate, setCandidate] = useState("");
  const { register, control, setValue, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema), defaultValues: { security_ids: [], provider_ids: [], from_date: "", to_date: "", operations: ["DIVIDENDS"], acknowledged: false },
  });
  const securityIds = useWatch({ control, name: "security_ids" });
  function select(ids: string[]) { setValue("security_ids", ids, { shouldValidate: true }); action.changed(); }
  return <FormFrame title="建立來源擷取工作" submitLabel="建立擷取工作" pendingLabel="正在排隊…" error={action.error} pending={isSubmitting} onCancel={onCancel}
    onSubmit={handleSubmit(values => action.run(async key => {
      if (!providers.data) throw new Error("來源設定診斷尚未載入；請重試確認來源，不會自動選取或啟用。");
      const body = { security_ids: [...values.security_ids].sort(), provider_ids: [...values.provider_ids].sort(), from_date: values.from_date, to_date: values.to_date, operations: [...values.operations].sort(), force: false } satisfies components["schemas"]["SyncDividendRequest"];
      return checked(await api.POST("/api/v1/dividends/sync", { params: { header: await mutationHeaders(key) }, body }));
    }))}>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">明確選擇證券、來源及包含首尾的日期。只建立 SOURCE_ONLY 工作；排隊不等於已擷取、已驗證或已收股息，不會繞過共用配額或修改現金、持股及歷史匯率。</p>
    <div className="grid min-w-0 content-start gap-3 sm:col-span-2">
      <ReferenceSelect kind="securities" label="擷取證券" activeOnly={false} value={candidate} registration={{ onChange: event => setCandidate(event.target.value) }} clearSelection={() => setCandidate("")} />
      <div><Button type="button" variant="outline" disabled={!candidate || securityIds.includes(candidate) || securityIds.length >= 100} onClick={() => { select([...securityIds, candidate]); setCandidate(""); }}>加入證券</Button></div>
      <p className="text-sm leading-7 text-secondary">已加入 {securityIds.length} / 100 個；可擷取下市證券的明確歷史身分。更改候選搜尋不會移除已加入證券。</p>
      {securityIds.length > 0 && <ul aria-label="已加入擷取證券" className="grid gap-3">{securityIds.map(id => <li key={id} className="flex min-w-0 flex-wrap items-center justify-between gap-3 rounded-md border border-border p-3"><span className="min-w-0 break-all text-sm"><SecurityName id={id} /></span><Button type="button" variant="outline" aria-label={`移除此擷取證券 ${id}`} onClick={() => select(securityIds.filter(value => value !== id))}>移除此證券</Button></li>)}</ul>}
      {errors.security_ids?.message && <p role="alert" className="text-sm text-danger">{errors.security_ids.message}</p>}
    </div>
    <Field label="擷取起日" type="date" min="0001-01-01" max="9999-12-31" {...register("from_date", { onChange: action.changed })} error={errors.from_date?.message} />
    <Field label="擷取迄日" type="date" min="0001-01-01" max="9999-12-31" {...register("to_date", { onChange: action.changed })} error={errors.to_date?.message} help="日期是要求範圍，不是已建立的市場覆蓋證明。" />
    <fieldset className="grid min-w-0 gap-3 sm:col-span-2"><legend className="mb-3 text-sm font-medium">明確選擇來源</legend>
      <SourceReadState result={providers} label="來源設定" />
      {providers.data?.items.filter(row => networkProviders.some(id => id === row.provider_id)).map(row => <label key={row.provider_id} className="flex min-h-11 items-start gap-3 rounded-md border border-border p-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" value={row.provider_id} {...register("provider_ids", { onChange: action.changed })} /><span>{providerNames[row.provider_id]}：{row.configured ? "本機已設定，未作即時上游探測" : "未設定／本機不可用，工作可能略過"}<span className="block text-secondary">{row.origin_group}；{row.capabilities.length > 0 ? row.capabilities.join(" / ") : "未宣告可用操作"}。選取不會啟用來源或匯入密鑰。</span></span></label>)}
      {errors.provider_ids?.message && <p role="alert" className="text-sm text-danger">{errors.provider_ids.message}</p>}
      <p className="text-sm leading-7 text-secondary">Yahoo 與 yfinance 共用同一上游，不是兩票；實際權限與配額由伺服器檢查。HKEX／SEC／手動引用需人工證據輸入，這個擷取表單不會捏造文件。</p>
    </fieldset>
    <fieldset className="grid gap-3 sm:col-span-2"><legend className="mb-3 text-sm font-medium">來源操作</legend>
      <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" value="DIVIDENDS" {...register("operations", { onChange: action.changed })} />擷取股息觀測</label>
      <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" value="SPLITS" {...register("operations", { onChange: action.changed })} />擷取拆股因子</label>
      {errors.operations?.message && <p role="alert" className="text-sm text-danger">{errors.operations.message}</p>}
      <p className="text-sm leading-7 text-secondary">拆股因子只保留 PROVIDER_ADJUSTMENT_ONLY，不會採納公司條款或重算股數；不支援的操作由工作明確略過。</p>
    </fieldset>
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input type="checkbox" className="mt-2 shrink-0" {...register("acknowledged")} />我明白這只擷取來源觀測，不是股息驗證、收入認列或實收確認。</label>{errors.acknowledged?.message && <p role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
