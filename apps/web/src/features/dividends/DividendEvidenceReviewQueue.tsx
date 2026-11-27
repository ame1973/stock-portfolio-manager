import { useCallback, useState } from "react";
import { api, type components } from "@spm/contracts";
import { SelectField } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { ApiRequestError, checked } from "../../lib/api";
import { usePageState } from "../settings/data";
import { checkedEvidenceReviewQueue, type EvidenceReviewFilters } from "./evidence-review-validation";
import { usePrivatePage } from "./use-private-page";

type Review = components["schemas"]["EvidenceReviewQueueRecord"];

export function DividendEvidenceReviewQueue({ onOpenEvent, disabled = false }: { onOpenEvent: (eventId: string) => void; disabled?: boolean }) {
  const page = usePageState();
  const [status, setStatus] = useState<"" | Review["status"]>("");
  const [severity, setSeverity] = useState<"" | NonNullable<EvidenceReviewFilters["severity"]>>("");
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => checkedEvidenceReviewQueue(checked(await api.GET(
    "/api/v1/dividends/evidence-reviews", { params: { query: { cursor, limit: 25, status: status || undefined, severity: severity || undefined } }, signal }
  )), 25, { status: status || undefined, severity: severity || undefined }), [status, severity]);
  const result = usePrivatePage(`evidence-review-queue:${status}:${severity}`, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError;
  const error = result.error instanceof ApiRequestError ? result.error.message : result.error instanceof TypeError
    ? "網絡連線中斷，請檢查連線後重試。" : "跨事件證據審核無法安全核對；請重新載入或檢查有效備份。";

  return <section aria-label="跨事件證據審核" className="grid min-w-0 gap-4 rounded-lg border border-border bg-surface p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-lg font-semibold">跨事件證據審核</h2>
      <Button type="button" variant="outline" disabled={result.isFetching || result.isPaused} onClick={() => result.refetch()}>重新載入跨事件審核</Button></div>
    <p className="text-sm leading-7 text-secondary">只列已記錄的來源衝突與資料不完整；收款及重建核對不在此清單。歷史完整性未驗證，每頁分開讀取，不代表付款或持股資格已確認。</p>
    <div className="grid gap-4 sm:grid-cols-2">
      <SelectField label="跨事件審核狀態" value={status} onChange={event => { setStatus(event.target.value as typeof status); page.reset(); }}>
        <option value="">全部狀態</option><option value="OPEN">待核對</option><option value="ACKNOWLEDGED">已記錄知悉（未驗證）</option><option value="RESOLVED">已記錄後繼修訂處理</option>
      </SelectField>
      <SelectField label="跨事件審核嚴重度" value={severity} onChange={event => { setSeverity(event.target.value as typeof severity); page.reset(); }}
        help="目前證據審核只記錄為警告；其他類型不在此清單。">
        <option value="">全部嚴重度</option><option value="INFO">資訊</option><option value="WARNING">警告</option><option value="CRITICAL">嚴重</option>
      </SelectField>
    </div>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；重新連線後再核對，不顯示舊資料。</p>
      : result.isFetching ? <p role="status" className="text-sm text-secondary">正在讀取跨事件證據審核…</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{error}</p>
      <Button type="button" variant="outline" disabled={result.isPaused} onClick={() => result.refetch()}>重試跨事件審核</Button></div>}
    {ready && result.data && <>{result.data.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有已記錄的證據審核；不代表所有問題已處理或歷史完整。</p>
      : <ul className="divide-y divide-border">{result.data.items.map(item => <li key={item.id} className="grid gap-2 py-4 text-sm leading-7">
        <h3 className="font-semibold">{item.reason_code === "DIVIDEND_SOURCE_CONFLICT" ? "來源條款衝突" : "資料不完整"}；嚴重度：警告</h3>
        <p>{item.status === "OPEN" ? "已記錄待核對。" : item.status === "ACKNOWLEDGED" ? "已記錄知悉；知悉未經驗證。" : "已記錄後繼版本處理；不代表文件或付款已確認。"}</p>
        <p className="break-all">事件 {item.event_id}；原修訂 {item.revision_no}／{item.revision_id}；讀取時事件修訂 {item.current_revision_no}／{item.current_revision_id}。</p>
        <p>記錄時間 {item.created_at}；使用前仍須重新核對。</p>
        <div><Button type="button" variant="outline" disabled={disabled} onClick={() => onOpenEvent(item.event_id)}>查看此事件</Button></div>
      </li>)}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={page.number === 1} onClick={() => page.previous()}>上一頁跨事件審核</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆，非全部歷史。</p>
        <Button type="button" variant="outline" disabled={!result.data.next_cursor} onClick={() => { if (result.data?.next_cursor) page.next(result.data.next_cursor); }}>下一頁跨事件審核</Button></div></>}
  </section>;
}
