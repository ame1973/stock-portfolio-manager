import { useCallback, useState } from "react";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { usePageState } from "../settings/data";
import { DividendAccountPicker } from "./DividendEstimatePanel";
import { DividendDirectReceiptPreparation } from "./DividendDirectReceiptPreparation";
import type { DirectReceiptTarget } from "./direct-receipt-input-validation";
import { DividendReceiptPreparation } from "./DividendReceiptPreparation";
import { DividendReceiptVoidPreparation } from "./DividendReceiptVoidPreparation";
import { DividendDirectReceiptVoidPreparation } from "./DividendDirectReceiptVoidPreparation";
import type { DirectReceiptVoidTarget } from "./direct-receipt-void-validation";
import { DividendReconciliationPanel } from "./DividendReconciliationPanel";
import type { ReceiptTarget } from "./receipt-input-validation";
import type { ReceiptVoidTarget } from "./receipt-void-validation";
import { validEstimateEvent } from "./estimate-validation";
import { checkedCombinedReceiptPage, sameId } from "./receipt-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Receipt = Schemas["CombinedRecordedReceipt"];
const cashLabels: Record<Receipt["cash_scope"], string> = {
  CASH_NOT_RECORDED: "未記錄現金關聯；不是已確認沒有現金。",
  CASH_REVERSAL_NOT_RECORDED: "未記錄現金沖銷；請核對原始紀錄。",
  MATCHING_CASH_RECORDED_UNVERIFIED: "已記錄現金關聯，尚未驗證；不代表付款已確認。",
  MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED: "已記錄現金沖銷關聯，尚未驗證；不代表沖銷已確認。",
};

function Record({ value, onSelect, disabled, selected }: { value: Receipt; onSelect?: (value: Receipt) => void; disabled: boolean; selected: boolean }) {
  return <li className="grid min-w-0 gap-2 rounded-md border border-border p-4 text-sm leading-7">
    <h5 className="font-semibold">{value.status === "VOID" ? "已作廢" : "已記錄"}收款；{value.audit_scope === "RECORDED_AUDIT_MATCHED_ONLY" ? "保留稽核記錄相符，付款仍未驗證。" : "歷史稽核及付款均未驗證。"}</h5>
    <p>{value.receipt_basis === "DIRECT_EVENT_ACCOUNT" ? "直接關聯事件與帳戶；沒有舊式應收識別碼，不代表已確認應收。" : "依據：舊式應收紀錄；不是已驗證的股息資格。"}</p>
    <p>記錄收款日 {value.received_date}；原幣金額 {value.currency} {value.recorded_amount}；{value.received_hkd === null ? "港幣金額未知" : `記錄港幣金額 ${value.received_hkd}`}。</p>
    <p>{value.fx_rate_hkd === null ? "記錄匯率未提供" : `原紀錄港幣匯率 ${value.fx_rate_hkd}`}；來源與實際換匯尚未驗證。</p>
    <p>{value.recorded_tax === null ? "記錄稅額未知，不是零" : `記錄稅額 ${value.currency} ${value.recorded_tax}`}；{value.recorded_fee === null ? "記錄費用未知，不是零" : `記錄費用 ${value.currency} ${value.recorded_fee}`}；稅費欄位僅為已記錄數值，不代表已確認扣款。</p>
    <p>{cashLabels[value.cash_scope]}</p>
    <p className="break-all">原事件修訂 {value.revision_no}／{value.revision_id}；{value.entitlement_id !== null && <>原應收識別碼 {value.entitlement_id}；</>}收款識別碼 {value.id}；版本 {value.version}。</p>
    <p>來源：{value.source === "MANUAL" ? "人工記錄" : "匯入紀錄"}；記錄時間 {value.created_at}；作廢時間 {value.voided_at ?? "未記錄"}。</p>
    {value.receipt_basis === "DIRECT_EVENT_ACCOUNT" && <p className="text-secondary">直接收款作廢仍保留事件限制；不能使用舊式收款流程。</p>}
    {onSelect && <div><Button type="button" variant="outline" aria-pressed={selected} disabled={disabled || value.status !== "ACTIVE"}
      onClick={() => onSelect(structuredClone(value))}>{value.receipt_basis === "DIRECT_EVENT_ACCOUNT" ? "核對作廢直接收款" : "核對作廢收款"} {value.id}</Button></div>}
  </li>;
}

function sameReceipt(left: Receipt, right: Receipt): boolean {
  const keys = Object.keys(left) as (keyof Receipt)[];
  return keys.length === Object.keys(right).length && keys.every(key => left[key] === right[key]);
}

function History({ event, account, onVoid, disabled }: { event: Event; account: Account; onVoid?: (target: ReceiptVoidTarget | DirectReceiptVoidTarget) => void; disabled: boolean }) {
  const [selected, setSelected] = useState<Receipt | null>(null);
  const page = usePageState();
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => checkedCombinedReceiptPage(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts", {
      params: { path: { event_id: event.id, account_id: account.id }, query: { cursor, limit: 25 } }, signal,
    }
  )), event, account), [event, account]);
  const result = usePrivatePage(`receipt-history:${event.id}:${event.current_revision_id}:${event.version}:${account.id}`, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError;
  const headChanged = result.data?.items.some(item => !sameId(item.current_revision_id, event.current_revision_id));
  const current = selected && result.data?.items.find(item => sameId(item.id, selected.id));
  const selectionMatches = !!selected && !!current && sameReceipt(selected, current);
  const hasDirect = result.data?.items.some(item => item.receipt_basis === "DIRECT_EVENT_ACCOUNT");
  const canSelect = ready && !headChanged && !disabled;
  return <section aria-label="帳戶收款紀錄歷史" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-semibold">帳戶收款紀錄歷史</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => result.refetch()}>重新載入收款紀錄</Button></div>
    <p className="text-sm leading-7 text-secondary">合併顯示直接收款與舊式應收關聯收款。這是未驗證的歷史記錄，不會確認持倉、收入、付款或應收；不同幣別及歷史版本不在此相加。每頁獨立讀取，內容可能隨時間變更。</p>
    {onVoid && <p className="text-sm leading-7 text-secondary">作廢須先核對所選收款的原現金與最新輸入；直接與舊式收款使用不同流程。混合歷史的舊式作廢防護不會因本頁未顯示直接收款而解除。</p>}
    {hasDirect && onVoid && <p role="status" className="text-sm text-secondary">本頁包含直接收款；目前不能從這份混合歷史開始舊式作廢。</p>}
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；顯示的是上次讀取，並非目前狀態。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入收款紀錄…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對收款紀錄。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次讀取，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入收款紀錄</Button></div>}
    {headChanged && <p role="status" className="break-all text-sm leading-7 text-danger">收款讀取與事件清單的目前版本不同；收款讀取所見修訂 {result.data?.items[0]?.current_revision_id}。兩者不是同一快照，請重新載入事件後核對。</p>}
    {result.data && <>{result.data.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有已記錄的收款；不代表實收為零或歷史完整。</p>
      : <ul className="grid gap-3">{result.data.items.map(value => <Record key={value.id} value={value} onSelect={onVoid ? setSelected : undefined}
        disabled={!canSelect || (value.receipt_basis === "LEGACY_ENTITLEMENT" && !!hasDirect)} selected={!!selected && sameId(selected.id, value.id) && selectionMatches} />)}</ul>}
      {selected && !result.isFetching && !result.isError && !selectionMatches && <p role="status" className="text-sm text-danger">所選收款與目前歷史不同；請重新選擇並核對，不會假定先前操作是否完成。</p>}
      {selected && onVoid && (selected.receipt_basis === "DIRECT_EVENT_ACCOUNT"
        ? <DividendDirectReceiptVoidPreparation key={`direct:${selected.id}:${selected.version}`} event={event} account={account} receipt={selected}
          onVoid={onVoid} disabled={!canSelect || !selectionMatches} />
        : <DividendReceiptVoidPreparation key={`legacy:${selected.id}:${selected.version}`} event={event} account={account} receipt={selected}
          onVoid={onVoid} disabled={!canSelect || !selectionMatches || !!hasDirect} />)}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={!ready || page.number === 1} onClick={() => { setSelected(null); page.previous(); }}>上一頁收款紀錄</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆，非全部歷史。</p>
        <Button type="button" variant="outline" disabled={!ready || !result.data.next_cursor} onClick={() => { if (result.data?.next_cursor) { setSelected(null); page.next(result.data.next_cursor); } }}>下一頁收款紀錄</Button></div></>}
  </section>;
}

export function DividendReceiptPanel({ event: observed, onWrite, onVoid, writeDisabled = false, refresh = 0, comparisonRefresh = 0 }: {
  event: Event; onWrite?: (target: ReceiptTarget | DirectReceiptTarget) => void; onVoid?: (target: ReceiptVoidTarget | DirectReceiptVoidTarget) => void; writeDisabled?: boolean; refresh?: number; comparisonRefresh?: number;
}) {
  const [event] = useState(() => structuredClone(observed));
  const [account, setAccount] = useState<Account | null>(null); const [preparing, setPreparing] = useState<"legacy" | "direct" | null>(null);
  const [comparing, setComparing] = useState(false);
  if (!validEstimateEvent(event)) return <p role="alert" className="text-sm text-danger">事件版本無法安全核對；請重新載入後再查閱收款紀錄。</p>;
  return <section aria-label="已記錄股息收款" className="grid min-w-0 gap-4 rounded-md border border-border p-4">
    <h3 className="font-semibold">已記錄股息收款</h3>
    <p className="break-all text-sm leading-7 text-secondary">固定事件 {event.id}；版本 {event.version}；實體修訂 {event.current_revision_id}。查閱不會建立或作廢任何紀錄。</p>
    <DividendAccountPicker purpose="收款紀錄" selected={account} onSelect={value => { setAccount(value); setComparing(false); setPreparing(null); }} />
    {account && <><p className="break-all text-sm">所選帳戶：{account.name}；{account.id}。</p>
      <History key={`receipt-history:${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${refresh}`} event={event} account={account} onVoid={onVoid} disabled={writeDisabled} />
      <div><Button type="button" variant="outline" aria-expanded={comparing} onClick={() => setComparing(value => !value)}>{comparing ? "收合金額比較" : "比較已記錄金額"}</Button></div>
      {comparing && <DividendReconciliationPanel key={`reconciliation:${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${refresh}:${comparisonRefresh}`} event={event} account={account} />}
      {onWrite && <><p className="text-sm leading-7 text-secondary">直接記錄不需要舊式應收；如需沿用現有舊式應收關聯，請另外展開舊式收款準備。兩者都不是付款或資格證明。</p>
        <div className="flex flex-wrap gap-3"><Button type="button" variant="outline" aria-expanded={preparing === "direct"} disabled={writeDisabled} onClick={() => setPreparing(value => value === "direct" ? null : "direct")}>{preparing === "direct" ? "收合直接收款準備" : "準備直接記錄收款"}</Button>
          <Button type="button" variant="outline" aria-expanded={preparing === "legacy"} disabled={writeDisabled} onClick={() => setPreparing(value => value === "legacy" ? null : "legacy")}>{preparing === "legacy" ? "收合收款寫入準備" : "準備記錄收款"}</Button></div></>}
      {preparing === "direct" && onWrite && <DividendDirectReceiptPreparation key={`direct-receipt-preparation:${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${refresh}`} event={event} account={account} onWrite={onWrite} disabled={writeDisabled} />}
      {preparing === "legacy" && onWrite && <DividendReceiptPreparation key={`receipt-preparation:${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${refresh}`} event={event} account={account} onWrite={onWrite} disabled={writeDisabled} />}</>}
  </section>;
}
