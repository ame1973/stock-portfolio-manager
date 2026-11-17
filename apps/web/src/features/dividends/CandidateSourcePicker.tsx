import { useCallback } from "react";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { date, price } from "../ledger/inputs";
import { usePageState } from "../settings/data";
import { basisLabels, eventTypeLabels } from "./event-labels";
import { eventTypes } from "./event-validation";
import { usePrivatePage } from "./use-private-page";

type Observation = components["schemas"]["DividendObservationRecord"];
type SourcePin = { observation_id: string; source_sha256: string };
type SourcePage = components["schemas"]["ObservationPage_DividendObservationRecord_"];
const sourceSchema = z.object({ id: z.uuid(), security_id: z.uuid(), normalized_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  provider_id: z.enum(["eodhd", "alphavantage", "massive", "yahoo", "yfinance", "hkex_evidence", "sec_evidence", "manual"]),
  provider_symbol: z.string(), origin_group: z.string(), ex_date: date.nullable(), dps: price.nullable(), currency: z.enum(["HKD", "USD"]).nullable(),
  event_type: z.enum(eventTypes), adjustment_basis: z.enum(["AS_DECLARED", "SPLIT_ADJUSTED", "UNKNOWN"]), incomplete_reasons: z.array(z.string()),
  provenance_version: z.literal(1), scope: z.literal("SOURCE_ONLY"), canonical_adoption: z.literal("NOT_ADOPTED"),
  canonical_adoption_scope: z.literal("RESPONSE_ONLY_NOT_LINKAGE") });

function sourcePage(value: SourcePage, security: string): SourcePage {
  const ids = new Set<string>();
  if (!value || value.scope !== "IMMUTABLE_SOURCE_V1" || !Number.isSafeInteger(value.legacy_records_omitted) || value.legacy_records_omitted < 0
    || !Array.isArray(value.items) || value.items.length > 25 || (value.next_cursor != null && typeof value.next_cursor !== "string")
    || value.items.some(record => {
      if (!sourceSchema.safeParse(record).success || record.security_id !== security || ids.has(record.id.toLowerCase())) return true;
      ids.add(record.id.toLowerCase()); return false;
    })) throw new Error("來源清單無法核對；請重新載入或檢查伺服器狀態及有效備份。");
  return value;
}

export function CandidateSourcePicker({ securityId, selectedIds, full, onSelect }: {
  securityId: string; selectedIds: string[]; full: boolean; onSelect: (pin: SourcePin) => void;
}) {
  const page = usePageState();
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => sourcePage(checked(await api.GET("/api/v1/dividends/observations", { params: { query: {
    security_id: securityId, cursor, limit: 25,
  } }, signal })), securityId), [securityId]);
  const result = usePrivatePage(securityId, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError;
  return <section aria-label="選擇已記錄來源觀測" className="grid min-w-0 gap-4 rounded-md border border-border p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-medium">選擇已記錄來源觀測</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => void result.refetch()}>重新載入可選來源</Button></div>
    <p className="text-sm leading-7 text-secondary">請逐筆核對並明確加入；只帶入觀測識別碼及原始雜湊，不填入候選條款、基準或關聯依據。來源不是文件驗證或收入。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；已載入的來源不能用作新選擇。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入可選來源…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對來源，完成前不能加入。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下保留上次載入內容，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => void result.refetch()}>重試載入可選來源</Button></div>}
    {result.data && <><p className="text-sm leading-7 text-secondary">略過 {result.data.legacy_records_omitted} 筆舊來源；未列出不代表沒有來源或股息。</p>
      {result.data.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有可選來源；不代表沒有股息。</p>
        : <ul className="grid gap-4">{result.data.items.map((record: Observation) => {
          const selected = selectedIds.includes(record.id.toLowerCase());
          return <li key={record.id}><article aria-label={`可選來源 ${record.id}`} className="grid min-w-0 gap-3 rounded-md border border-border p-3 text-sm leading-7">
            <p>{record.provider_id}；觀測時代號 {record.provider_symbol}；來源分組 {record.origin_group}。</p>
            <p>除淨日 {record.ex_date ?? "未知"}；每股 {record.currency ?? "幣別未知"} {record.dps ?? "未知"}；類型 {eventTypeLabels[record.event_type]}；口徑 {basisLabels[record.adjustment_basis]}。</p>
            <p className="break-all">觀測 {record.id}；原始來源雜湊 {record.normalized_sha256}。</p>
            <p>未完整原因：{record.incomplete_reasons.join("、") || "未列明"}。回應標記不是事件關聯查詢，不證明文件或收入。</p>
            <div><Button type="button" variant="outline" disabled={!ready || selected || full} onClick={() => onSelect({ observation_id: record.id, source_sha256: record.normalized_sha256 })}>{selected ? "已列入來源" : "加入此來源"}</Button></div>
          </article></li>;
        })}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={!ready || page.number === 1} onClick={page.previous}>上一頁來源</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆，非全部來源。</p>
        <Button type="button" variant="outline" disabled={!ready || !result.data.next_cursor} onClick={() => { if (result.data?.next_cursor) page.next(result.data.next_cursor); }}>下一頁來源</Button></div></>}
  </section>;
}
