import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { api } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { Field, FormFrame, SelectField } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { useFormAction, usePageState, type Identifier, type ProviderSymbol, type Security } from "./data";
import { RecordList } from "./RecordList";

type Mapping = Identifier | ProviderSymbol;
type Kind = "identifiers" | "provider-symbols";
const providers = ["eodhd", "alphavantage", "massive", "yahoo", "yfinance", "hkex_evidence", "sec_evidence", "manual"] as const;
const fields = z.object({
  value: z.string().trim().min(1, "請填寫識別碼。").max(100),
  identifier_type: z.enum(["TICKER", "ISIN", "MIC", "OTHER"]), provider_id: z.enum(providers),
  effective_from: z.string().min(1, "請填寫有效起日。"), effective_to: z.string(),
  verified: z.boolean(), reason: z.string().max(1000),
});

function MappingForm({ kind, security, record, onSaved, onCancel }: {
  kind: Kind; security: Security; record?: Mapping; onSaved: (record: Mapping) => void; onCancel: () => void;
}) {
  const provider = kind === "provider-symbols";
  const schema = fields.superRefine((value, context) => {
    if (value.effective_to && value.effective_to < value.effective_from) context.addIssue({ code: "custom", path: ["effective_to"], message: "有效迄日不能早於起日。" });
    if ((record || (provider && value.verified)) && value.reason.trim().length < 10) context.addIssue({ code: "custom", path: ["reason"], message: "更正或人工核實需要至少 10 個字元的依據說明。" });
    if (provider && !/^[A-Za-z0-9^][A-Za-z0-9.^=_-]{0,63}$/.test(value.value)) context.addIssue({ code: "custom", path: ["value"], message: "請填寫供應商原始代碼（最多 64 個字元），不能填入 URL。" });
  });
  const action = useFormAction(onSaved);
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof fields>>({
    resolver: zodResolver(schema), defaultValues: {
      value: record ? ("provider_symbol" in record ? record.provider_symbol : record.identifier_value) : "",
      identifier_type: record && "identifier_type" in record ? record.identifier_type : "TICKER",
      provider_id: record && "provider_id" in record ? record.provider_id as typeof providers[number] : "yahoo",
      effective_from: record?.effective_from ?? "", effective_to: record?.effective_to ?? "", verified: false, reason: "",
    },
  });
  return <FormFrame title={record ? "更正代碼有效期" : provider ? "新增供應商代碼" : "新增識別碼"}
    pending={isSubmitting} error={action.error} onCancel={onCancel} onChange={action.changed}
    onSubmit={handleSubmit(values => action.run(async key => {
      const header = await mutationHeaders(key);
      const period = { effective_from: values.effective_from, effective_to: values.effective_to || null };
      if (record) {
        const params = { path: { id: security.id, mapping_id: record.id }, header: { ...header, "If-Match": `"${record.version}"` } };
        return provider
          ? checked(await api.PATCH("/api/v1/securities/{id}/provider-symbols/{mapping_id}", { params, body: { ...period, reason: values.reason } }))
          : checked(await api.PATCH("/api/v1/securities/{id}/identifiers/{mapping_id}", { params, body: { ...period, reason: values.reason } }));
      }
      const params = { path: { id: security.id }, header };
      return provider
        ? checked(await api.POST("/api/v1/securities/{id}/provider-symbols", { params, body: { ...period, provider_id: values.provider_id, provider_symbol: values.value, verified: values.verified, reason: values.reason } }))
        : checked(await api.POST("/api/v1/securities/{id}/identifiers", { params, body: { ...period, exchange_code: security.exchange_code, identifier_type: values.identifier_type, identifier_value: values.value } }));
    }))}>
    {record ? <p className="break-all text-sm leading-7 text-secondary sm:col-span-2">只更正「{"provider_symbol" in record ? record.provider_symbol : record.identifier_value}」的有效期；代碼與證券歸屬不會被替換。</p> : <>
      {provider ? <SelectField label="資料供應商" {...register("provider_id")}>{providers.map(id => <option key={id} value={id}>{id}</option>)}</SelectField>
        : <SelectField label="識別碼類型" {...register("identifier_type")}><option value="TICKER">交易代碼</option><option value="ISIN">ISIN</option><option value="MIC">MIC</option><option value="OTHER">其他</option></SelectField>}
      <Field label={provider ? "供應商代碼" : "識別碼"} {...register("value")} error={errors.value?.message} autoFocus help={provider ? "使用已查證的原始代碼；不會猜測、發送請求或驗證資料覆蓋。" : `交易所固定為 ${security.exchange_code}；保留前導零及點等字元。`} />
    </>}
    <Field label="有效起日" type="date" {...register("effective_from")} error={errors.effective_from?.message} />
    <Field label="有效迄日（選填）" type="date" {...register("effective_to")} error={errors.effective_to?.message} help="留空代表尚未結束；日期區間包含起日與迄日。" />
    {provider && !record && <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" {...register("verified")} />已人工核實此供應商代碼</label>}
    {(record || provider) && <Field label="更正／核實依據" {...register("reason")} error={errors.reason?.message} help="人工判斷必須保留可審計依據；設定代碼不代表有訂閱權限。" />}
  </FormFrame>;
}

function Mappings({ kind, security }: { kind: Kind; security: Security }) {
  const page = usePageState();
  const cache = useQueryClient();
  const [editor, setEditor] = useState<Mapping | "new" | null>(null);
  const [saved, setSaved] = useState(false);
  const provider = kind === "provider-symbols";
  const title = provider ? "供應商代碼" : "識別碼";
  const result = useQuery<{ items: Mapping[]; next_cursor?: string | null }>({
    queryKey: ["security", security.id, kind, page.q, page.cursor],
    queryFn: async ({ signal }) => provider
      ? checked(await api.GET("/api/v1/securities/{id}/provider-symbols", { params: { path: { id: security.id }, query: { q: page.q, cursor: page.cursor, limit: 25 } }, signal }))
      : checked(await api.GET("/api/v1/securities/{id}/identifiers", { params: { path: { id: security.id }, query: { q: page.q, cursor: page.cursor, limit: 25 } }, signal })),
  });
  return <section aria-label={title} className="grid gap-4 border-t border-border pt-6">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="text-xl font-semibold">{title}</h2>
      <Button disabled={editor !== null} onClick={() => { setSaved(false); setEditor("new"); }}>新增{title}</Button>
    </div>
    {saved && <p role="status" className="text-sm text-success">已儲存{title}；歷史金融事實不會被改寫。</p>}
    {editor !== null && <MappingForm kind={kind} security={security} record={editor === "new" ? undefined : editor}
      onCancel={() => { setEditor(null); void result.refetch(); }} onSaved={() => {
        setEditor(null); setSaved(true); page.reset(); void cache.invalidateQueries({ queryKey: ["security", security.id] });
      }} />}
    <RecordList title={title} result={result} page={page}>{record => <div className="flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
      <div className="min-w-0 flex-1 break-words text-sm leading-7">
        <h3 className="font-semibold">{"provider_symbol" in record ? `${record.provider_id}: ${record.provider_symbol}` : `${record.identifier_type}: ${record.identifier_value}`}</h3>
        <p className="text-secondary">{record.effective_from} 至 {record.effective_to ?? "未結束"}</p>
        {"verified" in record && <p className="text-secondary">{record.verified ? "已人工核實" : "未核實"}</p>}
      </div>
      <Button variant="outline" disabled={editor !== null} onClick={() => { setSaved(false); setEditor(record); }}>更正有效期</Button>
    </div>}</RecordList>
  </section>;
}

export function SecurityDetailPage() {
  const { id = "" } = useParams();
  const security = useQuery({ queryKey: ["security", id], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/securities/{id}", { params: { path: { id } }, signal })) });
  return <div className="grid gap-6">
    <Link to="/settings/securities" className="inline-flex min-h-11 items-center text-sm text-primary underline">返回證券列表</Link>
    <h1 className="text-2xl font-semibold">證券識別與代碼</h1>
    {security.isPending && <p role="status">正在載入證券身分…</p>}
    {security.isError && <div role="alert"><p className="text-danger">{security.error.message}</p><Button variant="outline" onClick={() => void security.refetch()}>重試載入證券</Button></div>}
    {security.data && <>
      <p className="break-words text-lg">{security.data.display_ticker} / {security.data.exchange_code} — {security.data.name}</p>
      <p className="max-w-2xl text-sm leading-7 text-secondary">有效期不可重疊；歷史交易保留穩定證券身分。Yahoo 與 yfinance 屬於同一來源家族，不是兩份獨立證據。</p>
      <Mappings key={`${id}-identifiers`} kind="identifiers" security={security.data} />
      <Mappings key={`${id}-providers`} kind="provider-symbols" security={security.data} />
    </>}
  </div>;
}
