import type { ReactNode } from "react";
import { Link } from "react-router";
import type { components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { failureMessage } from "../../lib/api";
import { SecurityName } from "../ledger/ReferenceName";
import { fetchLabels, incompleteLabels, isDividend, observationTerms, operationLabels, providerNames, type Observation } from "./data";

type ReadState = { isPending: boolean; isPaused: boolean; isError: boolean; error: unknown; data?: unknown; refetch: () => Promise<unknown> };
export function SourceReadState({ result, label }: { result: ReadState; label: string }) {
  return <>
    {result.isPaused ? <p role="status" className="text-sm leading-7 text-secondary">連線已暫停；只有先前載入的記憶體資料，不能視為目前即時狀態。</p>
      : result.isPending ? <p role="status" className="panel text-secondary">正在載入{label}…</p> : null}
    {result.isError && <div role="alert" className="panel grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>{result.data !== undefined && <p>以下保留上次載入內容，尚未確認最新狀態。</p>}<Button type="button" variant="outline" onClick={() => void result.refetch()}>重試載入{label}</Button></div>}
  </>;
}
export function SourceValues({ values }: { values: [string, ReactNode][] }) {
  return <dl className="grid gap-4 break-words text-sm leading-7 sm:grid-cols-2">{values.map(([label, value]) => <div key={label}><dt className="text-secondary">{label}</dt><dd className="mt-1 break-all">{value ?? "未知"}</dd></div>)}</dl>;
}
export function SourceLink({ url, label }: { url: string | null; label: string }) {
  let parsed: URL | null = null;
  try { if (url && url.length <= 2048) parsed = new URL(url); } catch { parsed = null; }
  const href = parsed && (parsed.protocol === "https:" || parsed.protocol === "http:") && !parsed.username && !parsed.password ? parsed.href : null;
  return href ? <a href={href} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-11 max-w-full items-center break-all text-primary underline">{label}（另開視窗，應用程式未抓取）</a> : <span>{url ? "來源連結未通過安全檢查，不會開啟或抓取" : "未提供，不會猜測或抓取"}</span>;
}
export function RecordedPage({ record }: { record: components["schemas"]["RecordedSourcePage"] | null }) {
  if (!record) return <p className="text-sm leading-7 text-secondary">尚無已記錄來源頁面；略過／未設定工作不會製造 NO_DATA 或確認沒有股息。</p>;
  return <div className="grid min-w-0 gap-4">
    <p className="text-sm leading-7">最近已記錄頁面：{fetchLabels[record.state]}。這不是目前即時健康或完整覆蓋證明。</p>
    <SourceValues values={[
      ["已記錄執行識別碼", record.id], ["該次證券", <SecurityName id={record.security_id} />],
      ["該次操作", operationLabels[record.operation]], ["該次凍結來源代號", record.provider_symbol ?? "未解析；不由目前代號猜測"],
      ["該頁要求範圍（非已覆蓋期間）", `${record.window.start} 至 ${record.window.end}`],
      ["此頁解析／後續頁", `${record.parse_complete ? "此頁解析完整" : "未完整解析"}；${record.has_more ? "仍有後續頁" : "未提供後續頁"}`],
      ["實際 HTTP 記錄", record.attempted ? `有記錄要求；HTTP ${record.http_status ?? "狀態未知"}` : "未記錄 HTTP 要求"],
      ["本頁有效列（非新增數）", String(record.observations_count)],
      ["本頁格式問題／範圍外排除", `${record.issue_count} / ${record.filtered_count}`],
      ["安全錯誤碼", record.error_code ?? "未提供"], ["原始完成時間（UTC）", record.finished_at],
      ["該次來源指標", <SourceLink url={record.source_url} label="檢視來源指標" />],
    ]} />
  </div>;
}

export function ObservationDetails({ record }: { record: Observation }) {
  const dividend = isDividend(record);
  return <article aria-label={`${record.provider_symbol} ${record.id}`} className="grid min-w-0 gap-4 break-words">
    <h3 className="font-semibold">{providerNames[record.provider_id]} · 觀測時代號 {record.provider_symbol}</h3>
    <p className="break-all text-sm leading-7">{dividend ? `每股來源金額：${observationTerms(record)}` : `精確來源因子：${observationTerms(record)}（未採納股數）`}</p>
    <p className="text-sm leading-7 text-secondary">SOURCE_ONLY / NOT_ADOPTED。{dividend ? "來源列不是已驗證股息、應收或實收。" : "PROVIDER_ADJUSTMENT_ONLY；不改變持股、成本或現金。"}</p>
    {dividend && record.incomplete_reasons.length > 0 && <p className="text-sm leading-7 text-danger">尚待核對：{record.incomplete_reasons.map(code => incompleteLabels[code] ?? code).join("；")}。</p>}
    <SourceValues values={[
      ["目前目錄證券（非來源代號）", <SecurityName id={record.security_id} />], ["來源穩定分組", `${record.origin_group}；Yahoo 包裝不另計獨立來源，人工引用不是供應商第二票`],
      [dividend ? "除淨日期" : "因子生效日期", dividend ? record.ex_date ?? "未知，不用擷取日代替" : record.effective_date],
      ...(dividend ? [
        ["宣告／記錄／付款日期", `${record.announce_date ?? "未知"} / ${record.record_date ?? "未知"} / ${record.pay_date ?? "未知"}`],
        ["調整基準", record.adjustment_basis === "AS_DECLARED" ? "來源／人工稱宣告條款，仍未採納或驗證" : record.adjustment_basis === "SPLIT_ADJUSTED" ? "拆股調整後金額，不能直接認列" : "UNKNOWN，調整基準未知"],
        ["來源幣別標籤／期間／頻率", `${record.source_currency ?? "未知"} / ${record.source_period ?? "未知"} / ${record.source_frequency ?? "未知"}；不據此推定一般股息`],
      ] as [string, ReactNode][] : [["來源調整類型", record.source_adjustment_type ?? "未知，不推定公司條款"]] as [string, ReactNode][]),
      ["觀測識別碼", record.id], ["首次執行識別碼", record.provider_run_id], ["來源事件識別碼", record.provider_event_id ?? "未提供"],
      ["首次擷取時間（UTC）", record.retrieved_at], ["重複觀測次數／最近時間", `${record.sightings_count} 次 / ${record.last_seen_at}；不代表多筆收入`],
      ["原始 assertion 雜湊（非驗證結論）", record.normalized_sha256], ["來源指標", <SourceLink url={record.source_url} label="檢視原始來源指標" />],
    ]} />
    {record.evidence && <section aria-label="人工引用證據" className="grid gap-3 rounded-md border border-border p-4">
      <h4 className="font-medium">人工文件引用，未驗證文件內容</h4><SourceValues values={[["引用識別碼", record.evidence.id], ["引用參考", record.evidence.citation], ["人工說明", record.evidence.reason]]} />
      {record.evidence.url_withheld ? <p className="text-sm leading-7 text-secondary">引用包含查詢或片段，連結未提供；原始引用與雜湊不變。請以引用識別碼及參考核對文件。</p> : <SourceLink url={record.evidence.url ?? null} label="開啟引用文件" />}
    </section>}
    <Link to={`/settings/securities/${record.security_id}`} className="inline-flex min-h-11 items-center text-sm text-primary underline">核對穩定證券身分及代號期間</Link>
  </article>;
}
