import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { api } from "@spm/contracts";
import { SelectField } from "../../components/forms/fields";
import { FilterChips } from "../../components/forms/filter-chips";
import { Button } from "../../components/ui/button";
import { ConfirmAction } from "../../components/ui/confirm-action";
import { EditorSheet } from "../../components/ui/editor-sheet";
import { checked, mutationHeaders } from "../../lib/api";
import { useViewState } from "../../lib/view-state";
import { SecurityName } from "../ledger/ReferenceName";
import { RecordList } from "../settings/RecordList";
import { usePageState } from "../settings/data";
import { SyncForm } from "./SyncForm";
import { canCancel, itemLabels, jobLabels, operationLabels, providerNames, refreshSources, useJob, useJobItems, useJobs, workLabels, type CreatedJob, type Job, type JobItem } from "./data";
import { SourceReadState, SourceValues } from "./details";

function JobSummary({ job }: { job: Job }) {
  return <div className="grid min-w-0 gap-4"><h3 className="break-all font-semibold">{jobLabels[job.status]}</h3>
    <p className="text-sm leading-7">已處理證券 {job.progress_done} / {job.progress_total}，含失敗或略過，不是成功驗證數。已提交 {job.result.pages_committed} 頁，新增 {job.result.observations_new} 筆來源觀測；不是股息金額或已收收入。</p>
    <SourceValues values={[["工作識別碼", job.id], ["建立時間（UTC）", job.created_at], ["開始／完成時間（UTC）", `${job.started_at ?? "尚未開始"} / ${job.finished_at ?? "尚未結束"}`], ["下次允許處理時間（UTC）", job.next_run_at ?? "未排定／不適用"], ["最近工作原因", job.last_error_code ? workLabels[job.last_error_code] : "尚無錯誤原因"], ["已結束工作項目（含略過）", `${job.result.work_units_done} / ${job.result.work_units}`], ["失敗／不支援略過項目", `${job.result.failures} / ${job.result.skipped_unsupported}`], ["最高項目嘗試輪", `${job.attempt_count} / 4；分頁與執行權不是新輪`], ["上游軟體預留或已記錄要求", `${job.result.provider_attempts}（RESERVED_OR_RECORDED_HTTP），不是供應商帳單`], ["已記錄 HTTP／尚未解決預留", `${job.result.provider_http_recorded} / ${job.result.provider_attempts_unresolved}；未知耗用不退款或重發`]]} />
    <p className="text-sm leading-7 text-secondary">SOURCE_ONLY / NOT_RUN：不做股息驗證、應收／實收採納或金融帳務修改。限流／配額／熔斷期限由 worker 保留；等候狀態不持續輪詢，請按重新載入核對。建立另一工作不會重設共用配額。</p>
  </div>;
}
export function JobsPage() {
  const cache = useQueryClient();
  const page = usePageState("sources:jobs");
  const [filters, setFilters] = useViewState("filters:sources:jobs", { status: "" as "" | Job["status"] });
  const result = useJobs(page, filters.status);
  const [editor, setEditor] = useState(false);
  const [created, setCreated] = useState<CreatedJob | null>(null);
  function filter(status: "" | Job["status"]) { setFilters({ status }); page.search(page.q); }
  return <div className="grid min-w-0 gap-5">
    <div className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-3xl"><h2 className="text-xl font-semibold">來源擷取工作</h2><p className="mt-2 text-sm leading-7 text-secondary">只列目前擁有者建立的來源工作；HTTP 接受排隊不是資料取得成功。已保存來源保留原始證據，不會自動認列收入或改寫實收。</p></div><Button data-editor-create disabled={editor} onClick={() => { setCreated(null); setEditor(true); }}>建立來源擷取工作</Button></div>
    {created && <div role="status" className="grid gap-2 text-sm leading-7"><p>已接受來源擷取工作（HTTP 202）；尚未確認取得資料，也不是已驗證股息。</p><Link className="inline-flex min-h-11 items-center break-all text-primary underline" to={`/data-center/jobs/${created.job_id}`}>查看已接受工作 {created.job_id}</Link></div>}
    {editor && <EditorSheet title="建立來源擷取工作" onClose={() => setEditor(false)}><SyncForm onCancel={() => setEditor(false)} onSaved={record => { setEditor(false); setCreated(record); filter(""); page.reset(); void refreshSources(cache); }} /></EditorSheet>}
    <div className="max-w-xl"><SelectField label="工作狀態" value={filters.status} onChange={event => filter(event.target.value as "" | Job["status"])}><option value="">全部來源工作狀態</option>{(Object.keys(jobLabels) as Job["status"][]).map(status => <option key={status} value={status}>{jobLabels[status]}</option>)}</SelectField></div>
    <FilterChips values={filters} labels={{ status: "工作狀態" }} onRemove={() => filter("")} />
    <RecordList title="來源工作" result={result} page={page} filtered={!!filters.status} emptyMessage="尚未建立來源擷取工作。請明確選擇證券、來源與日期建立工作；不會自動掃描或啟用來源。" order={{ description: "建立時間／ID 倒序", direction: "descending" }} columns={[
      { id: "created", header: "建立時間（UTC）", cell: ({ row }) => row.original.created_at },
      { id: "status", header: "來源工作狀態", cell: ({ row }) => jobLabels[row.original.status] },
      { id: "progress", header: "已處理證券，非成功數", cell: ({ row }) => `${row.original.progress_done} / ${row.original.progress_total}` },
      { id: "pages", header: "已提交來源頁面", cell: ({ row }) => String(row.original.result.pages_committed) },
      { id: "new", header: "新來源觀測，非收入", cell: ({ row }) => String(row.original.result.observations_new) },
    ]}>{job => <article aria-label={job.id} className="grid min-w-0 gap-3"><JobSummary job={job} /><Link className="inline-flex min-h-11 items-center text-sm text-primary underline" to={`/data-center/jobs/${job.id}`}>查看工作明細及取消</Link></article>}</RecordList>
  </div>;
}
function JobItems({ job }: { job: Job }) {
  const page = usePageState(`sources:items:${job.id}`);
  const [filters, setFilters] = useViewState(`filters:sources:items:${job.id}`, { status: "" as "" | JobItem["status"] });
  const result = useJobItems(job, page, filters.status);
  function filter(status: "" | JobItem["status"]) { setFilters({ status }); page.search(page.q); }
  return <section className="grid min-w-0 gap-5" aria-label="逐項來源進度">
    <div className="max-w-xl"><SelectField label="工作項目狀態" value={filters.status} onChange={event => filter(event.target.value as "" | JobItem["status"])}><option value="">全部項目狀態</option>{(Object.keys(itemLabels) as JobItem["status"][]).map(status => <option key={status} value={status}>{itemLabels[status]}</option>)}</SelectField></div>
    <FilterChips values={filters} labels={{ status: "工作項目狀態" }} onRemove={() => filter("")} />
    <RecordList title="來源工作項目" page={page} result={result} filtered={!!filters.status} emptyMessage="此工作尚無可顯示的項目，請重新載入核對；不是成功取得零筆資料。" order={{ description: "項目序號／ID 升序", direction: "ascending" }} columns={[
      { id: "ordinal", header: "項目序號", cell: ({ row }) => String(row.original.ordinal) },
      { id: "security", header: "目錄證券", cell: ({ row }) => <SecurityName id={row.original.security_id} /> },
      { id: "provider", header: "來源／操作", cell: ({ row }) => <>{providerNames[row.original.provider_id]}<span className="block text-secondary">{operationLabels[row.original.operation]}</span></> },
      { id: "state", header: "已記錄項目狀態", cell: ({ row }) => itemLabels[row.original.status] },
      { id: "reason", header: "原因，非股息結論", cell: ({ row }) => row.original.last_error_code ? workLabels[row.original.last_error_code] : "尚無錯誤原因" },
    ]}>{item => <article aria-label={item.id} className="grid min-w-0 gap-3"><h3 className="font-semibold"><SecurityName id={item.security_id} /> · {providerNames[item.provider_id]}</h3><SourceValues values={[["項目狀態／操作", `${itemLabels[item.status]} / ${operationLabels[item.operation]}`], ["工作原因", item.last_error_code ? workLabels[item.last_error_code] : "尚無錯誤原因"], ["項目序號／嘗試輪", `${item.ordinal} / ${item.attempt_count}（最多四輪）`], ["下次允許處理（UTC）", item.next_run_at ?? "未排定／不適用"], ["項目識別碼", item.id]]} /></article>}</RecordList>
  </section>;
}
export function JobDetailPage() {
  const { id = "" } = useParams();
  const cache = useQueryClient();
  const result = useJob(id);
  const [notice, setNotice] = useState<string | null>(null);
  return <div className="grid min-w-0 gap-5"><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-xl font-semibold">來源工作明細</h2><Link to="/data-center/jobs" className="inline-flex min-h-11 items-center text-sm text-primary underline">返回來源工作列表</Link></div>
    <SourceReadState result={result} label="來源工作" />
    {notice && <p role="status" className="text-sm leading-7">{notice}</p>}
    <Button variant="outline" disabled={result.isFetching} onClick={() => void result.refetch()}>重新載入工作狀態</Button>
    {result.data && <><section aria-label="來源工作進度" className="panel grid min-w-0 gap-5"><JobSummary job={result.data} />
      <div><ConfirmAction label="要求取消此工作" title="要求停止尚未完成的來源擷取？" description="取消是停止意圖，不是立即停止保證。已預留要求可能仍在完成；已保存來源與審計保留，不會刪除金融紀錄或退回未知上游耗用。" disabled={!canCancel(result.data.status)} onConfirm={async key => {
        const next = checked(await api.POST("/api/v1/jobs/{id}/cancel", { params: { path: { id }, header: await mutationHeaders(key) } }));
        cache.setQueryData(["sources", "job", id], next);
        setNotice(`取消回覆：${jobLabels[next.status]}。下方狀態會重新讀取；已保存來源與未知上游耗用仍保留。`);
        await refreshSources(cache);
      }} /></div>
    </section><JobItems job={result.data} /></>}
  </div>;
}
