import { useCallback, useState } from "react";
import { api, type components } from "@spm/contracts";
import { Field, SelectField } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { date } from "../ledger/inputs";
import { checkedFee, checkedTax } from "./estimate-validation";
import { checkedProjectionInputs, validProjectionTarget, type ProjectionQuery, type ProjectionTarget } from "./projection-input-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type ReadResult<T> = ReturnType<typeof usePrivatePage<T>>;

function ReadStatus<T>({ result, label }: { result: ReadResult<T>; label: string }) {
  return <>{result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；已載入的{label}不是目前狀態。</p>
    : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入{label}…</p>
      : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對{label}。</p> : null}
    {result.isError && <p role="alert" className="text-sm text-danger">{failureMessage(result.error)}{result.data ? " 已顯示的資料尚未重新核對。" : ""}</p>}</>;
}

function Preflight({ event, account, query, onWrite, disabled }: {
  event: Event; account: Account; query: ProjectionQuery; onWrite: (target: ProjectionTarget) => void; disabled: boolean;
}) {
  const inputsLoad = useCallback(async (_cursor: string | undefined, signal: AbortSignal) => checkedProjectionInputs(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projection-inputs", { params: { path: { event_id: event.id, account_id: account.id }, query }, signal }
  )), event, account, query), [event, account, query]);
  const taxLoad = useCallback(async (_cursor: string | undefined, signal: AbortSignal) => checkedTax(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings", { params: { path: { event_id: event.id, account_id: account.id }, query: { effective_on: query.tax_effective_on } }, signal }
  )), event, account, query.tax_effective_on), [event, account, query]);
  const feeLoad = useCallback(async (_cursor: string | undefined, signal: AbortSignal) => checkedFee(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings", { params: { path: { event_id: event.id, account_id: account.id }, query: { effective_on: query.fee_effective_on } }, signal }
  )), event, account, query.fee_effective_on), [event, account, query]);
  const scope = `${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${query.as_of}:${query.eligibility_rule}:${query.tax_effective_on}:${query.fee_effective_on}`;
  const inputs = usePrivatePage(`projection-inputs:${scope}`, undefined, inputsLoad);
  const tax = usePrivatePage(`projection-tax:${scope}`, undefined, taxLoad);
  const fee = usePrivatePage(`projection-fee:${scope}`, undefined, feeLoad);
  const values = inputs.data && tax.data && fee.data ? { event, account, inputs: inputs.data, tax: tax.data, fee: fee.data, query } : null;
  const available = [inputs, tax, fee].every(result => !!result.data && !result.isFetching && !result.isPaused && !result.isError);
  const ready = !disabled && available && values !== null && validProjectionTarget(values);
  return <section aria-label="投影寫入前核對" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-semibold">核對投影輸入</h4>
      <Button type="button" variant="outline" disabled={inputs.isFetching || tax.isFetching || fee.isFetching} onClick={() => { inputs.refetch(); tax.refetch(); fee.refetch(); }}>重新載入投影輸入</Button></div>
    <p className="text-sm leading-7 text-secondary">投影輸入是一份快照；稅率及費用明細分別讀取，並非同一快照。只有範圍及設定一致時才能開啟；伺服器寫入時仍會重新核對。</p>
    <ReadStatus result={inputs} label="投影輸入" /><ReadStatus result={tax} label="稅率估算" /><ReadStatus result={fee} label="費用估算" />
    {inputs.data && <><p className="break-all text-sm leading-7">事件 {inputs.data.event_id}；版本 {inputs.data.revision_no}／{inputs.data.revision_id}；帳戶 {account.name}／{inputs.data.account_id}。</p>
      <p className="break-all text-sm leading-7">組合 {inputs.data.portfolio_id}；券商 {inputs.data.broker_id}；帳戶版本 {inputs.data.account_version}；券商版本 {inputs.data.broker_version}；組合資料版本 {inputs.data.portfolio_data_revision}。</p>
      <p className="break-all text-sm leading-7">投影頂端 {inputs.data.projection_head_no}／{inputs.data.projection_head_id ?? "沒有投影"}；{inputs.data.rejection_state === "REJECTED" ? "此版本已記錄拒絕，不能新增投影。" : "此讀取未記錄拒絕，並非接受決定或財務資格。"}</p></>}
    {tax.data && <p className="break-all text-sm leading-7">稅率查閱日 {tax.data.effective_on}：{tax.data.tax_status === "MISSING" ? "未設定，不是零" : tax.data.tax_status === "UNKNOWN" ? "未知，不是零" : `估算小數 ${tax.data.selected?.rate}，不是已確認扣稅`}；設定頂端 {tax.data.head_no}／{tax.data.head_id ?? "沒有"}。</p>}
    {fee.data && <p className="break-all text-sm leading-7">費用查閱日 {fee.data.effective_on}：{fee.data.fee_status === "MISSING" ? "未設定，不是零" : fee.data.fee_status === "UNKNOWN" ? `金額未知，幣別 ${fee.data.selected?.fee_currency ?? "未知"}` : `估算 ${fee.data.selected?.fee_currency} ${fee.data.selected?.fee}，不是已確認扣費`}；設定頂端 {fee.data.head_no}／{fee.data.head_id ?? "沒有"}。</p>}
    {available && values && !validProjectionTarget(values) && <p role="alert" className="text-sm leading-7 text-danger">讀取範圍不一致、已拒絕、帳戶設定已變更或投影版本已達上限；請重新載入事件、帳戶及輸入，不可開始新投影。</p>}
    <div><Button type="button" variant="outline" disabled={!ready} onClick={() => { if (ready && values) onWrite(structuredClone(values)); }}>開啟記錄投影</Button></div>
    <p className="text-sm leading-7 text-secondary">開啟後仍須確認密碼、明確選擇匯率是否提供，並分別確認不是財務資格以及事件修訂限制。</p>
  </section>;
}

export function DividendProjectionPreparation({ event, account, onWrite, disabled = false }: {
  event: Event; account: Account; onWrite: (target: ProjectionTarget) => void; disabled?: boolean;
}) {
  const [asOf, setAsOf] = useState(""); const [rule, setRule] = useState<"" | ProjectionQuery["eligibility_rule"]>("");
  const [taxDay, setTaxDay] = useState(""); const [feeDay, setFeeDay] = useState(""); const [query, setQuery] = useState<ProjectionQuery | null>(null);
  const valid = date.safeParse(asOf).success && date.safeParse(taxDay).success && date.safeParse(feeDay).success && rule !== "";
  return <section aria-label="準備記錄股息投影" className="grid min-w-0 gap-4 rounded-md border border-border p-4">
    <h4 className="font-semibold">準備記錄股息投影</h4>
    <p className="text-sm leading-7 text-secondary">請明確選擇查閱日、規則及兩個估算生效日；不會自動使用今天、除淨日或帳戶預設值。</p>
    <div className="grid gap-4 sm:grid-cols-2"><Field label="投影查閱日" type="date" value={asOf} onChange={change => { setAsOf(change.target.value); setQuery(null); }} />
      <SelectField label="投影規則" value={rule} onChange={change => { setRule(change.target.value as typeof rule); setQuery(null); }}>
        <option value="">請選擇</option><option value="ORDINARY_PRE_EX_DATE">一般除淨日前記錄</option><option value="REQUIRES_REVIEW">需要另行核對</option></SelectField>
      <Field label="投影稅率估算生效日" type="date" value={taxDay} onChange={change => { setTaxDay(change.target.value); setQuery(null); }} />
      <Field label="投影費用估算生效日" type="date" value={feeDay} onChange={change => { setFeeDay(change.target.value); setQuery(null); }} /></div>
    <div><Button type="button" variant="outline" disabled={!valid || disabled || query !== null} onClick={() => { if (valid && rule) setQuery({ as_of: asOf, eligibility_rule: rule, tax_effective_on: taxDay, fee_effective_on: feeDay }); }}>核對投影輸入</Button></div>
    {query && <Preflight key={JSON.stringify(query)} event={event} account={account} query={query} onWrite={onWrite} disabled={disabled} />}
  </section>;
}
