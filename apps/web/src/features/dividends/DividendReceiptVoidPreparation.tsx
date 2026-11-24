import { useCallback } from "react";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { checkedReceiptVoidInputs, validReceiptVoidTarget, type ReceiptVoidTarget } from "./receipt-void-validation";
import { sameId } from "./receipt-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Receipt = Schemas["RecordedReceipt"];

export function DividendReceiptVoidPreparation({ event, account, receipt, onVoid, disabled = false }: {
  event: Event; account: Account; receipt: Receipt; onVoid: (target: ReceiptVoidTarget) => void; disabled?: boolean;
}) {
  const load = useCallback(async (_cursor: string | undefined, signal: AbortSignal) => checkedReceiptVoidInputs(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/receipts/{receipt_id}/void-inputs", {
      params: { path: { event_id: event.id, account_id: account.id, receipt_id: receipt.id } }, signal,
    }
  )), event, account, receipt), [event, account, receipt]);
  const result = usePrivatePage(`receipt-void:${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${receipt.id}:${receipt.version}`, undefined, load);
  const target = result.data ? { event, account, receipt, inputs: result.data } : null;
  const ready = !!target && !result.isFetching && !result.isPaused && !result.isError && !disabled && validReceiptVoidTarget(target);
  return <section aria-label="核對收款作廢輸入" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-semibold">核對收款作廢輸入</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => result.refetch()}>重新載入作廢輸入</Button></div>
    <p className="text-sm leading-7 text-secondary">這與收款歷史及事件清單是分開讀取；伺服器寫入時會再次核對。來源相符不證明付款、歷史持倉或股息資格。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；上次載入內容不是目前狀態，不能開始作廢。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入作廢輸入…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對作廢輸入。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次載入內容，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入作廢輸入</Button></div>}
    {result.data && <><p className="break-all text-sm leading-7">收款 {result.data.receipt_id}；版本 {result.data.receipt_version}；事件 {result.data.event_id}；目前修訂 {result.data.observed_revision_no}／{result.data.observed_revision_id}。</p>
      <p className="text-sm leading-7">原收款 {result.data.currency} {result.data.received_amount}；{result.data.received_hkd === null ? "港幣金額未知" : `港幣金額 ${result.data.received_hkd}`}；{result.data.fx_rate_hkd === null ? "匯率未知" : `原記錄匯率 ${result.data.fx_rate_hkd}`}。</p>
      {result.data.audit_scope === "RECORDED_AUDIT_MATCHED_ONLY" ? <p className="text-sm leading-7">已記錄來源與現金稽核相符；這不是實際付款或原始操作者的證明。</p>
        : <p role="status" className="text-sm text-danger">此收款缺少可核對來源；目前不能透過此流程作廢，請核對原始紀錄。</p>}
      {result.data.status === "VOID" && <p role="status" className="text-sm text-danger">此收款已作廢，不能再次沖銷。</p>}
      {result.data.portfolio_data_revision === "9223372036854775807" && <p role="status" className="text-sm text-danger">組合資料版本已達上限，不能新增作廢。</p>}
      {result.data.account_status === "ARCHIVED" && <p role="status" className="text-sm text-secondary">帳戶已封存；符合來源核對時仍可更正已記錄收款。</p>}
      {result.data.origin_portfolio_id && !sameId(result.data.origin_portfolio_id, result.data.portfolio_id)
        && <p role="status" className="break-all text-sm text-danger">原組合 {result.data.origin_portfolio_id} 與目前組合 {result.data.portfolio_id} 不同；作廢會更新兩者。</p>}
      <div><Button type="button" variant="outline" disabled={!ready} onClick={() => { if (target && ready) onVoid(structuredClone(target)); }}>繼續核對並作廢收款</Button></div></>}
    <p className="text-sm leading-7 text-secondary">繼續後仍須確認密碼、填寫作廢依據並明確確認新增反向現金流水。</p>
  </section>;
}
