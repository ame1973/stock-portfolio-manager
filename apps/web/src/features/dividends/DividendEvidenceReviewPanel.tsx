import { useCallback } from "react";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { ApiRequestError, checked } from "../../lib/api";
import { usePageState } from "../settings/data";
import { validEstimateEvent } from "./estimate-validation";
import { termLabels } from "./event-labels";
import { checkedEvidenceReviewPage } from "./evidence-review-validation";
import { sameId } from "./receipt-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Review = Schemas["EvidenceReviewRecord"];

function ReviewItem({ value }: { value: Review }) {
  return <li className="grid min-w-0 gap-2 rounded-md border border-border p-4 text-sm leading-7">
    <h4 className="font-semibold">{value.reason_code === "DIVIDEND_SOURCE_CONFLICT" ? "來源條款衝突" : "資料不完整"}</h4>
    <p>{value.status === "OPEN" ? "已記錄待核對。" : value.status === "ACKNOWLEDGED"
      ? "已記錄為知悉；知悉未經驗證，不代表問題已處理。" : "已記錄後繼版本處理；不是文件驗證或股息對帳。"}</p>
    <p className="break-all">審核識別碼 {value.id}；原事件修訂 {value.revision_no}／{value.revision_id}。</p>
    <p className="break-all">{value.portfolio_id === null ? "此紀錄未限定投資組合。" : `投資組合識別碼 ${value.portfolio_id}。`}</p>
    <p>衝突欄位：{value.conflicting_fields.length ? value.conflicting_fields.map(field => termLabels[field]).join("、") : "未列明"}。</p>
    <p className="break-all">不完整原因代碼：{value.incomplete_reasons.join("、") || "未列明"}。</p>
    <p className="break-all">記錄時間 {value.created_at}；{value.resolved_at === null ? "未記錄處理時間" : `已記錄處理時間 ${value.resolved_at}；後繼修訂 ${value.resolved_by_revision_id}`}。</p>
  </li>;
}

function History({ event }: { event: Event }) {
  const page = usePageState();
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => checkedEvidenceReviewPage(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/evidence-reviews", {
      params: { path: { event_id: event.id }, query: { cursor, limit: 25 } }, signal,
    }
  )), event), [event]);
  const result = usePrivatePage(`evidence-reviews:${event.id}:${event.current_revision_id}:${event.version}`, page.cursor, load);
  const value = result.data;
  const headChanged = !!value && (!sameId(value.revision_id, event.current_revision_id) || value.revision_no !== event.version);
  const ready = !!value && !result.isFetching && !result.isPaused && !result.isError && !headChanged;
  const error = result.error instanceof ApiRequestError ? result.error.message : result.error instanceof TypeError
    ? "網絡連線中斷，請檢查連線後重試。" : "股息證據審核範圍無法安全核對；請重新載入或檢查有效備份。";
  return <section aria-label="股息證據審核歷史" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-semibold">股息證據審核歷史</h3>
      <Button type="button" variant="outline" disabled={result.isFetching || result.isPaused} onClick={() => result.refetch()}>重新載入證據審核</Button></div>
    <p className="text-sm leading-7 text-secondary">只顯示保留的審核記錄；歷史完整性未驗證，這不是文件驗證或持股資格。每頁獨立讀取，不保證屬於同一快照。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；請重新連線後再核對，本頁不顯示舊資料。</p>
      : result.isFetching ? <p role="status" className="text-sm text-secondary">正在核對證據審核歷史…</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{error}</p>
      <Button type="button" variant="outline" disabled={result.isPaused} onClick={() => result.refetch()}>重試載入證據審核</Button></div>}
    {headChanged && <p role="alert" className="text-sm leading-7 text-danger">審核讀取與事件清單的版本不是同一快照；請重新載入事件後核對，本頁不顯示紀錄。</p>}
    {ready && value && <>{value.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有已記錄的證據審核；不代表歷史完整或問題已處理。</p>
      : <ul className="grid gap-3">{value.items.map(item => <ReviewItem key={item.id} value={item} />)}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={page.number === 1} onClick={() => page.previous()}>上一頁證據審核</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {value.items.length} 筆，非全部歷史。</p>
        <Button type="button" variant="outline" disabled={!value.next_cursor} onClick={() => { if (value.next_cursor) page.next(value.next_cursor); }}>下一頁證據審核</Button></div></>}
  </section>;
}

export function DividendEvidenceReviewPanel({ event }: { event: Event }) {
  if (!validEstimateEvent(event)) return <p role="alert" className="text-sm text-danger">事件版本無法安全核對；請重新載入後再查閱證據審核。</p>;
  return <History key={`${event.id}:${event.current_revision_id}:${event.version}`} event={event} />;
}
