import { useCallback } from "react";
import { api } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { checkedDirectReceiptVoidInputs, validDirectReceiptVoidTarget, type DirectReceiptVoidTarget } from "./direct-receipt-void-validation";
import { sameId } from "./receipt-validation";
import { usePrivatePage } from "./use-private-page";

export function DividendDirectReceiptVoidPreparation({ event, account, receipt, onVoid, disabled = false }: {
  event: DirectReceiptVoidTarget["event"]; account: DirectReceiptVoidTarget["account"]; receipt: DirectReceiptVoidTarget["receipt"];
  onVoid: (target: DirectReceiptVoidTarget) => void; disabled?: boolean;
}) {
  const load = useCallback(async (_cursor: string | undefined, signal: AbortSignal) => {
    if (receipt.receipt_basis !== "DIRECT_EVENT_ACCOUNT") throw new Error("舊式收款不能使用直接收款作廢流程。");
    return checkedDirectReceiptVoidInputs(checked(await api.GET(
      "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts/{receipt_id}/void-inputs", {
        params: { path: { event_id: event.id, account_id: account.id, receipt_id: receipt.id } }, signal,
      }
    )), event, account, receipt);
  }, [event, account, receipt]);
  const result = usePrivatePage(`direct-receipt-void:${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${receipt.id}:${receipt.version}`, undefined, load);
  const target = result.data ? { event, account, receipt, inputs: result.data } : null;
  const ready = !!target && !result.isFetching && !result.isPaused && !result.isError && !disabled && validDirectReceiptVoidTarget(target);
  const current = result.data?.current; const original = result.data?.receipt;
  return <section aria-label="核對直接收款作廢輸入" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-semibold">核對直接收款作廢輸入</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => result.refetch()}>重新載入直接作廢輸入</Button></div>
    <p className="text-sm leading-7 text-secondary">這與事件及收款歷史是分開讀取；伺服器寫入時會再次核對。只處理所選直接收款，不會建立應收或證明付款、歷史持倉及股息資格。</p>
    <p className="text-sm leading-7 text-danger">直接收款的作廢也不會解除事件修訂、撤回及拒絕限制；目前沒有支援的解除流程。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；上次載入內容不是目前狀態，不能開始作廢。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入直接作廢輸入…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對直接作廢輸入。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次載入內容，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入直接作廢輸入</Button></div>}
    {result.data && current && original && <><p className="break-all text-sm leading-7">收款 {original.id}；版本 {original.version}；原修訂 {original.revision_no}／{original.revision_id}；原現金流水 {result.data.cash_entry_id}。</p>
      <p className="text-sm leading-7">原收款 {original.currency} {original.recorded_amount}；{original.received_hkd === null ? "港幣金額未知" : `港幣金額 ${original.received_hkd}`}；{original.fx_rate_hkd === null ? "匯率未知" : `原記錄匯率 ${original.fx_rate_hkd}`}。</p>
      <p className="break-all text-sm leading-7">目前事件修訂 {current.revision_no}／{current.revision_id}；帳戶／組合／券商版本 {current.account_version}／{current.portfolio_version}／{current.broker_version}；資料版本 {current.portfolio_data_revision}。</p>
      {original.status === "VOID" && <p role="status" className="text-sm text-danger">此直接收款已作廢，不能再次沖銷。</p>}
      {current.portfolio_data_revision === "9223372036854775807" && <p role="status" className="text-sm text-danger">組合資料版本已達上限，不能新增作廢。</p>}
      {current.account_status === "ARCHIVED" && <p role="status" className="text-sm text-secondary">帳戶已封存；符合來源核對時仍可更正已記錄收款。</p>}
      {!sameId(result.data.origin_portfolio_id, current.portfolio_id) && <p role="status" className="break-all text-sm text-danger">原組合 {result.data.origin_portfolio_id} 與目前組合 {current.portfolio_id} 不同；作廢會更新兩者。</p>}
      <div><Button type="button" variant="outline" disabled={!ready} onClick={() => { if (target && ready) onVoid(structuredClone(target)); }}>繼續核對並作廢直接收款</Button></div></>}
    <p className="text-sm leading-7 text-secondary">繼續後仍須確認密碼、填寫作廢依據並明確確認新增反向現金流水；原現金及歷史匯率不會重新估價。</p>
  </section>;
}
