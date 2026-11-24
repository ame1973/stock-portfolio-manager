import { useCallback } from "react";
import { api } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { checkedDirectReceiptInputs, validDirectReceiptTarget, type DirectReceiptTarget } from "./direct-receipt-input-validation";
import { usePrivatePage } from "./use-private-page";

export function DividendDirectReceiptPreparation({ event, account, onWrite, disabled = false }: {
  event: DirectReceiptTarget["event"]; account: DirectReceiptTarget["account"];
  onWrite: (target: DirectReceiptTarget) => void; disabled?: boolean;
}) {
  const load = useCallback(async (_cursor: string | undefined, signal: AbortSignal) => checkedDirectReceiptInputs(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/recorded-receipts/inputs", {
      params: { path: { event_id: event.id, account_id: account.id } }, signal,
    }
  )), event, account), [event, account]);
  const scope = JSON.stringify(["direct-receipt-inputs", event.id, event.current_revision_id, event.version, event.workflow_status,
    account.id, account.portfolio_id, account.broker_id, account.status, account.version]);
  const result = usePrivatePage(scope, undefined, load);
  const target = result.data ? { event, account, inputs: result.data } : null;
  const ready = !!target && !result.isFetching && !result.isPaused && !result.isError && !disabled && validDirectReceiptTarget(target);
  return <section aria-label="核對直接收款輸入" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-semibold">核對直接收款輸入</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => result.refetch()}>重新載入直接收款輸入</Button></div>
    <p className="text-sm leading-7 text-secondary">直接關聯目前事件與所選帳戶，不需要或建立舊式應收，不會帶入預設金額或匯率。這與事件及收款歷史是分開讀取；伺服器寫入時仍會重新核對。</p>
    <p className="text-sm leading-7 text-danger">直接收款會持續阻擋事件修訂、撤回及拒絕；作廢也不會解除限制，目前沒有支援的解除流程。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；先前輸入不是目前狀態，不能開始記錄。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入直接收款輸入…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對直接收款輸入。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次載入內容，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入直接收款輸入</Button></div>}
    {result.data && <><p className="break-all text-sm leading-7">事件 {result.data.event_id}；修訂 {result.data.revision_no}／{result.data.revision_id}；帳戶 {account.name}／{result.data.account_id}。</p>
      <p className="break-all text-sm leading-7">組合 {result.data.portfolio_id}；券商 {result.data.broker_id}；帳戶／組合／券商版本 {result.data.account_version}／{result.data.portfolio_version}／{result.data.broker_version}；組合資料版本 {result.data.portfolio_data_revision}。</p>
      {result.data.rejection_state === "REJECTED" && <p role="status" className="text-sm text-danger">目前事件版本已記錄拒絕；不能新增直接收款。</p>}
      {result.data.workflow_status === "WITHDRAWN" && <p role="status" className="text-sm text-danger">事件已撤回；不能新增直接收款。</p>}
      {result.data.account_status === "ARCHIVED" && <p role="status" className="text-sm text-danger">帳戶已封存；不能新增直接收款。</p>}
      {result.data.portfolio_data_revision === "9223372036854775807" && <p role="status" className="text-sm text-danger">組合資料版本已達上限；不能新增直接收款。</p>}
      <div><Button type="button" variant="outline" disabled={!ready} onClick={() => { if (target && ready) onWrite(structuredClone(target)); }}>核對後記錄直接收款</Button></div></>}
    <p className="text-sm leading-7 text-secondary">繼續後仍須確認密碼，明確輸入現金及各項是否未知，並分別確認非付款／資格證明及事件限制。</p>
  </section>;
}
