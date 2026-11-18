import { useCallback, useState } from "react";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { usePageState } from "../settings/data";
import { DividendAccountPicker } from "./DividendEstimatePanel";
import { DividendReceiptPreparation } from "./DividendReceiptPreparation";
import type { ReceiptTarget } from "./receipt-input-validation";
import { validEstimateEvent } from "./estimate-validation";
import { checkedReceiptPage } from "./receipt-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Receipt = Schemas["RecordedReceipt"];
const cashLabels: Record<Receipt["cash_scope"], string> = {
  CASH_NOT_RECORDED: "未記錄現金關聯；不是已確認沒有現金。",
  CASH_REVERSAL_NOT_RECORDED: "未記錄現金沖銷；請核對原始紀錄。",
  MATCHING_CASH_RECORDED_UNVERIFIED: "已記錄現金關聯，尚未驗證；不代表付款已確認。",
  MATCHING_CASH_REVERSAL_RECORDED_UNVERIFIED: "已記錄現金沖銷關聯，尚未驗證；不代表沖銷已確認。",
};

function Record({ value }: { value: Receipt }) {
  return <li className="grid min-w-0 gap-2 rounded-md border border-border p-4 text-sm leading-7">
    <h5 className="font-semibold">{value.status === "VOID" ? "已作廢" : "已記錄"}收款；歷史稽核及付款均未驗證。</h5>
    <p>記錄收款日 {value.received_date}；原幣金額 {value.currency} {value.recorded_amount}；{value.received_hkd === null ? "港幣金額未知" : `記錄港幣金額 ${value.received_hkd}`}。</p>
    <p>{value.fx_rate_hkd === null ? "記錄匯率未提供" : `原紀錄港幣匯率 ${value.fx_rate_hkd}`}；來源與實際換匯尚未驗證。</p>
    <p>{value.recorded_tax === null ? "記錄稅額未知，不是零" : `記錄稅額 ${value.currency} ${value.recorded_tax}`}；{value.recorded_fee === null ? "記錄費用未知，不是零" : `記錄費用 ${value.currency} ${value.recorded_fee}`}；稅費欄位僅為已記錄數值，不代表已確認扣款。</p>
    <p>{cashLabels[value.cash_scope]}</p>
    <p className="break-all">原事件修訂 {value.revision_no}／{value.revision_id}；原應收識別碼 {value.entitlement_id}；收款識別碼 {value.id}；版本 {value.version}。</p>
    <p>來源：{value.source === "MANUAL" ? "人工記錄" : "匯入紀錄"}；記錄時間 {value.created_at}；作廢時間 {value.voided_at ?? "未記錄"}。</p>
  </li>;
}

function History({ event, account }: { event: Event; account: Account }) {
  const page = usePageState();
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => checkedReceiptPage(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts", {
      params: { path: { event_id: event.id, account_id: account.id }, query: { cursor, limit: 25 } }, signal,
    }
  )), event, account), [event, account]);
  const result = usePrivatePage(`receipt-history:${event.id}:${event.current_revision_id}:${event.version}:${account.id}`, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError;
  const headChanged = result.data?.items.some(item => item.current_revision_id.toLowerCase() !== event.current_revision_id.toLowerCase());
  return <section aria-label="帳戶收款紀錄歷史" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-semibold">帳戶收款紀錄歷史</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => result.refetch()}>重新載入收款紀錄</Button></div>
    <p className="text-sm leading-7 text-secondary">這是未驗證的歷史記錄，不會確認持倉、收入、付款或應收；不同幣別及歷史版本不在此相加。每頁獨立讀取，內容可能隨時間變更。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；顯示的是上次讀取，並非目前狀態。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入收款紀錄…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對收款紀錄。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次讀取，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入收款紀錄</Button></div>}
    {headChanged && <p role="status" className="break-all text-sm leading-7 text-danger">收款讀取與事件清單的目前版本不同；收款讀取所見修訂 {result.data?.items[0]?.current_revision_id}。兩者不是同一快照，請重新載入事件後核對。</p>}
    {result.data && <>{result.data.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有已記錄的收款；不代表實收為零或歷史完整。</p>
      : <ul className="grid gap-3">{result.data.items.map(value => <Record key={value.id} value={value} />)}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={!ready || page.number === 1} onClick={page.previous}>上一頁收款紀錄</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆，非全部歷史。</p>
        <Button type="button" variant="outline" disabled={!ready || !result.data.next_cursor} onClick={() => { if (result.data?.next_cursor) page.next(result.data.next_cursor); }}>下一頁收款紀錄</Button></div></>}
  </section>;
}

export function DividendReceiptPanel({ event: observed, onWrite, writeDisabled = false, refresh = 0 }: {
  event: Event; onWrite?: (target: ReceiptTarget) => void; writeDisabled?: boolean; refresh?: number;
}) {
  const [event] = useState(() => structuredClone(observed));
  const [account, setAccount] = useState<Account | null>(null); const [preparing, setPreparing] = useState(false);
  if (!validEstimateEvent(event)) return <p role="alert" className="text-sm text-danger">事件版本無法安全核對；請重新載入後再查閱收款紀錄。</p>;
  return <section aria-label="已記錄股息收款" className="grid min-w-0 gap-4 rounded-md border border-border p-4">
    <h3 className="font-semibold">已記錄股息收款</h3>
    <p className="break-all text-sm leading-7 text-secondary">固定事件 {event.id}；版本 {event.version}；實體修訂 {event.current_revision_id}。查閱不會建立或作廢任何紀錄。</p>
    <DividendAccountPicker purpose="收款紀錄" selected={account} onSelect={setAccount} />
    {account && <><p className="break-all text-sm">所選帳戶：{account.name}；{account.id}。</p>
      <History key={`${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${refresh}`} event={event} account={account} />
      {onWrite && <div><Button type="button" variant="outline" aria-expanded={preparing} onClick={() => setPreparing(value => !value)}>{preparing ? "收合收款寫入準備" : "準備記錄收款"}</Button></div>}
      {preparing && onWrite && <DividendReceiptPreparation key={`${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${refresh}`} event={event} account={account} onWrite={onWrite} disabled={writeDisabled} />}</>}
  </section>;
}
