import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, type components } from "@spm/contracts";
import { SelectField } from "../../components/forms/fields";
import { FilterChips } from "../../components/forms/filter-chips";
import { Button } from "../../components/ui/button";
import { EditorSheet } from "../../components/ui/editor-sheet";
import { checked } from "../../lib/api";
import { useDesktop } from "../../lib/use-desktop";
import { useViewState } from "../../lib/view-state";
import { SecurityName } from "../ledger/ReferenceName";
import { RecordList } from "../settings/RecordList";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { PasswordForm } from "../settings/SecuritySettingsPage";
import { usePageState } from "../settings/data";
import { SourceValues } from "../sources/details";
import { basisLabels, eventTypeLabels, termLabels } from "./event-labels";
import { CandidateCreateForm, CandidateRevisionForm } from "./CandidateForm";
import { CandidateEventCreateForm } from "./CandidateEventCreateForm";
import { validCandidate, validCandidateForEvent } from "./candidate-validation";

type Candidate = components["schemas"]["CandidateRecord"];
type Field = components["schemas"]["CandidateAssessment"]["conflicting_fields"][number];

function verifiedPage<T extends { items: Candidate[]; next_cursor?: string | null }>(page: T, scope?: { id: string; security: string }): T {
  const ids = new Set<string>();
  const revisions = new Set<string>();
  let previousVersion = 2_147_483_648;
  if (!page || !Array.isArray(page.items) || page.items.some(record => {
    if (!validCandidate(record) || revisions.has(record.revision_id)) return true;
    if (scope && (record.id !== scope.id || record.terms.security_id !== scope.security || record.version >= previousVersion)) return true;
    if (!scope && ids.has(record.id)) return true;
    ids.add(record.id); revisions.add(record.revision_id); previousVersion = record.version;
    return false;
  })) throw new Error("候選資料無法核對；請重新載入或檢查伺服器狀態及有效備份。");
  return page;
}

const states: Record<Candidate["state"], string> = { OPEN: "待審核", DISMISSED: "已標記排除，歷史保留" };
const assessments: Record<Candidate["assessment"]["status"], string> = {
  PROVISIONAL: "暫定", CORROBORATED: "來源條款相符，非文件驗證", CONFLICT: "來源條款有衝突", INCOMPLETE: "條款不完整",
};
const names = (items: Field[]) => items.length ? items.map(item => termLabels[item]).join("、") : "未列明";
const amount = (terms: Candidate["terms"]) => `${terms.currency ?? "幣別未知"} ${terms.dps ?? "未知"}`;

function CandidateDetails({ record }: { record: Candidate }) {
  const { terms, assessment } = record;
  return <div className="grid min-w-0 gap-5">
    <p className="text-sm leading-7 text-secondary">候選評估與來源關聯是已記錄的審閱依據，不是文件驗證、持倉資格或收入。回應標記不是目前或過往事件關聯查詢，不能據此判斷候選是否已被關聯。</p>
    <SourceValues values={[
      ["穩定候選識別碼", record.id], ["本版／實體修訂識別碼", `${record.version} / ${record.revision_id}`],
      ["事件類型", eventTypeLabels[terms.event_type]], ["調整基準", basisLabels[terms.adjustment_basis]],
      ["宣告／記錄／付款日期", `${terms.announce_date ?? "未知"} / ${terms.record_date ?? "未知"} / ${terms.pay_date ?? "未知"}`],
      ["本版記錄時間（UTC）", record.created_at], ["審閱依據", record.change_reason],
    ]} />
    <section aria-label="明確來源關聯" className="grid min-w-0 gap-3 text-sm leading-7"><h4 className="font-medium">明確來源關聯，不證明同一事件或文件真偽</h4>
      {record.matches.length === 0 ? <p>此版本未記錄來源關聯；不推定不存在來源。</p>
        : <ul className="grid gap-3">{record.matches.map(match => <li key={match.observation_id} className="break-all">觀測 {match.observation_id}；原始來源雜湊 {match.source_sha256}；調整基準聲明 {match.basis_assertion_id ?? "未記錄"}；關聯依據 {match.reason}。</li>)}</ul>}</section>
    <section aria-label="歷史來源比較" className="grid min-w-0 gap-3 text-sm leading-7"><h4 className="font-medium">歷史來源比較，不代表目前狀態</h4>
      <p>比較結果：{assessments[assessment.status]}；不是文件驗證、持倉資格或收入。</p>
      <p className="break-all">已列獨立來源分組：{assessment.independent_origins.join("、") || "未列明"}；不代表來源覆蓋或文件核實。</p>
      <p>衝突欄位：{names(assessment.conflicting_fields)}；未完整原因：{assessment.incomplete_reasons.join("、") || "未列明"}。</p>
      <p className="break-all">比較列入的觀測識別碼：{assessment.eligible_observation_ids.join("、") || "未列明"}；不是收入資格。</p>
      {assessment.checks.length > 0 && <ul className="grid gap-3">{assessment.checks.map(check => <li key={check.observation_id} className="break-all">觀測 {check.observation_id}；比較金額 {check.comparison_dps ?? "未知"}；口徑 {basisLabels[check.comparison_basis]}；支持欄位 {names(check.supporting_fields)}；衝突欄位 {names(check.conflicting_fields)}；未完整原因 {check.incomplete_reasons.join("、") || "未列明"}；基準聲明 {check.basis_assertion_id ?? "未記錄"}。</li>)}</ul>}
    </section>
    <Link to={`/settings/securities/${terms.security_id}`} className="inline-flex min-h-11 items-center text-sm text-primary underline">核對穩定證券身分</Link>
  </div>;
}

function CandidateHistory({ record }: { record: Candidate }) {
  const page = usePageState();
  const result = useQuery({ queryKey: ["dividends", "candidate-revisions", record.id, page.cursor], staleTime: 0,
    queryFn: async ({ signal }) => verifiedPage(checked(await api.GET("/api/v1/dividends/candidates/{id}/revisions", { params: { path: { id: record.id }, query: { cursor: page.cursor, limit: 25 } }, signal })), { id: record.id, security: record.terms.security_id }),
    select: data => ({ ...data, items: data.items.map(candidate => ({ id: candidate.revision_id, candidate })) }),
  });
  return <section aria-label="候選修訂歷史" className="grid min-w-0 gap-4"><h3 className="font-semibold">候選修訂歷史</h3>
    <p className="text-sm leading-7 text-secondary">清單與歷史分開讀取，可能包含較新版本；按版本倒序保留原始審閱。歷史比較，不代表目前狀態或事件關聯。</p>
    <RecordList title="候選修訂" page={page} result={result} searchable={false} emptyMessage="此頁沒有候選修訂；請重新載入核對。">{row => <article className="grid min-w-0 gap-4">
      <h4 className="font-semibold">歷史版本 {row.candidate.version}；{states[row.candidate.state]}</h4>
      <p className="break-all text-sm">除淨日 {row.candidate.terms.ex_date ?? "未知"}；每股 {amount(row.candidate.terms)}</p>
      <CandidateDetails record={row.candidate} />
    </article>}</RecordList></section>;
}

function CandidateCard({ record, disabled, onOpen }: { record: Candidate; disabled: boolean; onOpen: (editor: Editor) => void }) {
  const desktop = useDesktop();
  const [open, setOpen] = useState(false);
  const [history, setHistory] = useState(false);
  const eventReady = validCandidateForEvent(record);
  return <article aria-label={`股息候選 ${record.id}`} className="grid min-w-0 gap-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><h2 className="font-semibold"><SecurityName id={record.terms.security_id} /></h2>
      <p className="mt-2 break-all text-sm">除淨日 {record.terms.ex_date ?? "未知"}；每股 <span className="font-medium">{amount(record.terms)}</span></p></div>
      <span className="rounded-md border border-border px-2 py-1 text-sm">{states[record.state]}</span></div>
    {!desktop && <Button variant="outline" aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? "收合候選資料" : "查看候選資料"}</Button>}
    {(desktop || open) && <div className="grid min-w-0 gap-5"><CandidateDetails record={record} />
      <div className="flex flex-wrap gap-3"><Button variant="outline" disabled={disabled || record.version >= 2_147_483_647} onClick={() => onOpen({ mode: "revise", target: record })}>修訂候選資料</Button>
        {eventReady && <Button variant="outline" disabled={disabled} onClick={() => onOpen({ mode: "distinct", target: record })}>建立獨立股息事件</Button>}
        <Button variant="outline" aria-expanded={history} onClick={() => setHistory(value => !value)}>{history ? "收合候選修訂歷史" : "查看候選修訂歷史"}</Button></div>
      {!eventReady && <p className="text-sm leading-7 text-secondary">候選已排除、欠缺明確除淨日、每股金額或幣別，或來源比較無法核對；目前不能建立事件。</p>}
      {record.version >= 2_147_483_647 && <p className="text-sm text-danger">候選版本已達上限，不能建立新修訂。</p>}
      {history && <CandidateHistory record={record} />}</div>}
  </article>;
}

type Editor = { mode: "create" } | { mode: "revise" | "distinct"; target: Candidate };

function DistinctEventEditor({ target, onClose, onSaved }: { target: Candidate; onClose: () => void; onSaved: (event: components["schemas"]["DividendEventRecord"]) => void }) {
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [decisionBusy, setDecisionBusy] = useState(false);
  return <EditorSheet title="由候選建立獨立股息事件" onClose={onClose}>
    <div className="mb-6"><PasswordForm disabled={decisionBusy} onPendingChange={setPasswordBusy} /></div>
    <CandidateEventCreateForm target={target} onCancel={onClose} onSaved={onSaved} disabled={passwordBusy} onPendingChange={setDecisionBusy} />
  </EditorSheet>;
}
const initialFilters = { security_id: "", state: "" as "" | Candidate["state"] };
export function DividendCandidatesPage() {
  const cache = useQueryClient();
  const [editor, setEditor] = useState<Editor | null>(null);
  const [notice, setNotice] = useState<{ kind: "candidate" | "event"; id: string } | null>(null);
  const page = usePageState("dividends:candidates");
  const [filters, setFilters] = useViewState("filters:dividends:candidates", initialFilters);
  const result = useQuery({ queryKey: ["dividends", "candidates", filters, page.q, page.cursor], staleTime: 0,
    queryFn: async ({ signal }) => verifiedPage(checked(await api.GET("/api/v1/dividends/candidates", { params: { query: {
      security_id: filters.security_id || undefined, state: filters.state || undefined, q: page.q, cursor: page.cursor, limit: 25,
    } }, signal }))),
  });
  function filter(change: Partial<typeof initialFilters>) { setFilters(previous => ({ ...previous, ...change })); page.search(page.q); }
  const ready = !!result.data && !result.isFetching && !result.isError && !result.isPaused;
  function closeEditor() { setEditor(null); void cache.invalidateQueries({ queryKey: ["dividends"] }); }
  return <div className="grid min-w-0 gap-6"><header className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-3xl"><h1 className="text-2xl font-semibold">股息候選資料</h1>
    <p className="mt-2 text-sm leading-7 text-secondary">檢視已記錄的候選條款、來源關聯和歷史比較。這裡不驗證文件、不判斷持倉資格，也不產生應收或實收；未知不會補零。</p>
    <div className="flex flex-wrap gap-x-5"><Link to="/dividends" className="inline-flex min-h-11 items-center text-sm text-primary underline">返回股息事件</Link>
      <Link to="/data-center/dividends" className="inline-flex min-h-11 items-center text-sm text-primary underline">查看股息來源觀測</Link></div></div>
    <Button data-editor-create disabled={!ready || editor !== null} onClick={() => { setNotice(null); setEditor({ mode: "create" }); }}>新增股息候選</Button></header>
    {!ready && <p className="text-sm leading-7 text-secondary">候選清單尚未完成重新核對；目前不能開始新增、修訂或建立事件，請等候或重試。</p>}
    {notice && <p role="status" className="panel break-all text-sm leading-7">伺服器已回覆{notice.kind === "event" ? "獨立事件" : "候選決定"} {notice.id}；可能是原鍵歷史回覆，請重新載入核對目前版本與來源。這不是文件驗證、持倉資格或實收。</p>}
    {editor?.mode === "distinct" ? <DistinctEventEditor target={editor.target} onClose={closeEditor} onSaved={record => { setNotice({ kind: "event", id: record.id }); closeEditor(); }} />
      : editor && <EditorSheet title={editor.mode === "create" ? "新增股息候選" : "修訂股息候選"} onClose={closeEditor}>
        {editor.mode === "create" ? <CandidateCreateForm onCancel={closeEditor} onSaved={record => { setNotice({ kind: "candidate", id: record.id }); closeEditor(); }} />
          : <CandidateRevisionForm target={editor.target} onCancel={closeEditor} onSaved={record => { setNotice({ kind: "candidate", id: record.id }); closeEditor(); }} />}
      </EditorSheet>}
    <details className="rounded-lg border border-border bg-surface p-4"><summary className="flex min-h-11 cursor-pointer items-center font-medium">篩選股息候選</summary>
      <div className="mt-5 grid gap-5 sm:grid-cols-2 lg:grid-cols-3"><ReferenceSelect kind="securities" activeOnly={false} label="候選證券" value={filters.security_id} registration={{ onChange: event => filter({ security_id: event.target.value }) }} clearSelection={() => filter({ security_id: "" })} />
        <SelectField label="候選狀態" value={filters.state} onChange={event => filter({ state: event.target.value as typeof filters.state })}><option value="">全部候選狀態</option>{(Object.keys(states) as Candidate["state"][]).map(state => <option key={state} value={state}>{states[state]}</option>)}</SelectField>
        <div><Button variant="outline" onClick={() => { setFilters(initialFilters); page.reset(); }}>清除候選篩選</Button></div></div></details>
    <FilterChips values={filters} labels={{ security_id: "候選證券", state: "候選狀態" }} onRemove={key => filter({ [key]: "" })} />
    <RecordList title="股息候選" page={page} result={result} filtered={Object.values(filters).some(Boolean)} emptyMessage="尚無可顯示的候選資料；這不代表沒有股息。請核對來源觀測及事件決定。" order={{ description: "本版記錄時間／實體修訂 ID 倒序", direction: "descending" }} columns={[
      { id: "created", header: "本版記錄時間", cell: ({ row }) => row.original.created_at },
      { id: "security", header: "證券", cell: ({ row }) => <SecurityName id={row.original.terms.security_id} /> },
      { id: "ex", header: "除淨日", cell: ({ row }) => row.original.terms.ex_date ?? "未知" },
      { id: "amount", header: "每股候選金額，非收入", cell: ({ row }) => amount(row.original.terms) },
      { id: "state", header: "候選狀態", cell: ({ row }) => states[row.original.state] },
      { id: "assessment", header: "歷史比較", cell: ({ row }) => assessments[row.original.assessment.status] },
    ]}>{record => <CandidateCard record={record} disabled={!ready || editor !== null} onOpen={value => { setNotice(null); setEditor(value); }} />}</RecordList>
  </div>;
}
