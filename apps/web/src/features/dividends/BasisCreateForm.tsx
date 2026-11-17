import { useCallback, useContext, useId, useRef, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Link } from "react-router";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, failureMessage, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { actionLabels, exactActionVersion, type CorporateAction } from "../ledger/data";
import { date, ratio } from "../ledger/inputs";
import { useFormAction, usePageState } from "../settings/data";
import { validBasisSource } from "./basis-validation";
import { decisionCitation, decisionReason, decisionUrl, matchesDecisionReference } from "./event-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Observation = Schemas["DividendObservationRecord"];
type Basis = Schemas["DividendBasisRecord"];
type Input = Schemas["DividendBasisCreate"];
type ActionPage = Schemas["Page_CorporateAction_"];

const actionSchema = z.object({ id: z.uuid(), root_id: z.uuid(), security_id: z.uuid(), action_type: z.enum(["SPLIT", "REVERSE_SPLIT", "SYMBOL_CHANGE", "CASH_IN_LIEU", "MERGER", "SPIN_OFF"]),
  effective_date: date, ratio_numerator: ratio.nullable(), ratio_denominator: ratio.nullable(), status: z.enum(["CONFIRMED", "NEEDS_REVIEW", "VOID"]),
  lifecycle_status: z.enum(["CURRENT", "VOID"]), is_current: z.literal(true), version: z.number(), version_exact: z.string(),
  effect_scope: z.enum(["QUANTITY_TERMS", "DISPLAY_ONLY", "REVIEW_ONLY", "NONE"]), allocation_scope: z.literal("TERMS_NOT_A_VALUATION"), review_required: z.boolean() });
function inWindow(day: string, ex: string, basis: string) {
  return basis >= ex ? day >= ex && day <= basis : day > basis && day < ex;
}
function checkedActions(page: ActionPage, security: string, ex: string, basis: string): ActionPage {
  const ids = new Set<string>(); const roots = new Set<string>();
  if (!page || !Array.isArray(page.items) || page.items.length > 25 || (page.next_cursor != null && typeof page.next_cursor !== "string")
    || page.items.some(record => {
      if (!actionSchema.safeParse(record).success || record.security_id.toLowerCase() !== security.toLowerCase() || !exactActionVersion(record)
        || record.effective_date < (basis < ex ? basis : ex) || record.effective_date > (basis < ex ? ex : basis)
        || ids.has(record.id.toLowerCase()) || roots.has(record.root_id.toLowerCase()) || (record.status === "VOID") !== (record.lifecycle_status === "VOID")
        || (record.ratio_numerator === null) !== (record.ratio_denominator === null)) return true;
      if (record.action_type === "SPLIT" || record.action_type === "REVERSE_SPLIT") {
        if (record.ratio_numerator === null || record.ratio_denominator === null) return true;
        const numerator = BigInt(record.ratio_numerator); const denominator = BigInt(record.ratio_denominator);
        if (record.action_type === "SPLIT" ? numerator <= denominator : numerator >= denominator) return true;
        if (record.status === "CONFIRMED" && record.effect_scope !== "QUANTITY_TERMS") return true;
      }
      ids.add(record.id.toLowerCase()); roots.add(record.root_id.toLowerCase()); return false;
    })) throw new Error("企業行動清單無法核對；請重新載入或檢查伺服器狀態及有效備份。");
  return page;
}
function selectable(record: CorporateAction, ex: string, basis: string) {
  return (record.action_type === "SPLIT" || record.action_type === "REVERSE_SPLIT") && record.status === "CONFIRMED"
    && record.lifecycle_status === "CURRENT" && record.is_current === true && record.effect_scope === "QUANTITY_TERMS"
    && !!exactActionVersion(record) && inWindow(record.effective_date, ex, basis);
}

function BasisActionPicker({ security, ex, basis, selected, onSelect }: {
  security: string; ex: string; basis: string; selected: CorporateAction[]; onSelect: (record: CorporateAction) => void;
}) {
  const page = usePageState();
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => checkedActions(checked(await api.GET("/api/v1/corporate-actions", { params: { query: {
    security_id: security, from_date: basis < ex ? basis : ex, to_date: basis < ex ? ex : basis, include_history: false, cursor, limit: 25,
  } }, signal })), security, ex, basis), [security, ex, basis]);
  const result = usePrivatePage(`${security}:${ex}:${basis}`, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError;
  return <section aria-label="選擇已記錄企業行動" className="grid min-w-0 gap-4 rounded-md border border-border p-4 sm:col-span-2">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-medium">選擇已記錄企業行動</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => result.refetch()}>重新載入企業行動</Button></div>
    <p className="text-sm leading-7 text-secondary">此處按日期分頁顯示已記錄行動，並非完整市場歷史。只能明確加入範圍內目前已採納的拆股或合股；待覆核、作廢、其他類型及邊界外資料不會自動略成零。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停，不能加入新行動。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入企業行動…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對行動，完成前不能加入。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次載入內容，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入企業行動</Button></div>}
    {result.data && <>{result.data.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有可選企業行動；不代表沒有拆股或完整歷史。</p>
      : <ul className="grid gap-3">{result.data.items.map(record => {
        const used = selected.some(item => item.id.toLowerCase() === record.id.toLowerCase() || item.root_id.toLowerCase() === record.root_id.toLowerCase());
        const allowed = selectable(record, ex, basis);
        return <li key={record.id}><article aria-label={`可選企業行動 ${record.id}`} className="grid min-w-0 gap-2 rounded-md border border-border p-3 text-sm leading-7">
          <p>{record.effective_date}；{actionLabels[record.action_type]}；{record.status}／{record.lifecycle_status}。</p>
          <p className="break-all">行動 {record.id}；穩定事件 {record.root_id}；精確版本 {record.version_exact}。</p>
          <p className="break-all">精確比例：{record.ratio_numerator !== null && record.ratio_denominator !== null ? `${record.ratio_numerator} / ${record.ratio_denominator}` : "未提供，不代表 1:1"}。</p>
          {record.review_required && <p className="text-danger">此行動另有未解決覆核；已記錄條款不是完整歷史或估值。</p>}
          {!allowed && <p className="text-secondary">此行動不能加入本聲明；請核對狀態、類型與日期邊界。</p>}
          <div><Button type="button" variant="outline" disabled={!ready || !allowed || used || selected.length >= 100} onClick={() => onSelect(record)}>{used ? "已列入行動" : "加入此行動"}</Button></div>
        </article></li>;
      })}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={!ready || page.number === 1} onClick={page.previous}>上一頁行動</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆，非全部行動。</p>
        <Button type="button" variant="outline" disabled={!ready || !result.data.next_cursor} onClick={() => { if (result.data?.next_cursor) page.next(result.data.next_cursor); }}>下一頁行動</Button></div></>}
  </section>;
}

const scalarText = (value: string) => Array.from(value).every(character => {
  const point = character.codePointAt(0)!; return point < 0xd800 || point > 0xdfff;
});
const schema = z.object({ share_basis_date: date, citation: decisionCitation.refine(scalarText, "引用參考含有無效的 Unicode 字元。"),
  reason: decisionReason.refine(scalarText, "聲明依據含有無效的 Unicode 字元。"), url: decisionUrl.refine(scalarText, "引用網址含有無效的 Unicode 字元。"),
  complete: z.boolean().refine(Boolean, "請確認已核對完整的已記錄行動範圍。"), split_only: z.boolean().refine(Boolean, "請明確聲明只涉及拆股與合股。"),
  acknowledged: z.boolean().refine(Boolean, "請確認這不是市場、文件或收入驗證。"), empty: z.boolean() });
const resultSchema = z.object({ id: z.uuid(), observation_id: z.uuid(), security_id: z.uuid(), source_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  ex_date: date, share_basis_date: date, source_basis: z.literal("AFTER_EFFECTIVE_DATE_ACTIONS"), target_basis: z.literal("PRE_EX_DATE_EVENTS"),
  complete: z.literal(true), split_only: z.literal(true), actions: z.array(z.object({ action_id: z.uuid(), action_root_id: z.uuid(), security_id: z.uuid(),
    observed_version: z.number(), observed_version_exact: z.string(), action_type: z.enum(["SPLIT", "REVERSE_SPLIT"]), effective_date: date,
    ratio_numerator: ratio, ratio_denominator: ratio, observed_status: z.literal("CONFIRMED"), observed_lifecycle_status: z.literal("CURRENT") })).max(100),
  citation: z.object({ id: z.uuid(), citation: z.string(), reason: z.string(), url: z.string().nullable().optional(), url_withheld: z.boolean() }), created_at: z.string(),
  scope: z.literal("BASIS_DECLARATION_ONLY"), canonical_adoption: z.literal("NOT_ADOPTED"), canonical_adoption_scope: z.literal("RESPONSE_ONLY_NOT_LINKAGE"),
  current_use_scope: z.literal("REVALIDATION_REQUIRED_FOR_NEW_ADOPTION"), market_history: z.literal("OWNER_ASSERTED_NOT_VERIFIED") });
function checkedBasis(record: Basis, source: Observation, body: Input, selected: CorporateAction[]): Basis {
  const validShape = resultSchema.safeParse(record).success;
  const expected = new Map(selected.map(item => [item.id.toLowerCase(), item])); const seen = new Set<string>();
  const valid = validShape && record.observation_id.toLowerCase() === source.id.toLowerCase() && record.security_id.toLowerCase() === source.security_id.toLowerCase()
    && record.source_sha256 === source.normalized_sha256 && record.ex_date === source.ex_date && record.share_basis_date === body.share_basis_date
    && record.citation.id.toLowerCase() === record.id.toLowerCase() && record.citation.reason === body.citation.reason
    && matchesDecisionReference({ citation: record.citation.citation, url: record.citation.url ?? null, url_withheld: record.citation.url_withheld }, body.citation.citation, body.citation.url ?? null)
    && record.actions.length === selected.length && record.actions.every(item => {
      const key = item.action_id.toLowerCase(); const previous = expected.get(key);
      if (!previous || seen.has(key) || item.action_root_id.toLowerCase() !== previous.root_id.toLowerCase() || item.security_id.toLowerCase() !== source.security_id.toLowerCase()
        || item.observed_version_exact !== exactActionVersion(previous) || exactActionVersion({ version: item.observed_version, version_exact: item.observed_version_exact }) !== item.observed_version_exact
        || item.action_type !== previous.action_type || item.effective_date !== previous.effective_date || item.ratio_numerator !== previous.ratio_numerator || item.ratio_denominator !== previous.ratio_denominator) return false;
      seen.add(key); return true;
    });
  if (!valid) throw new Error("伺服器回覆與本次基準聲明不符；請保留輸入並核對歷史，不會假定未儲存。");
  return record;
}

export function BasisCreateForm({ record: observed, onSaved, onCancel }: { record: Observation; onSaved: (record: Basis) => void; onCancel: () => void }) {
  const [source] = useState(() => structuredClone(observed));
  const [selected, setSelected] = useState<CorporateAction[]>([]);
  const action = useFormAction(onSaved); const submitted = useRef<string | null>(null); const close = useContext(EditorCloseContext); const confirmationId = useId();
  const { register, control, setValue, setError, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: {
    share_basis_date: "", citation: "", reason: "", url: "", complete: false, split_only: false, acknowledged: false, empty: false,
  } });
  const basisDate = useWatch({ control, name: "share_basis_date" });
  const ex = source.ex_date ?? "";
  const scopeValid = date.safeParse(basisDate).success;
  const outside = scopeValid && selected.some(item => !selectable(item, ex, basisDate));
  function resetConfirmations() { for (const field of ["complete", "split_only", "acknowledged", "empty"] as const) setValue(field, false); }
  if (!validBasisSource(source)) return <p role="alert" className="text-sm leading-7 text-danger">來源範圍無法安全核對；請關閉並重新載入具除淨日的拆股調整來源。</p>;
  return <FormFrame title="記錄拆股基準聲明" submitLabel="記錄基準聲明" error={action.error} pending={isSubmitting} onCancel={close ?? onCancel} onSubmit={event => void handleSubmit(values => {
    if (selected.some(item => !selectable(item, ex, values.share_basis_date))) return;
    if (!selected.length && !values.empty) { setError("empty", { message: "請明確確認本次不列拆股行動。" }); return; }
    const body: Input = { observation_id: source.id, source_sha256: source.normalized_sha256, share_basis_date: values.share_basis_date,
      source_basis: "AFTER_EFFECTIVE_DATE_ACTIONS", target_basis: "PRE_EX_DATE_EVENTS", complete: true, split_only: true,
      actions: selected.map(item => ({ action_id: item.id, version: exactActionVersion(item)! })).sort((a, b) => a.action_id.localeCompare(b.action_id)),
      citation: { citation: values.citation, reason: values.reason, url: values.url || null } };
    const intent = JSON.stringify(body); if (submitted.current !== null && submitted.current !== intent) action.changed(); submitted.current = intent;
    return action.run(async key => checkedBasis(checked(await api.POST("/api/v1/dividends/basis-assertions", { params: { header: await mutationHeaders(key) }, body })), source, body, selected));
  })(event)}>
    <div className="grid min-w-0 gap-2 text-sm leading-7 text-secondary sm:col-span-2"><p>固定原始來源及除淨日；請自行核對來源所用股數基準與所有相關已記錄行動。這只保存擁有人聲明，不抓取文件，也不採納股息、持倉資格或收入。</p>
      <p className="break-all">來源觀測 {source.id}；證券 {source.security_id}；原始雜湊 {source.normalized_sha256}。</p>
      <p>除淨日 {ex}；來源每股 {source.currency ?? "幣別未知"} {source.dps ?? "未知"}；來源口徑 SPLIT_ADJUSTED。</p>
      <p>回應未確認時請保留視窗，以相同內容及識別碼重試；成功回覆也可能只是歷史重播。</p></div>
    <Field label="來源股數基準日" type="date" {...register("share_basis_date", { onChange: resetConfirmations })} error={errors.share_basis_date?.message} help="請依原文件明確輸入，不使用擷取日或今天代替。基準日當天的行動已包含在來源股數中。" />
    {scopeValid && <BasisActionPicker key={`${source.security_id}:${ex}:${basisDate}`} security={source.security_id} ex={ex} basis={basisDate} selected={selected} onSelect={record => {
      if (!selectable(record, ex, basisDate) || selected.length >= 100 || selected.some(item => item.id.toLowerCase() === record.id.toLowerCase() || item.root_id.toLowerCase() === record.root_id.toLowerCase())) return;
      resetConfirmations(); setSelected(previous => previous.length >= 100 || previous.some(item => item.id.toLowerCase() === record.id.toLowerCase() || item.root_id.toLowerCase() === record.root_id.toLowerCase())
        ? previous : [...previous, structuredClone(record)]);
    }} />}
    <div className="grid gap-3 sm:col-span-2"><p className="text-sm font-medium">已明確選擇的行動：{selected.length} / 100</p>
      {outside && <p role="alert" className="text-sm text-danger">已選行動不屬於目前基準範圍；請逐筆移除或重新核對日期。</p>}
      {selected.length > 0 && <ul aria-label="已選基準行動" className="grid gap-3">{selected.map(item => <li key={item.id} className="grid min-w-0 gap-2 rounded-md border border-border p-3 text-sm leading-7">
        <p className="break-all">行動 {item.id}；生效 {item.effective_date}；精確版本 {exactActionVersion(item)}；精確比例 {item.ratio_numerator} / {item.ratio_denominator}。</p>
        <div><Button type="button" variant="outline" aria-label={`移除已選行動 ${item.id}`} onClick={() => { resetConfirmations(); setSelected(previous => previous.filter(row => row.id !== item.id)); }}>移除此行動</Button></div></li>)}</ul>}
      <Link to="/corporate-actions" target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 items-center text-sm text-primary underline">另開分頁核對企業行動</Link></div>
    <Field label="基準引用參考" maxLength={602} {...register("citation")} error={errors.citation?.message} help="例如文件名稱及頁碼；應用程式不驗證文件。" />
    <Field label="基準引用網址（選填）" maxLength={4096} {...register("url")} error={errors.url?.message} help="只保存參考，不會抓取；查詢或片段在後續讀取時會隱藏。" />
    <Field label="基準聲明依據" maxLength={2002} {...register("reason")} error={errors.reason?.message} help="至少 10 個字元，說明來源股數基準及拆股範圍的人工依據。" />
    <div className="grid gap-3 sm:col-span-2">{([
      ["complete", "我已核對此範圍的已記錄行動，明確聲明所列拆股範圍完整；伺服器仍會重查。"],
      ["split_only", "我明確聲明此範圍只涉及拆股與合股，沒有以空值代替其他股數行動。"],
      ["acknowledged", "我明白這不是市場完整性、文件驗證或收入，也不會自動採納事件。"],
      ...(!selected.length ? [["empty", "我確認本次明確不列拆股行動；這不是缺資料的預設值。"] as const] : []),
    ] as const).map(([field, label]) => <div key={field}><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors[field]} aria-describedby={errors[field] ? `${confirmationId}-${field}` : undefined} {...register(field)} />{label}</label>
      {errors[field] && <p id={`${confirmationId}-${field}`} role="alert" className="text-sm text-danger">{errors[field]?.message}</p>}</div>)}</div>
  </FormFrame>;
}
