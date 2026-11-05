import { useState } from "react";
import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { api } from "@spm/contracts";
import { Field } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, failureMessage, mutationHeaders } from "../../lib/api";
import type { Account } from "../settings/data";
import { CashNav } from "./CashNav";
import { CompletenessForm, OpeningForm } from "./OpeningForm";
import { ReasonedAction } from "./ReasonedAction";
import { refreshLedger, useAccount, type Opening } from "./data";

const balanceLabels = { COMPLETE: "已明確核對完整現金帳", UNKNOWN: "完整性未知", PARTIAL: "只有部分現金紀錄", BEFORE_OPENING: "早於目前期初日期", NEEDS_REVIEW: "期初來源或餘額需要覆核" };
const openingLabels = { UNSET: "未設定，不代表零", OK: "斷言來源完整", MISSING_FX: "原幣已記錄，歷史 FX 未知", NEEDS_REVIEW: "目前斷言來源需要覆核" };
type Editor = { kind: "opening" | "clear"; record: Opening } | { kind: "completeness"; record: Account };

function ReadStatus({ label, result }: { label: string; result: UseQueryResult<unknown> }) {
  if (result.isPaused) return <p role="status" className="text-sm leading-7 text-secondary">{label}連線已暫停；只保留記憶體資料，不會離線排程寫入。</p>;
  if (result.isPending) return <p role="status" className="text-sm text-secondary">正在讀取{label}…</p>;
  if (result.isError) return <div role="alert" className="grid gap-3 text-sm leading-7 text-danger"><p>{failureMessage(result.error)}{result.data !== undefined ? " 以下為上次讀取資料，尚未確認最新版本。" : ""}</p><Button variant="outline" onClick={() => void result.refetch()}>重試讀取{label}</Button></div>;
  return null;
}

export function AccountCashPage() {
  const { id = "" } = useParams();
  const cache = useQueryClient();
  const account = useAccount(id);
  const [asOf, setAsOf] = useState("");
  const [editor, setEditor] = useState<Editor | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const opening = useQuery({ queryKey: ["ledger", "opening", id], enabled: !!id, queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/accounts/{id}/opening-cash", { params: { path: { id } }, signal })) });
  const balances = useQuery({ queryKey: ["ledger", "balances", id, asOf], enabled: !!id, queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/accounts/{id}/cash-balances", { params: { path: { id }, query: { as_of: asOf || undefined } }, signal })) });
  function saved(message: string) { setEditor(null); setNotice(message); void refreshLedger(cache); }
  const busy = editor !== null;
  const archived = account.data?.status === "ARCHIVED";
  return <div className="grid gap-6">
    <CashNav />
    <div><h1 className="text-2xl font-semibold">帳戶現金與期初</h1><p className="mt-2 break-words text-sm leading-7 text-secondary">{account.data?.name ?? id}。原幣錢包分開呈現，不加總不同幣別，也不以歷史入帳淨額冒充現價估值。</p></div>
    <ReadStatus label="帳戶" result={account} />
    <div className="flex flex-wrap gap-4 text-sm"><Link to={`/cash?account_id=${id}`} className="inline-flex min-h-11 items-center text-primary underline">此帳戶現金日誌</Link><Link to={`/cash/expenses?account_id=${id}`} className="inline-flex min-h-11 items-center text-primary underline">此帳戶費用與退款</Link></div>
    {notice && <p role="status" className="text-sm leading-7 text-success">{notice}</p>}
    {archived && <p role="status" className="text-sm leading-7 text-secondary">帳戶已封存。歷史仍可讀取及作廢一般錯誤入帳；新入帳或期初更正前請先在設定重新啟用。</p>}
    {editor?.kind === "opening" && <OpeningForm key={`opening-${editor.record.version}`} record={editor.record} onCancel={() => { setEditor(null); void opening.refetch(); }} onSaved={record => {
      cache.setQueryData(["ledger", "opening", id], record); saved("已儲存期初斷言；原始值及精確沖銷會保留，完整性不會被自動提升。");
    }} />}
    {editor?.kind === "completeness" && <CompletenessForm key={`completeness-${editor.record.version}`} record={editor.record} onCancel={() => { setEditor(null); void account.refetch(); }} onSaved={record => {
      cache.setQueryData(["accounts", "record", id], record); saved("已儲存現金完整性斷言；是否可確認餘額仍以期初來源、期間及伺服器結果為準。");
    }} />}
    {editor?.kind === "clear" && <ReasonedAction key={`clear-${editor.record.version}`} title="撤回期初斷言" label="確認撤回期初斷言" reasonLabel="撤回期初依據" description="會精確沖銷目前期初及其有效調整、保留普通現金紀錄，並把現金完整性重設為未知。這不是把實際餘額設為零。" onCancel={() => setEditor(null)} onConfirm={async (reason, key) => {
      const result = checked(await api.PATCH("/api/v1/accounts/{id}/opening-cash", { params: { path: { id }, header: { ...await mutationHeaders(key), "If-Match": `"${editor.record.version}"` } }, body: { opening_cash: null, opening_cash_date: null, opening_fx_rate_hkd: null, reason, note: "" } }));
      cache.setQueryData(["ledger", "opening", id], result); saved("已撤回期初斷言並重設完整性為未知；普通現金及原始歷史仍保留，未宣稱實際餘額為零。");
    }} />}
    <section aria-label="原幣錢包" className="panel grid gap-5">
      <h2 className="text-lg font-semibold">原幣錢包</h2>
      <Field label="餘額生效日（留空使用伺服器今日）" type="date" value={asOf} onChange={event => setAsOf(event.target.value)} help="目前已更正的生效日視圖，不是當時已知觀測或歷史稽核時間快照。" />
      <ReadStatus label="原幣餘額" result={balances} />
      {balances.data && <><p className="text-sm text-secondary">伺服器生效日：{balances.data.as_of}</p><ul className="grid gap-5">
        {balances.data.balances.map(wallet => <li key={wallet.currency} aria-label={`${wallet.currency} 錢包`} className="rounded-md border border-border p-4">
          <h3 className="font-semibold">{wallet.currency} 原幣錢包</h3>
          <dl className="mt-4 grid gap-4 text-sm sm:grid-cols-2">
            <div><dt className="text-secondary">已記錄原幣淨額（含沖銷）</dt><dd className="mt-1 break-all">{wallet.currency} {wallet.recorded_balance}</dd></div>
            <div><dt className="text-secondary">可確認的實際餘額</dt><dd className="mt-1 break-all">{wallet.balance === null ? "未知，不是零" : `${wallet.currency} ${wallet.balance}`}</dd></div>
            <div><dt className="text-secondary">餘額品質</dt><dd className="mt-1">{balanceLabels[wallet.balance_quality]}</dd></div>
            <div><dt className="text-secondary">歷史 HKD 入帳淨額，非現價估值／FX 損益</dt><dd className="mt-1 break-all">{wallet.historical_booked_hkd_net === null ? "未知（仍有未認列歷史 FX）" : `HKD ${wallet.historical_booked_hkd_net}`}</dd></div>
            <div><dt className="text-secondary">未認列歷史 HKD 的有效項目</dt><dd className="mt-1">{wallet.unvalued_entries} 筆</dd></div>
          </dl>
        </li>)}
      </ul></>}
      <p className="text-sm leading-7 text-secondary">已記錄金額不一定等於實際餘額。只有明確完整對帳、適用的期初日期及完整來源才能確認；缺少 FX 不會補零或採用預設匯率。</p>
      <Button variant="outline" onClick={() => void balances.refetch()} disabled={balances.isFetching}>重新讀取原幣餘額</Button>
    </section>
    <section aria-label="目前期初斷言" className="panel grid gap-5">
      <h2 className="text-lg font-semibold">目前期初斷言</h2>
      <ReadStatus label="期初斷言" result={opening} />
      {opening.data && <>
        <dl className="grid gap-4 text-sm sm:grid-cols-2">
          {[["斷言金額", opening.data.opening_cash === null ? "未設定，不代表零" : `${opening.data.currency} ${opening.data.opening_cash}`],
            ["期初日期", opening.data.opening_cash_date ?? "未設定"], ["歷史 FX", opening.data.fx_rate_hkd ?? "未知／未設定"],
            ["歷史 HKD 斷言金額", opening.data.booked_hkd === null ? "未知／未設定" : `HKD ${opening.data.booked_hkd}`],
            ["來源品質", openingLabels[opening.data.quality]], ["現金完整性", balanceLabels[opening.data.cash_completeness]],
            ["目前現金事實", opening.data.entry_id ?? "沒有期初事實"], ["前一期初事實", opening.data.predecessor_id ?? "沒有前一更正"],
            ["帳戶控制版本", String(opening.data.version)],
          ].map(([label, value]) => <div key={label}><dt className="text-secondary">{label}</dt><dd className="mt-1 break-all">{value}</dd></div>)}
        </dl>
        <div className="flex flex-wrap gap-3"><Button variant="outline" disabled={busy || archived || opening.isFetching || opening.isError || !account.data} onClick={() => { setNotice(null); if (opening.data) setEditor({ kind: "opening", record: opening.data }); }}>設定／更正期初</Button>
          {opening.data.opening_cash !== null && <Button variant="outline" disabled={busy || archived || opening.isFetching || opening.isError} onClick={() => { setNotice(null); if (opening.data) setEditor({ kind: "clear", record: opening.data }); }}>撤回期初斷言</Button>}</div>
      </>}
      <p className="text-sm leading-7 text-secondary">斷言是其原幣在當日開始時的餘額，不是存款，也不會替另一原幣錢包補錢。撤回與重新設定已知零是不同操作；撤回後必須重新核對，才能明確提升完整性。</p>
    </section>
    {account.data && <section className="panel grid gap-4"><h2 className="text-lg font-semibold">現金對帳完整性</h2><p className="text-sm leading-7 text-secondary">目前：{balanceLabels[account.data.cash_completeness]}。不會因記錄了一筆現金或補填 FX 而自動變為完整。</p>
      <Button variant="outline" disabled={busy || account.isFetching || account.isError} onClick={() => { setNotice(null); if (account.data) setEditor({ kind: "completeness", record: account.data }); }}>設定現金完整性</Button>
    </section>}
  </div>;
}
