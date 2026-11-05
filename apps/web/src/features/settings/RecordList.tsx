import type { ReactNode } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import { Button } from "../../components/ui/button";
import { failureMessage } from "../../lib/api";
import { usePageState } from "./data";

type Page<T> = { items: T[]; next_cursor?: string | null };
export function RecordList<T extends { id: string }>({ title, result, page, children, searchable = true, filtered = false }: {
  title: string; result: UseQueryResult<Page<T>>; page: ReturnType<typeof usePageState>; searchable?: boolean; filtered?: boolean;
  children: (record: T) => ReactNode;
}) {
  return <section aria-label={`${title}列表`} className="grid min-w-0 gap-4">
    <div className="flex flex-wrap items-end gap-3">
      {searchable && <label className="grid min-w-0 flex-1 gap-2 text-sm font-medium">
        搜尋{title}
        <input type="search" className="field" maxLength={100} value={page.q} onChange={event => page.search(event.target.value)} />
      </label>}
      <Button variant="outline" onClick={() => void result.refetch()} disabled={result.isFetching}>重新載入{title}</Button>
    </div>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；目前僅顯示記憶體內已載入的資料。連線恢復前無法儲存。</p>
      : result.isPending ? <div role="status" className="panel text-secondary">正在載入{title}…</div> : null}
    {result.isError && <div role="alert" className="panel text-sm leading-7 text-danger">
      <p>{failureMessage(result.error)}</p>
      {result.data && <p>以下為上次載入的資料，尚未確認最新版本。</p>}
      <Button variant="outline" onClick={() => void result.refetch()}>重試載入{title}</Button>
    </div>}
    {result.data && <>
      {result.data.items.length === 0 ? <div className="panel text-sm leading-7 text-secondary">
        {filtered ? "這個篩選範圍沒有紀錄；請調整日期、搜尋或清除篩選。" : page.q ? "沒有符合搜尋的紀錄；請縮短名稱或清除搜尋。" : `尚未建立${title}；請使用新增按鈕開始。`}
      </div> : <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
        {result.data.items.map(record => <li key={record.id} className="min-w-0 p-4 sm:p-5">{children(record)}</li>)}
      </ul>}
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" onClick={page.previous} disabled={page.number === 1 || result.isFetching}>上一頁</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆（非全部紀錄）</p>
        <Button variant="outline" disabled={!result.data.next_cursor || result.isFetching}
          onClick={() => { if (result.data?.next_cursor) page.next(result.data.next_cursor); }}>下一頁</Button>
      </div>
      <p className="text-xs leading-6 text-secondary">本頁最後載入：{new Date(result.dataUpdatedAt).toLocaleString("zh-HK", { hour12: false })}。不會儲存至瀏覽器離線資料庫。</p>
    </>}
  </section>;
}
