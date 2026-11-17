import { useCallback, useState } from "react";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { date } from "../ledger/inputs";
import { usePageState } from "../settings/data";
import { checkedEstimateRejection, checkedFee, checkedHistory, checkedTax, validEstimateEvent, validEstimateTarget, type EstimateTarget } from "./estimate-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Tax = Schemas["TaxSettingSelection"];
type Fee = Schemas["FeeSettingSelection"];
type Setting = Schemas["TaxSettingRecord"] | Schemas["FeeSettingRecord"];
type Page<T> = { items: T[]; next_cursor?: string | null };
const accountSchema = z.object({ id: z.uuid(), portfolio_id: z.uuid(), broker_id: z.uuid(), name: z.string().min(1).max(100),
  default_currency: z.enum(["HKD", "USD"]), status: z.enum(["ACTIVE", "ARCHIVED"]) });
function checkedAccounts(page: Page<Account>): Page<Account> {
  const ids = new Set<string>();
  if (!page || !Array.isArray(page.items) || page.items.length > 25 || (page.next_cursor != null && typeof page.next_cursor !== "string")
    || page.items.some(item => {
      if (!accountSchema.safeParse(item).success || ids.has(item.id.toLowerCase())) return true;
      ids.add(item.id.toLowerCase()); return false;
    })) throw new Error("帳戶清單無法安全核對；請重新載入或檢查有效備份。");
  return page;
}

export function DividendAccountPicker({ selected, onSelect, purpose = "估算" }: { selected: Account | null; onSelect: (record: Account) => void; purpose?: "估算" | "投影" | "收款紀錄" }) {
  const page = usePageState();
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => checkedAccounts(checked(await api.GET("/api/v1/accounts", { params: { query: { cursor, limit: 25 } }, signal }))), []);
  const result = usePrivatePage(`dividend-${purpose}-accounts`, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError;
  return <section aria-label={`選擇${purpose}帳戶`} className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-medium">選擇帳戶</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => result.refetch()}>重新載入{purpose}帳戶</Button></div>
    <p className="text-sm leading-7 text-secondary">這只選擇已記錄帳戶；不代表持有此證券、符合股息資格或收到款項。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停，不能選擇新帳戶。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入帳戶…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對帳戶。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次載入內容，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入{purpose}帳戶</Button></div>}
    {result.data && <>{result.data.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有帳戶；不代表沒有持倉或股息。</p>
      : <ul className="grid gap-3">{result.data.items.map(record => <li key={record.id} className="grid min-w-0 gap-2 rounded-md border border-border p-3 text-sm leading-7">
        <p>{record.name}；帳戶預設幣別 {record.default_currency}；{record.status === "ARCHIVED" ? "已封存，仍可查閱歷史" : "有效帳戶"}。</p>
        <p className="break-all">帳戶 {record.id}；組合 {record.portfolio_id}；券商 {record.broker_id}。</p>
        <div><Button type="button" variant="outline" disabled={!ready || selected?.id.toLowerCase() === record.id.toLowerCase()} onClick={() => onSelect(structuredClone(record))}>
          {selected?.id.toLowerCase() === record.id.toLowerCase() ? `已選此${purpose}帳戶` : `選擇${purpose}帳戶 ${record.name}`}</Button></div></li>)}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={!ready || page.number === 1} onClick={page.previous}>上一頁{purpose}帳戶</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆，非全部帳戶。</p>
        <Button type="button" variant="outline" disabled={!ready || !result.data.next_cursor} onClick={() => { if (result.data?.next_cursor) page.next(result.data.next_cursor); }}>下一頁{purpose}帳戶</Button></div></>}
  </section>;
}

type ReadResult<T> = ReturnType<typeof usePrivatePage<T>>;
function ReadStatus<T>({ result, label }: { result: ReadResult<T>; label: string }) {
  return <>{result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；已載入內容不是目前狀態。</p>
    : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入{label}…</p>
      : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對{label}。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次載入的歷史內容，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入{label}</Button></div>}</>;
}

function History({ kind, event, account }: { kind: "tax" | "fee"; event: Event; account: Account }) {
  const page = usePageState(); const label = kind === "tax" ? "稅率估算歷史" : "費用估算歷史";
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal): Promise<Page<Setting>> => {
    const params = { path: { event_id: event.id, account_id: account.id }, query: { revision_id: event.current_revision_id, revision_no: event.version, cursor, limit: 25 } };
    const result = kind === "tax" ? checked(await api.GET("/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings/history", { params, signal }))
      : checked(await api.GET("/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings/history", { params, signal }));
    return checkedHistory<Setting>(result, event, account, kind);
  }, [kind, event, account]);
  const result = usePrivatePage(`${kind}:${event.id}:${event.current_revision_id}:${event.version}:${account.id}`, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError;
  return <section aria-label={label} className="grid min-w-0 gap-3 rounded-md border border-border p-4"><h5 className="font-medium">{label}</h5>
    <p className="text-sm text-secondary">歷史與上方選擇是分開讀取，可能包含較新的設定；不是已確認扣款或收入。</p><ReadStatus result={result} label={label} />
    {result.data && <>{result.data.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有已記錄的估算；不代表費用或稅率為零。</p>
      : <ul className="grid gap-3">{result.data.items.map(record => <li key={record.id} className="grid min-w-0 gap-1 rounded-md border border-border p-3 text-sm leading-7">
        <p className="break-all">設定 {record.setting_no}；識別碼 {record.id}；生效日 {record.effective_from}。</p>
        <p>{"tax_status" in record ? record.tax_status === "UNKNOWN" ? "稅率未知，不是零。" : `稅率估算 ${record.rate}，不是已確認扣稅。`
          : record.fee_status === "UNKNOWN" ? `金額未知；幣別 ${record.fee_currency ?? "未知"}。` : `費用估算 ${record.fee_currency} ${record.fee}，不是已確認扣費。`}</p>
        <p>記錄依據：{record.reason}</p><p className="break-all text-secondary">組合 {record.portfolio_id}；券商 {record.broker_id}；帳戶版本 {record.account_version}；券商版本 {record.broker_version}。</p>
      </li>)}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={!ready || page.number === 1} onClick={page.previous}>上一頁{label}</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆，非全部歷史。</p>
        <Button type="button" variant="outline" disabled={!ready || !result.data.next_cursor} onClick={() => { if (result.data?.next_cursor) page.next(result.data.next_cursor); }}>下一頁{label}</Button></div></>}
  </section>;
}

function WritePreparation({ event, account, tax, fee, onWrite, disabled, refresh }: {
  event: Event; account: Account; tax: ReadResult<Tax>; fee: ReadResult<Fee>;
  onWrite: (target: EstimateTarget) => void; disabled: boolean; refresh: number;
}) {
  const load = useCallback(async (_cursor: string | undefined, signal: AbortSignal) => checkedEstimateRejection(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/rejection", { params: { path: { event_id: event.id } }, signal }
  )), event), [event]);
  const rejection = usePrivatePage(`estimate-rejection:${event.id}:${event.current_revision_id}:${event.version}:${refresh}`, undefined, load);
  const ready = !!rejection.data && !rejection.isFetching && !rejection.isPaused && !rejection.isError && !disabled;
  const taxTarget: EstimateTarget | null = rejection.data && tax.data ? { kind: "tax", event, account, selection: tax.data, rejection: rejection.data } : null;
  const feeTarget: EstimateTarget | null = rejection.data && fee.data ? { kind: "fee", event, account, selection: fee.data, rejection: rejection.data } : null;
  const taxReady = ready && !tax.isFetching && !tax.isPaused && !tax.isError && !!taxTarget && validEstimateTarget(taxTarget);
  const feeReady = ready && !fee.isFetching && !fee.isPaused && !fee.isError && !!feeTarget && validEstimateTarget(feeTarget);
  return <section aria-label="估算寫入前的拒絕狀態" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-medium">再次核對事件拒絕狀態</h4>
      <Button type="button" variant="outline" disabled={rejection.isFetching} onClick={() => rejection.refetch()}>重新載入估算拒絕狀態</Button></div>
    <p className="text-sm leading-7 text-secondary">此讀取與事件及稅費不是同一快照；伺服器在寫入時仍會重查。若讀取失敗、離線或範圍變更，不能開始新設定。</p>
    <ReadStatus result={rejection} label="估算拒絕狀態" />
    {rejection.data && <p className="text-sm leading-7">{rejection.data.state === "REJECTED" ? "此事件版本已記錄拒絕，不能新增估算。" : "此讀取未記錄拒絕；不是接受決定或財務資格。"}</p>}
    <div className="flex flex-wrap gap-3"><Button type="button" variant="outline" disabled={!taxReady} onClick={() => { if (taxTarget && taxReady) onWrite(structuredClone(taxTarget)); }}>記錄稅率估算</Button>
      <Button type="button" variant="outline" disabled={!feeReady} onClick={() => { if (feeTarget && feeReady) onWrite(structuredClone(feeTarget)); }}>記錄費用估算</Button></div>
  </section>;
}

function SelectionDetails({ event, account, effective, onWrite, writeDisabled, refresh }: {
  event: Event; account: Account; effective: string; onWrite?: (target: EstimateTarget) => void; writeDisabled: boolean; refresh: number;
}) {
  const [taxHistory, setTaxHistory] = useState(false); const [feeHistory, setFeeHistory] = useState(false); const [preparing, setPreparing] = useState(false);
  const taxLoad = useCallback(async (_cursor: string | undefined, signal: AbortSignal): Promise<Tax> => checkedTax(checked(await api.GET("/api/v1/dividends/events/{event_id}/accounts/{account_id}/tax-settings", { params: {
    path: { event_id: event.id, account_id: account.id }, query: { effective_on: effective },
  }, signal })), event, account, effective), [event, account, effective]);
  const feeLoad = useCallback(async (_cursor: string | undefined, signal: AbortSignal): Promise<Fee> => checkedFee(checked(await api.GET("/api/v1/dividends/events/{event_id}/accounts/{account_id}/fee-settings", { params: {
    path: { event_id: event.id, account_id: account.id }, query: { effective_on: effective },
  }, signal })), event, account, effective), [event, account, effective]);
  const scope = `${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${effective}:${refresh}`;
  const tax = usePrivatePage(`tax:${scope}`, undefined, taxLoad); const fee = usePrivatePage(`fee:${scope}`, undefined, feeLoad);
  return <div className="grid min-w-0 gap-4"><p className="text-sm leading-7 text-secondary">稅率與拒絕狀態不是同一讀取；費用、稅率與事件也不是同一快照。下列是擁有人估算，不確認實際扣款、持倉資格或收入。</p>
    <section aria-label="稅率估算" className="grid min-w-0 gap-3 rounded-md border border-border p-4"><div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-medium">稅率估算</h4><Button type="button" variant="outline" disabled={tax.isFetching} onClick={() => tax.refetch()}>重新載入稅率估算</Button></div>
      <p className="text-sm text-secondary">稅率以小數表示；1 代表 100%，不代表已確認扣稅。</p>
      <ReadStatus result={tax} label="稅率估算" />{tax.data && <><p className="text-sm leading-7">{tax.data.tax_status === "MISSING" ? "未設定，不是零。" : tax.data.tax_status === "UNKNOWN" ? "稅率未知，不是零。" : `稅率估算 ${tax.data.selected?.rate}；不是已確認扣稅。`}</p>
        {tax.data.selected_scope === "ACCOUNT_SCOPE_CHANGED" && <p role="status" className="text-sm text-danger">帳戶或券商範圍已變更；原估算不能視為目前有效。</p>}
        <p className="break-all text-sm leading-7">觀察到設定頂端 {tax.data.head_no}；識別碼 {tax.data.head_id ?? "沒有設定"}；帳戶版本 {tax.data.account_version}；券商版本 {tax.data.broker_version}。</p>
        {tax.data.selected && <p className="break-all text-sm leading-7">所選設定 {tax.data.selected.setting_no}；生效日 {tax.data.selected.effective_from}；依據 {tax.data.selected.reason}。</p>}
        <div><Button type="button" variant="outline" aria-expanded={taxHistory} onClick={() => setTaxHistory(value => !value)}>{taxHistory ? "收合稅率估算歷史" : "查看稅率估算歷史"}</Button></div>
        {taxHistory && <History key={`tax:${scope}`} kind="tax" event={event} account={account} />}</>}</section>
    <section aria-label="費用估算" className="grid min-w-0 gap-3 rounded-md border border-border p-4"><div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-medium">費用估算</h4><Button type="button" variant="outline" disabled={fee.isFetching} onClick={() => fee.refetch()}>重新載入費用估算</Button></div>
      <ReadStatus result={fee} label="費用估算" />{fee.data && <><p className="text-sm leading-7">{fee.data.fee_status === "MISSING" ? "未設定，不是零。" : fee.data.fee_status === "UNKNOWN" ? `金額未知；幣別 ${fee.data.selected?.fee_currency ?? "未知"}。` : `費用估算 ${fee.data.selected?.fee_currency} ${fee.data.selected?.fee}；不是已確認扣費。`}</p>
        <p className="text-sm leading-7">{fee.data.revision_rejection === "REJECTED" ? "此讀取顯示事件版本已拒絕；不是實收沖銷。" : "此讀取顯示事件版本未記錄拒絕；不是接受決定或財務資格。"}</p>
        {fee.data.selected_scope === "ACCOUNT_SCOPE_CHANGED" && <p role="status" className="text-sm text-danger">帳戶或券商範圍已變更；原估算不能視為目前有效。</p>}
        <p className="break-all text-sm leading-7">觀察到設定頂端 {fee.data.head_no}；識別碼 {fee.data.head_id ?? "沒有設定"}；帳戶版本 {fee.data.account_version}；券商版本 {fee.data.broker_version}。</p>
        {fee.data.selected && <p className="break-all text-sm leading-7">所選設定 {fee.data.selected.setting_no}；生效日 {fee.data.selected.effective_from}；依據 {fee.data.selected.reason}。</p>}
        <div><Button type="button" variant="outline" aria-expanded={feeHistory} onClick={() => setFeeHistory(value => !value)}>{feeHistory ? "收合費用估算歷史" : "查看費用估算歷史"}</Button></div>
        {feeHistory && <History key={`fee:${scope}`} kind="fee" event={event} account={account} />}</>}</section>
    {onWrite && <div><Button type="button" variant="outline" aria-expanded={preparing} onClick={() => setPreparing(value => !value)}>{preparing ? "收合估算寫入核對" : "核對拒絕狀態以記錄估算"}</Button></div>}
    {preparing && onWrite && <WritePreparation event={event} account={account} tax={tax} fee={fee} onWrite={onWrite} disabled={writeDisabled} refresh={refresh} />}
  </div>;
}

export function DividendEstimatePanel({ event: observed, onWrite, writeDisabled = false, refresh = 0 }: {
  event: Event; onWrite?: (target: EstimateTarget) => void; writeDisabled?: boolean; refresh?: number;
}) {
  const [event] = useState(() => structuredClone(observed));
  const [account, setAccount] = useState<Account | null>(null); const [effective, setEffective] = useState("");
  if (!validEstimateEvent(event)) return <p role="alert" className="text-sm text-danger">事件版本無法安全核對；請重新載入後再查閱估算。</p>;
  return <section aria-label="帳戶稅費估算" className="grid min-w-0 gap-4 rounded-md border border-border p-4"><h3 className="font-semibold">帳戶稅費估算</h3>
    <p className="break-all text-sm leading-7 text-secondary">固定事件 {event.id}；版本 {event.version}；實體修訂 {event.current_revision_id}。查閱不會建立持倉、應收、實收或換匯。</p>
    <DividendAccountPicker selected={account} onSelect={setAccount} />
    {account && <><p className="break-all text-sm">所選帳戶：{account.name}；{account.id}。</p><Field label="查閱估算生效日" type="date" value={effective} onChange={change => setEffective(change.target.value)} help="請明確選擇查閱日期，不由除淨日或今天自動推定。" />
      {date.safeParse(effective).success && <SelectionDetails key={`${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${effective}`} event={event} account={account} effective={effective} onWrite={onWrite} writeDisabled={writeDisabled} refresh={refresh} />}</>}
  </section>;
}
