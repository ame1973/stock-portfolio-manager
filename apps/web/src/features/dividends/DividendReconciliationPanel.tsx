import { useCallback } from "react";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { checkedReconciliation } from "./reconciliation-validation";
import { sameId } from "./receipt-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Record = Schemas["RecordedReconciliation"];
const states: { [K in Record["state"]]: string } = {
  NO_RECORDED_PROJECTION: "沒有已記錄投影；不代表預計金額為零。",
  STALE_RECORDED_PROJECTION: "投影與事件修訂不同；不能把舊投影當作目前金額。",
  PROJECTION_NOT_COMPARABLE: "投影無法比較；缺少或待核對的金額不是零。",
  CASH_LINK_UNAVAILABLE: "收款或沖銷缺少現金關聯；不能推定已結清。",
  NO_RECORDED_ACTIVE_RECEIPTS: "沒有已記錄的未作廢收款；不代表實收已確認為零。",
  CURRENCY_REVIEW: "收款與投影幣別不同，不能直接相減或猜測匯率。",
  RECORDED_PARTIAL: "已記錄收款金額少於已記錄預計值；不表示差額已確認。",
  RECORDED_RECEIVED: "已記錄金額相等；不代表付款已確認。",
  RECORDED_OVER_RECEIVED: "已記錄金額超過預計值；請核對，不會自動修正。",
  RECORDED_NEEDS_REVIEW: "已記錄金額需要核對，不能認定收入。",
};
const qualities: { [K in NonNullable<Record["projection_quality"]>]: string } = {
  OK: "計算可用，不代表來源或付款已核實。",
  MISSING_QUOTE: "缺少報價，不是零。",
  MISSING_FX: "缺少港幣匯率；不會使用預設匯率。",
  NOT_COMPUTABLE: "無法計算，請核對來源。",
  NEEDS_REVIEW: "需要核對，不能視為已確認。",
};

function Amount({ label, amount, currency }: { label: string; amount: string | null; currency: Record["currency"] }) {
  return <div className="min-w-0"><dt className="text-secondary">{label}</dt>
    <dd className="break-words font-medium">{amount === null || currency === null ? "未提供，不是零" : `${currency} ${amount}`}</dd></div>;
}

export function DividendReconciliationPanel({ event, account }: { event: Event; account: Account }) {
  const load = useCallback(async (_cursor: string | undefined, signal: AbortSignal) => checkedReconciliation(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/reconciliation", {
      params: { path: { event_id: event.id, account_id: account.id } }, signal,
    }
  )), event, account), [event, account]);
  const result = usePrivatePage(`reconciliation:${event.id}:${event.current_revision_id}:${event.version}:${account.id}`, undefined, load);
  const value = result.data;
  const headChanged = value && (!sameId(value.revision_id, event.current_revision_id) || value.revision_no !== event.version);
  return <section aria-label="已記錄股息金額比較" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-semibold">已記錄股息金額比較</h4>
      <Button type="button" variant="outline" disabled={result.isFetching || result.isPaused} onClick={() => result.refetch()}>重新載入金額比較</Button></div>
    <p className="text-sm leading-7 text-secondary">這只比較已記錄的歷史數值，不代表付款、資格或收入已確認；歷史完整性未驗證，使用前仍須重新核對。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">{value ? "連線已暫停；顯示的是上次讀取，並非目前狀態。" : "連線已暫停；尚未載入金額比較，不能推定金額為零。"}</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入金額比較…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">{value ? "正在重新核對；以下是上次讀取，並非目前狀態。" : "正在重新核對金額比較。"}</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {value && <p>以下是上次讀取，尚未重新核對。</p>}
      <Button type="button" variant="outline" disabled={result.isPaused} onClick={() => result.refetch()}>重試載入金額比較</Button></div>}
    {headChanged && <p role="alert" className="text-sm leading-7 text-danger">金額比較與目前載入的事件版本不同；兩者不是同一快照，請重新載入事件後再核對。</p>}
    {value && !headChanged && <div className="grid min-w-0 gap-3 text-sm leading-7">
      <p className="font-medium">{states[value.state]}</p>
      <dl className="grid min-w-0 gap-3 sm:grid-cols-3"><Amount label="已記錄預計淨額" amount={value.recorded_expected} currency={value.currency} />
        <Amount label="已記錄收款" amount={value.recorded_received} currency={value.currency} />
        <Amount label="已記錄差額" amount={value.recorded_outstanding} currency={value.currency} /></dl>
      <p>可比較時，差額為已記錄預計淨額減已記錄收款；不是已確認應收。</p>
      <p>未作廢收款 {value.active_receipts} 筆；已作廢收款 {value.void_receipts} 筆；收款來源稽核未核對 {value.unverified_origins} 筆；缺少現金關聯 {value.unavailable_cash_links} 筆。</p>
      {value.projection_id === null ? <p>沒有已記錄投影識別碼。</p> : <p className="break-all">投影識別碼 {value.projection_id}；版本 {value.projection_no}；投影截至 {value.projection_as_of}；收款不以投影日期篩選。</p>}
      <p>投影品質：{value.projection_quality === null ? "未提供。" : qualities[value.projection_quality]}</p>
      <p>比較品質：{value.comparison_quality === null ? "未完成比較。" : qualities[value.comparison_quality]}</p>
    </div>}
  </section>;
}
