import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "@spm/contracts";
import { Field } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { ConfirmAction } from "../../components/ui/confirm-action";
import { EditorSheet } from "../../components/ui/editor-sheet";
import { checked, mutationHeaders } from "../../lib/api";
import { FilterChips } from "../../components/forms/filter-chips";
import { useViewState } from "../../lib/view-state";
import { RecordList } from "../settings/RecordList";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { PasswordForm } from "../settings/SecuritySettingsPage";
import { usePageState } from "../settings/data";
import { reason } from "./inputs";
import { TradeForm, type Trade } from "./TradeForm";
import { TradeOriginPanel, TradeOriginRecoveryForm, type TradeOrigin } from "./TradeOrigin";
import { TradeHistoryReviewForm, TradeHistoryReviewPanel, type ReviewIntent } from "./TradeHistoryReview";
import { useAccount, useSecurity } from "./data";
import { AccountName, SecurityName } from "./ReferenceName";

const statuses: Record<Trade["status"], string> = {
  ACTIVE: "已入帳", DRAFT_SCHEDULED: "未來草稿，尚未入帳", PENDING_FX: "待補歷史 FX", SUPERSEDED: "已有新修訂", VOID: "已作廢，歷史保留",
};
const editable = (record: Trade) => record.status !== "SUPERSEDED" && record.status !== "VOID";

function TradeDetails({ record, children }: { record: Trade; children: ReactNode }) {
  const security = useSecurity(record.security_id);
  const account = useAccount(record.account_id);
  return <article aria-label={`${record.trade_date} ${record.side === "BUY" ? "買入" : "賣出"} ${record.quantity}`} className="grid gap-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 break-words"><h2 className="font-semibold">{record.trade_date} · {record.side === "BUY" ? "買入" : "賣出"} {record.quantity} 股</h2>
        <p className="mt-2 text-sm text-secondary">目前代碼：{security.data ? `${security.data.display_ticker} / ${security.data.exchange_code}` : record.security_id}</p>
        <p className="mt-1 text-sm text-secondary">帳戶：{account.data?.name ?? record.account_id}</p>
        {(security.isError || account.isError) && <p role="status" className="mt-2 text-sm text-danger">主檔名稱未能更新；財務原值仍保留，請重新載入或核對下方穩定識別碼。</p>}
      </div><span className="rounded-md border border-border px-2 py-1 text-sm">{statuses[record.status]}</span>
    </div>
    <dl className="grid gap-x-6 gap-y-4 text-sm sm:grid-cols-3">
      <div><dt className="text-secondary">每股成交價</dt><dd className="mt-1 break-all">{record.currency} {record.price}</dd></div>
      <div><dt className="text-secondary">{record.status === "DRAFT_SCHEDULED" ? "草稿交收額，未入帳" : record.side === "BUY" ? "交收支出" : "交收淨收入"}</dt><dd className="mt-1 break-all font-medium">{record.currency} {record.settlement}</dd></div>
      <div><dt className="text-secondary">歷史 HKD 入帳額，非現價估值</dt><dd className="mt-1 break-all">{record.booked_hkd === null ? "未知，未確認 HKD 認列" : `HKD ${record.booked_hkd}`}</dd></div>
    </dl>
    {record.status === "DRAFT_SCHEDULED" && <p className="text-sm leading-7 text-secondary">未影響目前現金或持倉。日期到達不會自動入帳；須明確更正、補歷史 FX 並重新檢查持倉。</p>}
    <details className="text-sm"><summary className="min-h-11 cursor-pointer py-3 font-medium">交易原值與修訂依據</summary>
      <dl className="grid gap-4 break-words rounded-md bg-muted p-4 sm:grid-cols-2">
        {[
          ["交易費用", `${record.currency} ${record.fee}`], ["交易稅費", `${record.currency} ${record.tax}`],
          ["歷史 FX（1 原幣 = HKD）", record.fx_rate_hkd ?? "未知，不是零"], ["FX 來源", record.fx_source ?? "未知"],
          ["成交時間", record.executed_at_utc ?? "未知，未補造時間"], ["原幣總額", `${record.currency} ${record.gross}`],
          ["交收捨入差額", `${record.currency} ${record.rounding_delta}`], ["實際金額覆寫", `${record.settlement_override ? "原幣實際值" : "計算交收"}；${record.hkd_override ? "HKD 實際值" : "按歷史 FX"}`],
          ["覆寫依據", record.override_reason || "未覆寫"], ["備註", record.note || "未填寫"],
          ["交易識別碼", record.id], ["穩定事件識別碼", record.root_id], ["前一修訂", record.supersedes_id ?? "首個版本"],
          ["修訂／控制版本", `${record.revision} / ${record.version}`], ["穩定排序序號", Number.isSafeInteger(record.created_sequence) ? String(record.created_sequence) : "超過瀏覽器精確整數範圍；請以原始伺服器 JSON 為準"],
          ["證券識別碼", record.security_id], ["帳戶識別碼", record.account_id], ["建立時間", record.created_at],
        ].map(([label, value]) => <div key={label}><dt className="text-secondary">{label}</dt><dd className="mt-1 break-all">{value}</dd></div>)}
      </dl>
    </details>
    {children}
  </article>;
}

function VoidTrade({ record, onDone, onCancel }: { record: Trade; onDone: () => void; onCancel: () => void }) {
  const [explanation, setExplanation] = useState("");
  const valid = reason.safeParse(explanation);
  return <section aria-label="作廢交易" className="panel grid gap-5">
    <h3 className="font-semibold">作廢 {record.trade_date} 的交易</h3>
    <p className="text-sm leading-7 text-secondary">原始紀錄不會刪除。已入帳的原幣、歷史 FX 與 HKD 金額會精確沖銷；若影響後續賣出或轉倉，整項操作會拒絕。</p>
    <Field label="作廢交易依據" value={explanation} maxLength={1000} onChange={event => setExplanation(event.target.value)} help="請填寫至少 10 個字元的具體原因，再確認作廢。" autoFocus />
    <div className="flex flex-wrap gap-3">
      <ConfirmAction label="作廢此交易" title="確認作廢這筆交易？" disabled={!valid.success}
        description={`${record.trade_date}，${record.side === "BUY" ? "買入" : "賣出"} ${record.quantity} 股，${record.currency} ${record.settlement}。歷史與審計會保留；不會向券商下單。`}
        onConfirm={async key => {
          const header = { ...await mutationHeaders(key), "If-Match": `"${record.version}"` };
          checked(await api.DELETE("/api/v1/trades/{id}", { params: { path: { id: record.id }, header }, body: { reason: reason.parse(explanation) } }));
          onDone();
        }} />
      <Button variant="outline" onClick={onCancel}>取消作廢</Button>
    </div>
  </section>;
}

function RecoveryEditor({ origin, onCancel, onSaved }: {
  origin: TradeOrigin; onCancel: () => void; onSaved: () => void;
}) {
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  return <EditorSheet title="恢復交易稽核來源" onClose={onCancel}>
    <div className="mb-6"><PasswordForm disabled={recoveryBusy} onPendingChange={setPasswordBusy} /></div>
    <TradeOriginRecoveryForm origin={origin} disabled={passwordBusy} onPendingChange={setRecoveryBusy} onCancel={onCancel} onSaved={onSaved} />
  </EditorSheet>;
}

function ReviewEditor({ intent, onCancel, onSaved }: {
  intent: ReviewIntent; onCancel: () => void; onSaved: (withdrawn: boolean) => void;
}) {
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [reviewBusy, setReviewBusy] = useState(false);
  const title = intent.decision === "WITHDRAW" ? "撤回未驗證審核" : "記錄未驗證審核";
  return <EditorSheet title={title} onClose={onCancel}>
    <div className="mb-6"><PasswordForm disabled={reviewBusy} onPendingChange={setPasswordBusy} /></div>
    <TradeHistoryReviewForm intent={intent} disabled={passwordBusy} onPendingChange={setReviewBusy} onCancel={onCancel}
      onSaved={record => onSaved(record.decision === "WITHDRAW")} />
  </EditorSheet>;
}

const initialFilters = { portfolio_id: "", account_id: "", security_id: "", from_date: "", to_date: "", root_id: "", include_history: false };

export function TradesPage() {
  const cache = useQueryClient();
  const page = usePageState("trades");
  const [filters, setFilters] = useViewState("filters:trades", initialFilters);
  const [editor, setEditor] = useState<Trade | "new" | null>(null);
  const [voiding, setVoiding] = useState<Trade | null>(null);
  const [recovery, setRecovery] = useState<TradeOrigin | null>(null);
  const [review, setReview] = useState<ReviewIntent | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const editing = editor !== null || voiding !== null || recovery !== null || review !== null;
  const result = useQuery({ queryKey: ["ledger", "trades", filters, page.q, page.cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/trades", { params: { query: {
      q: page.q, cursor: page.cursor, limit: 25, include_history: filters.include_history,
      portfolio_id: filters.portfolio_id || undefined, account_id: filters.account_id || undefined, security_id: filters.security_id || undefined,
      from_date: filters.from_date || undefined, to_date: filters.to_date || undefined, root_id: filters.root_id || undefined,
    } }, signal })) });
  function filter(change: Partial<typeof initialFilters>) { setFilters(previous => ({ ...previous, ...change })); page.search(page.q); }
  function refresh() {
    void cache.invalidateQueries({ queryKey: ["ledger"] });
    void cache.invalidateQueries({ queryKey: ["portfolios"] });
  }
  function closeRecovery() {
    if (recovery) void cache.invalidateQueries({ queryKey: ["ledger", "trade-audit-origin", recovery.trade_id] });
    setRecovery(null);
  }
  function closeReview() {
    if (review) void cache.invalidateQueries({ queryKey: ["ledger", "trade-history-review", review.trade_id] });
    setReview(null);
  }
  return <div className="grid gap-6">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div className="max-w-2xl"><h1 className="text-2xl font-semibold">交易紀錄</h1><p className="mt-2 text-sm leading-7 text-secondary">逐帳戶記錄買賣與歷史交收。更正建立新版本，不覆寫原值；缺少 FX 不會被當作零，也不會以現價補填。</p></div>
      <Button data-editor-create disabled={editing} onClick={() => { setNotice(null); setEditor("new"); }}>新增交易</Button>
    </div>
    <p className="text-sm leading-7 text-secondary">請先在<Link to="/settings/accounts" className="inline-flex min-h-11 items-center text-primary underline">管理設定</Link>建立組合、帳戶與證券。此頁不是現金估值或目前持倉總額。</p>
    {notice && <p role="status" className="text-sm leading-7 text-success">{notice}</p>}
    {editor !== null && <EditorSheet title={editor === "new" ? "新增交易" : "更正交易"} onClose={() => { setEditor(null); void result.refetch(); }}>
      <TradeForm key={editor === "new" ? "new" : editor.id} record={editor === "new" ? undefined : editor} onCancel={() => { setEditor(null); void result.refetch(); }}
        onSaved={record => { setEditor(null); setNotice(record.status === "DRAFT_SCHEDULED" ? "已儲存未來交易草稿；沒有現金或目前持倉影響。" : "已儲存交易；原始版本與歷史匯率會保留。請核對下方伺服器入帳結果。"); refresh(); }} />
    </EditorSheet>}
    {voiding && <VoidTrade key={voiding.id} record={voiding} onCancel={() => setVoiding(null)} onDone={() => { setVoiding(null); setNotice("交易已作廢；原值、精確沖銷及審計仍保留。"); refresh(); }} />}
    {recovery && <RecoveryEditor origin={recovery} onCancel={closeRecovery}
      onSaved={() => { setRecovery(null); setNotice("已留存與資料庫稽核相符的交易來源；這不是完整持倉或股息實收證明。"); refresh(); }} />}
    {review && <ReviewEditor intent={review} onCancel={closeReview} onSaved={withdrawn => {
      setReview(null); setNotice(withdrawn ? "已撤回未驗證審核；先前決定與審計仍保留，交易和股息沒有改變。" : "已記錄未驗證審核；這不是持倉或股息證明，也不會解除交易更正或作廢限制。"); refresh();
    }} />}
    <details className="rounded-lg border border-border bg-surface p-4"><summary className="cursor-pointer font-medium">篩選交易</summary>
      <div className="mt-5 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
        <ReferenceSelect kind="portfolios" label="篩選組合" value={filters.portfolio_id} registration={{ onChange: event => filter({ portfolio_id: event.target.value, account_id: "" }) }} clearSelection={() => filter({ portfolio_id: "", account_id: "" })} />
        <ReferenceSelect key={filters.portfolio_id} kind="accounts" label="篩選帳戶" activeOnly={false} portfolioId={filters.portfolio_id || undefined} value={filters.account_id} registration={{ onChange: event => filter({ account_id: event.target.value }) }} clearSelection={() => filter({ account_id: "" })} />
        <ReferenceSelect kind="securities" label="篩選證券" value={filters.security_id} registration={{ onChange: event => filter({ security_id: event.target.value }) }} clearSelection={() => filter({ security_id: "" })} />
        <Field label="交易起日" type="date" value={filters.from_date} onChange={event => filter({ from_date: event.target.value })} />
        <Field label="交易迄日" type="date" value={filters.to_date} onChange={event => filter({ to_date: event.target.value })} />
        <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" checked={filters.include_history} onChange={event => filter({ include_history: event.target.checked })} />包括已被取代的修訂</label>
        <Button variant="outline" onClick={() => { setFilters(initialFilters); page.reset(); }}>清除交易篩選</Button>
      </div>
    </details>
    <FilterChips values={filters} labels={{ portfolio_id: "組合", account_id: "帳戶", security_id: "證券", from_date: "交易起日", to_date: "交易迄日", root_id: "事件修訂鏈", include_history: "歷史修訂" }} onRemove={key => filter({ [key]: key === "include_history" ? false : "", ...(key === "portfolio_id" ? { account_id: "" } : {}) })} />
    {filters.root_id && <div className="flex flex-wrap items-center gap-3 text-sm"><p>只顯示所選事件的修訂鏈。</p><Button variant="outline" onClick={() => filter({ root_id: "" })}>顯示全部交易事件</Button></div>}
    <RecordList title="交易" result={result} page={page} filtered={Object.values(filters).some(value => value !== false && value !== "")} columns={[
      { accessorKey: "trade_date", header: "交易日期" },
      { id: "references", header: "證券／帳戶", cell: ({ row }) => <div className="grid gap-1"><SecurityName id={row.original.security_id} /><AccountName id={row.original.account_id} /></div> },
      { id: "quantity", header: "方向／股數", cell: ({ row }) => <>{row.original.side === "BUY" ? "買入" : "賣出"}<br />{row.original.quantity} 股</> },
      { id: "settlement", header: "原幣交收額", cell: ({ row }) => <>{row.original.currency} {row.original.settlement}{row.original.status === "DRAFT_SCHEDULED" && <span className="block text-secondary">草稿，未入帳</span>}</> },
      { id: "booked", header: "歷史 HKD，非估值", cell: ({ row }) => row.original.booked_hkd === null ? "未知，不是零" : `HKD ${row.original.booked_hkd}` },
      { id: "status", header: "紀錄狀態", cell: ({ row }) => statuses[row.original.status] },
    ]}>{record => <TradeDetails record={record}>
      <TradeOriginPanel tradeId={record.id} disabled={editing} onRecover={origin => { setNotice(null); setRecovery(origin); }} />
      <TradeHistoryReviewPanel tradeId={record.id} disabled={editing} onReview={intent => { setNotice(null); setReview(intent); }} />
      <div className="flex flex-wrap gap-3">
        {editable(record) && <><Button variant="outline" disabled={editing} onClick={() => { setNotice(null); setEditor(record); }}>更正交易</Button>
          <Button variant="outline" disabled={editing} onClick={() => { setNotice(null); setVoiding(record); }}>作廢交易</Button></>}
        <Button variant="outline" onClick={() => { filter({ root_id: record.root_id, include_history: true }); page.search(""); }}>查看此事件修訂鏈</Button>
      </div>
    </TradeDetails>}</RecordList>
  </div>;
}
