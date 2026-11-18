import { useCallback } from "react";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { usePageState } from "../settings/data";
import { checkedReceiptInputs, validReceiptTarget, type ReceiptTarget } from "./receipt-input-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];

export function DividendReceiptPreparation({ event, account, onWrite, disabled = false }: {
  event: Event; account: Account; onWrite: (target: ReceiptTarget) => void; disabled?: boolean;
}) {
  const page = usePageState();
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => checkedReceiptInputs(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts/inputs", {
      params: { path: { event_id: event.id, account_id: account.id }, query: { cursor, limit: 25 } }, signal,
    }
  )), event, account), [event, account]);
  const result = usePrivatePage(`receipt-inputs:${event.id}:${event.current_revision_id}:${event.version}:${account.id}`, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError && !disabled;
  return <section aria-label="核對收款依據" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-semibold">核對收款依據</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => result.refetch()}>重新載入收款依據</Button></div>
    <p className="text-sm leading-7 text-secondary">請明確選擇一筆已記錄舊應收作為關聯；數值及狀態未驗證，不是持倉資格、已確認收入或付款。伺服器寫入時仍會重新核對版本。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；已載入的依據不是目前狀態，不能開始記錄。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入收款依據…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對收款依據。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次載入內容，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入收款依據</Button></div>}
    {result.data && <><p className="break-all text-sm leading-7">事件 {result.data.event_id}；版本 {result.data.revision_no}／{result.data.revision_id}；帳戶 {account.name}／{result.data.account_id}。</p>
      <p className="break-all text-sm leading-7">組合 {result.data.portfolio_id}；券商 {result.data.broker_id}；帳戶版本 {result.data.account_version}；組合版本 {result.data.portfolio_version}；券商版本 {result.data.broker_version}；組合資料版本 {result.data.portfolio_data_revision}。</p>
      {result.data.account_status === "ARCHIVED" && <p role="status" className="text-sm text-danger">此帳戶已封存，只可查看歷史，不能新增收款。</p>}
      {result.data.entitlements.items.length === 0 ? <p className="text-sm leading-7 text-secondary">此頁沒有已記錄舊應收；不代表應收為零。目前新安裝尚不能建立收款所需的舊應收依據。</p>
        : <ul className="grid gap-3">{result.data.entitlements.items.map(item => {
          const target = { event, account, inputs: result.data!, basis: item }; const canWrite = ready && validReceiptTarget(target);
          return <li key={item.id} className="grid min-w-0 gap-2 rounded-md border border-border p-3 text-sm leading-7">
            <p>歷史修訂 {item.revision_no}；記錄股數 {item.recorded_shares}；原記錄預計淨額 {item.currency} {item.recorded_expected_net}。</p>
            <p className="break-all">應收 {item.id}；實體修訂 {item.revision_id}；舊紀錄狀態 {item.recorded_quality}；均未驗證。</p>
            <div><Button type="button" variant="outline" disabled={!canWrite} onClick={() => { if (canWrite) onWrite(structuredClone(target)); }}>選擇應收 {item.id} 並記錄收款</Button></div>
          </li>;
        })}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={!ready || page.number === 1} onClick={page.previous}>上一頁收款依據</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.entitlements.items.length} 筆，非全部歷史。</p>
        <Button type="button" variant="outline" disabled={!ready || !result.data.entitlements.next_cursor} onClick={() => { if (result.data?.entitlements.next_cursor) page.next(result.data.entitlements.next_cursor); }}>下一頁收款依據</Button></div></>}
    <p className="text-sm leading-7 text-secondary">開啟後仍須確認密碼、明確輸入實際收款及各項是否未知，並確認這不證明付款或資格。</p>
  </section>;
}
