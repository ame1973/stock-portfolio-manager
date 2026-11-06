import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { SelectField } from "../../components/forms/fields";
import { FilterChips } from "../../components/forms/filter-chips";
import { Button } from "../../components/ui/button";
import { EditorSheet } from "../../components/ui/editor-sheet";
import { useViewState } from "../../lib/view-state";
import { SecurityName } from "../ledger/ReferenceName";
import { RecordList } from "../settings/RecordList";
import { usePageState } from "../settings/data";
import { ScheduleForm } from "./ScheduleForm";
import { operationLabels, providerNames, refreshSources, scheduleTiming, useSchedule, useScheduleOccurrences, useSchedules, type Schedule, type ScheduleOccurrence, type SchedulePolicy } from "./data";
import { SourceReadState, SourceValues } from "./details";

function PolicyValues({ policy }: { policy: SchedulePolicy }) {
  return <><SourceValues values={[
    ["市場／當地時區", `${policy.market} / ${policy.market === "HK" ? "Asia/Hong_Kong" : "America/New_York"}`],
    ["明確週期／當地時間", scheduleTiming(policy)], ["最早市場日期", policy.first_date],
    ["一般窗口（包含市場終日）", policy.lookback_days == null ? `${policy.lookback_months} 個曆月；同日／月底裁切，不換成固定日數` : `${policy.lookback_days} 日，包含首尾`],
    ["首次回補起日", policy.backfill_from_date ?? "未指定，不推定完整歷史"],
    ["所選來源（不代表已啟用）", policy.provider_ids.map(id => providerNames[id]).join(" / ")],
    ["所選操作", policy.operations.map(id => operationLabels[id]).join(" / ")],
    ["日曆依據", "LOCAL_WEEK_PATTERN_NOT_TRADING_CALENDAR；未驗證假期、提早收市或競價完成"],
  ]} /><ul aria-label="明確排程證券" className="grid gap-2 text-sm">{policy.security_ids.map(id => <li className="break-all" key={id}><SecurityName id={id} /></li>)}</ul></>;
}
function ScheduleSummary({ record }: { record: Schedule }) {
  return <div className="grid min-w-0 gap-4"><h3 className="break-all font-semibold">{record.name}</h3>
    <p className="text-sm leading-7">{record.enabled ? "已啟用：到期允許評估，不代表已排隊或成功擷取。" : "未啟用：不會新增排程評估；已排隊工作不會因此取消。"}</p>
    <PolicyValues policy={record.policy} />
    <SourceValues values={[
      ["下次預定評估（UTC，非完成保證）", record.next_run_at ?? "未排定／停用或日期已到邊界"],
      ["已記錄評估（含略過）", `${record.occurrence_count} 次，不是來源成功或已收收入`],
      ["首次回補是否已排隊", record.initial_backfill_queued ? "已消耗首次排隊；失敗或取消不會自動整段重發" : "尚未排隊或未指定回補，不是已完成歷史"],
      ["自動排程程式", record.automatic_materialization_installed ? "已安裝程式路徑，不是 worker 心跳、來源健康或資料權利保證" : "尚未宣告安裝；已啟用也只是保存意圖"],
      ["最後評估時段／實際記錄時間（UTC）", `${record.last_due_at ?? "尚無紀錄"} / ${record.last_materialized_at ?? "尚無紀錄"}`],
      ["觀察版本／排程識別碼", `${record.version} / ${record.id}`],
    ]} />
    {record.last_job_id && <Link className="inline-flex min-h-11 items-center text-primary underline" to={`/data-center/jobs/${record.last_job_id}`}>查看最後排隊的來源工作</Link>}
  </div>;
}
const savedNotice = (record: Schedule) => `已儲存來源排程版本 ${record.version}，回覆狀態為${record.enabled ? "啟用" : "停用"}；不是擷取完成。請重新載入確認目前進度。`;

export function SchedulesPage() {
  const cache = useQueryClient();
  const page = usePageState("sources:schedules");
  const [filters, setFilters] = useViewState("filters:sources:schedules", { enabled: "" as "" | "true" | "false" });
  const result = useSchedules(page, filters.enabled);
  const [editor, setEditor] = useState<"new" | Schedule | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  function filter(enabled: typeof filters.enabled) { setFilters({ enabled }); page.search(page.q); }
  function saved(record: Schedule) { setEditor(null); setNotice(savedNotice(record)); void refreshSources(cache); }
  return <div className="grid min-w-0 gap-5">
    <div className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-3xl"><h2 className="text-xl font-semibold">來源排程</h2><p className="mt-2 text-sm leading-7 text-secondary">明確設定範圍與市場當地時段；這是週期規則，不是交易所日曆或收市證明。沒有預設啟用，排隊不代表已擷取或已驗證股息。只在重新載入時確認目前進度，不持續輪詢遠期排程。</p></div><Button data-editor-create disabled={editor !== null} onClick={() => setEditor("new")}>建立停用來源排程</Button></div>
    {notice && <p role="status" className="text-sm leading-7">{notice}</p>}
    {editor !== null && <EditorSheet title={editor === "new" ? "建立停用來源排程" : "編輯來源排程"} onClose={() => setEditor(null)}><ScheduleForm record={editor === "new" ? undefined : editor} onSaved={saved} onCancel={() => setEditor(null)} /></EditorSheet>}
    <div className="max-w-xl"><SelectField label="排程啟用狀態" value={filters.enabled} onChange={event => filter(event.target.value as typeof filters.enabled)}><option value="">全部啟用狀態</option><option value="true">已啟用的保存意圖</option><option value="false">停用的保存意圖</option></SelectField></div>
    <FilterChips values={filters} labels={{ enabled: "排程啟用狀態" }} onRemove={() => filter("")} />
    <RecordList title="來源排程" page={page} result={result} filtered={!!filters.enabled} emptyMessage="尚無明確來源排程；請建立停用定義，再另行核對及啟用。不會自動建立全市場工作。" order={{ description: "名稱（SQLite NOCASE）／ID 升序", direction: "ascending" }} columns={[
      { id: "name", header: "排程名稱", cell: ({ row }) => row.original.name },
      { id: "enabled", header: "保存意圖，非來源健康", cell: ({ row }) => row.original.enabled ? "已啟用" : "未啟用" },
      { id: "pattern", header: "市場當地週期", cell: ({ row }) => `${row.original.policy.market} / ${scheduleTiming(row.original.policy)}` },
      { id: "next", header: "下次評估（UTC）", cell: ({ row }) => row.original.next_run_at ?? "未排定／停用或到邊界" },
      { id: "count", header: "已記錄評估，含略過", cell: ({ row }) => String(row.original.occurrence_count) },
    ]}>{record => <article aria-label={record.id} className="grid min-w-0 gap-4"><ScheduleSummary record={record} /><div className="flex flex-wrap gap-3"><Button variant="outline" onClick={() => setEditor(structuredClone(record))}>編輯、啟用或暫停排程</Button><Link className="inline-flex min-h-11 items-center text-sm text-primary underline" to={`/data-center/schedules/${record.id}`}>查看排程評估紀錄</Link></div></article>}</RecordList>
  </div>;
}
function OccurrenceDetails({ record }: { record: ScheduleOccurrence }) {
  return <article className="grid min-w-0 gap-4" aria-label={record.id}>
    <h3 className="font-semibold">評估 {record.sequence}：{record.outcome === "QUEUED" ? "已排隊，未宣稱擷取成功（QUEUED）" : "已有未結束工作，未再次排隊（SKIPPED_IN_FLIGHT）"}</h3>
    <SourceValues values={[
      ["要求日期（非資料覆蓋證明）", `${record.window.start} / ${record.window.end}`], ["窗口種類", record.kind === "INITIAL_BACKFILL" ? "明確首次回補；排隊即消耗，不是已完成" : "一般回看窗口"],
      ["原預定／實際到期時段（UTC）", `${record.expected_run_at} / ${record.due_at}`], ["記錄／下一時段（UTC）", `${record.materialized_at} / ${record.next_run_at ?? "日期已到邊界"}`],
      ["該次觀察版本／評估識別碼", `${record.schedule_version} / ${record.id}`], ["採納狀態", "SOURCE_ONLY / NOT_RUN；不修改實收、現金、持股或匯率"],
    ]} />
    <PolicyValues policy={record.policy} />
    {record.job_id && <Link className="inline-flex min-h-11 items-center text-primary underline" to={`/data-center/jobs/${record.job_id}`}>查看該次排隊的來源工作</Link>}
    {record.blocking_job_id && <Link className="inline-flex min-h-11 items-center text-primary underline" to={`/data-center/jobs/${record.blocking_job_id}`}>查看阻擋中的來源工作</Link>}
  </article>;
}
function Occurrences({ schedule }: { schedule: Schedule }) {
  const page = usePageState(`sources:occurrences:${schedule.id}`);
  const result = useScheduleOccurrences(schedule, page);
  return <RecordList title="排程評估紀錄" page={page} result={result} emptyMessage="尚無排程評估紀錄；不是沒有股息、零收入或已完成擷取。請核對啟用狀態與下一時段。" order={{ description: "到期時段（UTC）／ID 倒序", direction: "descending" }} columns={[
    { id: "due", header: "到期時段（UTC）", cell: ({ row }) => row.original.due_at },
    { id: "sequence", header: "評估序號／觀察版本", cell: ({ row }) => `${row.original.sequence} / ${row.original.schedule_version}` },
    { id: "outcome", header: "評估結果，非來源成功", cell: ({ row }) => row.original.outcome === "QUEUED" ? "已排隊（QUEUED）" : "未再次排隊（SKIPPED_IN_FLIGHT）" },
    { id: "window", header: "要求日期，非資料覆蓋", cell: ({ row }) => `${row.original.window.start} / ${row.original.window.end}` },
  ]}>{record => <OccurrenceDetails record={record} />}</RecordList>;
}
export function ScheduleDetailPage() {
  const { id = "" } = useParams();
  const cache = useQueryClient();
  const result = useSchedule(id);
  const [editor, setEditor] = useState<Schedule | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  return <div className="grid min-w-0 gap-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-xl font-semibold">來源排程與不可變評估紀錄</h2><Link className="inline-flex min-h-11 items-center text-sm text-primary underline" to="/data-center/schedules">返回來源排程列表</Link></div>
    <SourceReadState result={result} label="來源排程" />
    {notice && <p role="status" className="text-sm leading-7">{notice}</p>}
    {editor && <EditorSheet title="編輯來源排程" onClose={() => setEditor(null)}><ScheduleForm record={editor} onCancel={() => setEditor(null)} onSaved={record => { setEditor(null); setNotice(savedNotice(record)); void refreshSources(cache); }} /></EditorSheet>}
    <Button variant="outline" disabled={result.isFetching} onClick={() => void refreshSources(cache)}>重新載入排程與評估紀錄</Button>
    {result.data && <><section aria-label="保存的來源排程" className="panel grid min-w-0 gap-5"><ScheduleSummary record={result.data} /><div><Button variant="outline" onClick={() => setEditor(structuredClone(result.data!))}>編輯、啟用或暫停排程</Button></div></section><Occurrences schedule={result.data} /></>}
  </div>;
}
