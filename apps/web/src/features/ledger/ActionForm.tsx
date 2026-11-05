import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { useFormAction } from "../settings/data";
import { actionLabels, unallocatedAction, useSecurity, type CorporateAction } from "./data";
import { date, note, optional, ratio, reason } from "./inputs";

const kinds = z.enum(["SPLIT", "REVERSE_SPLIT", "SYMBOL_CHANGE", "CASH_IN_LIEU", "MERGER", "SPIN_OFF"]);
const evidence = z.string().max(2048).refine(value => {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && !Array.from(value).some(character => /\s/.test(character) || character.charCodeAt(0) < 32);
  } catch { return false; }
}, "請使用不含帳密或空白的 HTTP(S) 證據網址；此表單不會抓取網址內容。");
const schema = z.object({
  security_id: z.uuid("請選擇證券。"), action_type: kinds, effective_date: date,
  ratio_numerator: optional(ratio), ratio_denominator: optional(ratio),
  new_symbol: z.string().trim().max(32), evidence_url: optional(evidence), status: z.enum(["NEEDS_REVIEW", "CONFIRMED"]),
  reason, note, confirmed_terms: z.boolean(),
}).superRefine((value, context) => {
  const numerator = value.ratio_numerator, denominator = value.ratio_denominator;
  if (!!numerator !== !!denominator) context.addIssue({ code: "custom", path: ["ratio_denominator"], message: "比例分子與分母必須一併提供，或一併留空。" });
  if (value.action_type === "SPLIT" || value.action_type === "REVERSE_SPLIT") {
    if (!numerator || !denominator) context.addIssue({ code: "custom", path: ["ratio_numerator"], message: "拆合股必須明確提供整數比例；不會假設 1:1。" });
    else if (ratio.safeParse(numerator).success && ratio.safeParse(denominator).success && (value.action_type === "SPLIT" ? BigInt(numerator) <= BigInt(denominator) : BigInt(numerator) >= BigInt(denominator))) context.addIssue({ code: "custom", path: ["ratio_numerator"], message: "拆股分子必須大於分母；合股分子必須小於分母。" });
  }
  if (value.action_type === "SYMBOL_CHANGE" && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/.test(value.new_symbol)) context.addIssue({ code: "custom", path: ["new_symbol"], message: "請明確提供有效新代號，保留前導零、點及其他有效符號。" });
  if (unallocatedAction(value.action_type) && value.status !== "NEEDS_REVIEW") context.addIssue({ code: "custom", path: ["status"], message: "尚未支援的成本／現金分配只能保留待覆核，不能假裝已完成。" });
  if (value.status === "CONFIRMED" && !value.confirmed_terms) context.addIssue({ code: "custom", path: ["confirmed_terms"], message: "請明確確認已核對條款，並理解採納條款不代表完整估值或分配。" });
});

export function ActionForm({ record, onSaved, onCancel }: { record?: CorporateAction; onSaved: (record: CorporateAction) => void; onCancel: () => void }) {
  const action = useFormAction(onSaved);
  const { register, control, setValue, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: {
    security_id: record?.security_id ?? "", action_type: record?.action_type ?? "SPLIT", effective_date: record?.effective_date ?? "",
    ratio_numerator: record?.ratio_numerator ?? "", ratio_denominator: record?.ratio_denominator ?? "", new_symbol: record?.new_symbol ?? "", evidence_url: record?.evidence_url ?? "", status: record?.status === "CONFIRMED" ? "CONFIRMED" : "NEEDS_REVIEW", reason: "", note: record?.note ?? "", confirmed_terms: false,
  } });
  const [securityId, kind, status] = useWatch({ control, name: ["security_id", "action_type", "status"] });
  const security = useSecurity(securityId);
  const unallocated = unallocatedAction(kind);
  return <FormFrame title={record ? "更正企業行動" : "新增企業行動"} error={action.error} pending={isSubmitting} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => {
      const header = await mutationHeaders(key);
      const body = { security_id: values.security_id, action_type: values.action_type, effective_date: values.effective_date,
        ratio_numerator: values.action_type === "SYMBOL_CHANGE" ? null : values.ratio_numerator || null,
        ratio_denominator: values.action_type === "SYMBOL_CHANGE" ? null : values.ratio_denominator || null,
        new_symbol: values.action_type === "SYMBOL_CHANGE" ? values.new_symbol : null,
        evidence_url: values.evidence_url || null, status: values.status, reason: values.reason, note: values.note,
      } satisfies components["schemas"]["CorporateActionCreate"];
      return record
        ? checked(await api.PATCH("/api/v1/corporate-actions/{id}", { params: { path: { id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } }, body }))
        : checked(await api.POST("/api/v1/corporate-actions", { params: { header }, body }));
    }))}>
    {record ? <div className="text-sm leading-7 text-secondary"><input type="hidden" {...register("security_id")} /><p>穩定證券：{security.data ? `${security.data.display_ticker} / ${security.data.exchange_code}` : record.security_id}；更正不能移至另一證券。</p></div>
      : <ReferenceSelect kind="securities" label="企業行動證券" value={securityId} registration={register("security_id")} clearSelection={() => setValue("security_id", "")} error={errors.security_id?.message} />}
    <SelectField label="企業行動類型" {...register("action_type", { onChange: () => { setValue("ratio_numerator", ""); setValue("ratio_denominator", ""); setValue("new_symbol", ""); setValue("status", "NEEDS_REVIEW"); setValue("confirmed_terms", false); } })}>{kinds.options.map(kind => <option key={kind} value={kind}>{actionLabels[kind]}</option>)}</SelectField>
    <Field label="企業行動生效日期" type="date" {...register("effective_date")} error={errors.effective_date?.message} help="使用交易所當地日期。未來顯示代號只能待覆核；已採納的未來股數條款也不會提前改變今日持倉。" />
    <SelectField label="條款狀態" {...register("status", { onChange: () => setValue("confirmed_terms", false) })} error={errors.status?.message}><option value="NEEDS_REVIEW">待覆核，未完成分配</option>{!unallocated && <option value="CONFIRMED">採納條款，不代表完整估值</option>}</SelectField>
    {kind === "SYMBOL_CHANGE" ? <Field label="新顯示代號" {...register("new_symbol")} maxLength={32} error={errors.new_symbol?.message} help="只改目前顯示代號，保留 security_id、交易及識別碼／供應商期間；仍會留下身分映射覆核。" /> : <>
      <Field label="比例分子" inputMode="numeric" {...register("ratio_numerator")} maxLength={19} error={errors.ratio_numerator?.message} help="正 INT64 整數字串，完整保留每一位；例如拆股 3:2 的 3。" />
      <Field label="比例分母" inputMode="numeric" {...register("ratio_denominator")} maxLength={19} error={errors.ratio_denominator?.message} help={unallocated ? "可一併留空；即使有比例也不代表已分配成本、股數或現金。" : "拆股分子大於分母、合股相反；不接受小數、科學記號或猜測 1:1。"} />
    </>}
    <Field label="證據網址（選填）" {...register("evidence_url")} maxLength={2048} error={errors.evidence_url?.message} help="只儲存 HTTP(S) 參考網址，不會抓取內容或宣稱供應商已核實。" />
    <Field label="企業行動依據" {...register("reason")} maxLength={1000} error={errors.reason?.message} />
    <Field label="企業行動備註" {...register("note")} maxLength={1000} error={errors.note?.message} />
    {status === "CONFIRMED" && <div className="grid content-start gap-2 sm:col-span-2"><label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" {...register("confirmed_terms")} />我已核對條款，理解採納不等於完整估值或現金／成本分配</label>{errors.confirmed_terms && <p role="alert" className="text-sm text-danger">{errors.confirmed_terms.message}</p>}</div>}
    <p className="text-sm leading-7 text-secondary sm:col-span-2">{unallocated ? "零碎股現金、合併及分拆沒有已實作的分配流程，只保留真實待覆核證據；不會編造現金、持倉或已實現獲利。" : "比例只描述精確條款；股數精度殘餘或未解決的後續行動仍需覆核。代號變更不會猜測供應商映射，也不能跨過後續採納的代號變更倒退覆寫。"} 所有修訂都保留穩定事件與序號，原始條款不會改寫。</p>
  </FormFrame>;
}
