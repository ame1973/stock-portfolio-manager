import { useCallback, useState } from "react";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { usePageState } from "../settings/data";
import { DividendAccountPicker } from "./DividendEstimatePanel";
import { DividendProjectionPreparation } from "./DividendProjectionPreparation";
import type { ProjectionTarget } from "./projection-input-validation";
import { validEstimateEvent } from "./estimate-validation";
import { checkedProjectionPage } from "./projection-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type Projection = Schemas["ProjectionRecord"];
const states: Record<Projection["state"], string> = {
  EXPECTED: "已記錄估算，不代表已確認資格或實收。",
  NO_POSITION: "此快照沒有已記錄持倉；不是已確認零持倉。",
  NEEDS_REVIEW: "需要核對；沒有可計算金額，不是零。",
  CANCELLED: "事件在此快照中已撤回；不是實收沖銷。",
};
const qualities: Record<NonNullable<Projection["estimated_amounts"]>["quality"], string> = {
  OK: "計算可用；不代表稅費或匯率已確認。",
  MISSING_QUOTE: "缺少報價；不是零。",
  MISSING_FX: "缺少港幣匯率，港幣估值未知。",
  NOT_COMPUTABLE: "無法計算，請核對來源。",
  NEEDS_REVIEW: "計算需要核對。",
};

function Provenance({ value, kind }: { value: Projection["tax_provenance"]; kind: "稅率" | "費用" }) {
  if (value === null) return <p>舊投影沒有保留{kind}估算來源；不能推定為零。</p>;
  return <p className="break-all">{kind}估算：{value.status === "MISSING" ? "未設定，不是零" : value.status === "UNKNOWN" ? "未知，不是零" : "擁有人估算，並非實際扣款"}；
    生效日 {value.effective_on}；觀察到設定頂端 {value.observed_head_no}；頂端識別碼 {value.observed_head_id ?? "沒有設定"}；
    選擇的設定 {value.selected_setting_id ?? "沒有選擇"}。</p>;
}

function Record({ value, event }: { value: Projection; event: Event }) {
  const sameRevision = value.revision_no === event.version && value.revision_id.toLowerCase() === event.current_revision_id.toLowerCase();
  const amounts = value.estimated_amounts;
  return <li className="grid min-w-0 gap-2 rounded-md border border-border p-4 text-sm leading-7">
    <h5 className="font-semibold">投影版本 {value.projection_no}；{states[value.state]}</h5>
    <p>{value.timing_scope === "BEFORE_EX_DATE_RECORDED_HOLDINGS_ONLY" ? "僅為除淨日前的已記錄持倉，不是已認列收入。" : "此日的已記錄歷史，不代表已確認資格或實收。"}</p>
    <p>{sameRevision ? "與目前載入的事件版本一致；仍須重新核對。" : "與目前載入的事件版本不同；請按歷史版本核對，不可相加。"}</p>
    <p className="break-all">事件修訂 {value.revision_no}／{value.revision_id}；投影識別碼 {value.id}；前一版本 {value.previous_id ?? "沒有"}。</p>
    <p>查閱日 {value.as_of}；歷史除淨日 {value.ex_date}；規則 {value.eligibility_rule === "ORDINARY_PRE_EX_DATE" ? "一般除淨日前" : "需另行核對"}。</p>
    <p className="break-all text-secondary">原組合 {value.portfolio_id}；原券商 {value.broker_id}；帳戶版本 {value.account_version}；券商版本 {value.broker_version}；組合資料版本 {value.portfolio_data_revision}。</p>
    {amounts ? <><p>已記錄股數 {value.recorded_shares}；估算毛額 {amounts.currency} {amounts.gross_local}；估算稅額 {amounts.tax_local}；估算費用 {amounts.fee_local}。</p>
      <p>估算淨額 {amounts.currency} {amounts.net_local}；分項尾差 {amounts.rounding_delta_local}；港幣估值 {amounts.estimated_hkd === null ? "未知" : amounts.estimated_hkd}。</p>
      <p>{qualities[amounts.quality]}</p><p>稅率估算 {value.tax_rate}；原始費用 {value.fee_currency} {value.fee_original}；費用換算率 {value.fee_rate_event ?? "未提供"}；港幣匯率 {value.fx_rate_hkd ?? "未提供"}。</p></>
      : <p>股數及金額未提供；不以零代替。</p>}
    <Provenance value={value.tax_provenance} kind="稅率" /><Provenance value={value.fee_provenance} kind="費用" />
    <p>記錄依據：{value.reason}</p><p>記錄時間（UTC）：{value.created_at}；歷史完整性未驗證，使用前必須重新核對。</p>
  </li>;
}

function History({ event, account }: { event: Event; account: Account }) {
  const page = usePageState();
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => checkedProjectionPage(checked(await api.GET(
    "/api/v1/dividends/events/{event_id}/accounts/{account_id}/projections", {
      params: { path: { event_id: event.id, account_id: account.id }, query: { cursor, limit: 25 } }, signal,
    }
  )), event, account), [event, account]);
  const result = usePrivatePage(`projection-history:${event.id}:${event.current_revision_id}:${event.version}:${account.id}`, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError;
  return <section aria-label="帳戶投影歷史" className="grid min-w-0 gap-3 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-semibold">帳戶投影歷史</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => result.refetch()}>重新載入投影歷史</Button></div>
    <p className="text-sm leading-7 text-secondary">歷史投影不是已確認資格或實收；不同版本不能相加，舊紀錄不保證仍然有效。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；顯示的是上次讀取，並非目前狀態。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入投影歷史…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對投影歷史。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下是上次讀取，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => result.refetch()}>重試載入投影歷史</Button></div>}
    {result.data && <>{result.data.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有已記錄的投影；不代表持倉或股息為零。</p>
      : <ul className="grid gap-3">{result.data.items.map(value => <Record key={value.id} value={value} event={event} />)}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={!ready || page.number === 1} onClick={page.previous}>上一頁投影歷史</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆，非全部歷史。</p>
        <Button type="button" variant="outline" disabled={!ready || !result.data.next_cursor} onClick={() => { if (result.data?.next_cursor) page.next(result.data.next_cursor); }}>下一頁投影歷史</Button></div></>}
  </section>;
}

export function DividendProjectionPanel({ event: observed, onWrite, writeDisabled = false, refresh = 0 }: {
  event: Event; onWrite?: (target: ProjectionTarget) => void; writeDisabled?: boolean; refresh?: number;
}) {
  const [event] = useState(() => structuredClone(observed));
  const [account, setAccount] = useState<Account | null>(null); const [preparing, setPreparing] = useState(false);
  if (!validEstimateEvent(event)) return <p role="alert" className="text-sm text-danger">事件版本無法安全核對；請重新載入後再查閱投影。</p>;
  return <section aria-label="已記錄股息投影" className="grid min-w-0 gap-4 rounded-md border border-border p-4">
    <h3 className="font-semibold">已記錄股息投影</h3>
    <p className="break-all text-sm leading-7 text-secondary">固定事件 {event.id}；版本 {event.version}；實體修訂 {event.current_revision_id}。查閱不會建立持倉、應收或實收。</p>
    <DividendAccountPicker purpose="投影" selected={account} onSelect={setAccount} />
    {account && <><p className="break-all text-sm">所選帳戶：{account.name}；{account.id}。</p>
      <History key={`${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${refresh}`} event={event} account={account} />
      {onWrite && <div><Button type="button" variant="outline" aria-expanded={preparing} onClick={() => setPreparing(value => !value)}>{preparing ? "收合投影寫入準備" : "準備記錄投影"}</Button></div>}
      {preparing && onWrite && <DividendProjectionPreparation key={`${event.id}:${event.current_revision_id}:${event.version}:${account.id}:${refresh}`} event={event} account={account} onWrite={onWrite} disabled={writeDisabled} />}</>}
  </section>;
}
