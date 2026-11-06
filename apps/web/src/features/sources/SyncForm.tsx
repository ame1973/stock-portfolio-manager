import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { date } from "../ledger/inputs";
import { useFormAction } from "../settings/data";
import { SourceOperations, SourceProviders, SourceSecurities } from "./SourceInputs";
import { networkProviders, useProviders, type CreatedJob } from "./data";

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
    <SourceSecurities label="擷取證券" ids={securityIds} onChange={select} error={errors.security_ids?.message} />
    <Field label="擷取起日" type="date" min="0001-01-01" max="9999-12-31" {...register("from_date", { onChange: action.changed })} error={errors.from_date?.message} />
    <Field label="擷取迄日" type="date" min="0001-01-01" max="9999-12-31" {...register("to_date", { onChange: action.changed })} error={errors.to_date?.message} help="日期是要求範圍，不是已建立的市場覆蓋證明。" />
    <SourceProviders result={providers} registration={register("provider_ids", { onChange: action.changed })} error={errors.provider_ids?.message} />
    <SourceOperations registration={register("operations", { onChange: action.changed })} error={errors.operations?.message} />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input type="checkbox" className="mt-2 shrink-0" {...register("acknowledged")} />我明白這只擷取來源觀測，不是股息驗證、收入認列或實收確認。</label>{errors.acknowledged?.message && <p role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
