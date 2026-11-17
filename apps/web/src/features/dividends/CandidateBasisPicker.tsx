import { useCallback } from "react";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { date, ratio } from "../ledger/inputs";
import { usePageState } from "../settings/data";
import { usePrivatePage } from "./use-private-page";

type Basis = components["schemas"]["DividendBasisRecord"];
type BasisPage = components["schemas"]["Page_DividendBasisRecord_"];
const actionSchema = z.object({ action_id: z.uuid(), action_root_id: z.uuid(), security_id: z.uuid(), action_type: z.enum(["SPLIT", "REVERSE_SPLIT"]),
  effective_date: date, ratio_numerator: ratio, ratio_denominator: ratio, observed_status: z.literal("CONFIRMED"), observed_lifecycle_status: z.literal("CURRENT"),
  observed_version: z.number().refine(value => Number.isInteger(value) && value >= 1) });
const basisSchema = z.object({ id: z.uuid(), observation_id: z.uuid(), security_id: z.uuid(), source_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  ex_date: date, share_basis_date: date, source_basis: z.literal("AFTER_EFFECTIVE_DATE_ACTIONS"), target_basis: z.literal("PRE_EX_DATE_EVENTS"),
  complete: z.literal(true), split_only: z.literal(true), actions: z.array(actionSchema).max(100),
  citation: z.object({ id: z.uuid(), citation: z.string(), reason: z.string(), url_withheld: z.boolean() }), created_at: z.string(),
  scope: z.literal("BASIS_DECLARATION_ONLY"), canonical_adoption: z.literal("NOT_ADOPTED"), canonical_adoption_scope: z.literal("RESPONSE_ONLY_NOT_LINKAGE"),
  current_use_scope: z.literal("REVALIDATION_REQUIRED_FOR_NEW_ADOPTION"), market_history: z.literal("OWNER_ASSERTED_NOT_VERIFIED") });

function basisPage(value: BasisPage, security: string, observation: string, sha256: string): BasisPage {
  const ids = new Set<string>();
  if (!value || !Array.isArray(value.items) || value.items.length > 25 || (value.next_cursor != null && typeof value.next_cursor !== "string")
    || value.items.some(record => {
      if (!basisSchema.safeParse(record).success || record.security_id.toLowerCase() !== security.toLowerCase()
        || record.observation_id.toLowerCase() !== observation.toLowerCase() || record.source_sha256 !== sha256
        || record.citation.id.toLowerCase() !== record.id.toLowerCase() || ids.has(record.id.toLowerCase())) return true;
      const actions = new Set<string>(); const roots = new Set<string>();
      if (record.actions.some(action => {
        const inside = record.share_basis_date >= record.ex_date
          ? action.effective_date >= record.ex_date && action.effective_date <= record.share_basis_date
          : action.effective_date > record.share_basis_date && action.effective_date < record.ex_date;
        const numerator = BigInt(action.ratio_numerator); const denominator = BigInt(action.ratio_denominator);
        if (action.security_id.toLowerCase() !== security.toLowerCase() || actions.has(action.action_id.toLowerCase())
          || roots.has(action.action_root_id.toLowerCase()) || !inside
          || (action.action_type === "SPLIT" ? numerator <= denominator : numerator >= denominator)) return true;
        actions.add(action.action_id.toLowerCase()); roots.add(action.action_root_id.toLowerCase()); return false;
      })) return true;
      ids.add(record.id.toLowerCase()); return false;
    })) throw new Error("基準聲明清單無法核對；請重新載入或檢查來源及有效備份。");
  return value;
}

export function CandidateBasisPicker({ securityId, observationId, sourceSha256, selectedId, usedIds, onSelect }: {
  securityId: string; observationId: string; sourceSha256: string; selectedId: string; usedIds: string[]; onSelect: (id: string) => void;
}) {
  const page = usePageState();
  const load = useCallback(async (cursor: string | undefined, signal: AbortSignal) => basisPage(checked(await api.GET("/api/v1/dividends/basis-assertions", { params: { query: {
    security_id: securityId, observation_id: observationId, cursor, limit: 25,
  } }, signal })), securityId, observationId, sourceSha256), [securityId, observationId, sourceSha256]);
  const result = usePrivatePage(`${securityId}:${observationId}:${sourceSha256}`, page.cursor, load);
  const ready = !!result.data && !result.isFetching && !result.isPaused && !result.isError;
  return <section aria-label="選擇已記錄基準聲明" className="grid min-w-0 gap-4 rounded-md border border-border p-4 sm:col-span-2">
    <div className="flex flex-wrap items-center justify-between gap-3"><h4 className="font-medium">選擇已記錄基準聲明</h4>
      <Button type="button" variant="outline" disabled={result.isFetching} onClick={() => void result.refetch()}>重新載入可選基準</Button></div>
    <p className="text-sm leading-7 text-secondary">這些是已記錄的擁有人聲明，並非目前有效性或市場覆蓋證明。伺服器會在新候選寫入時重新核對來源及行動；不會自動採用。</p>
    {result.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；已載入的基準不能用作新選擇。</p>
      : result.isPending ? <p role="status" className="text-sm text-secondary">正在載入可選基準…</p>
        : result.isFetching ? <p role="status" className="text-sm text-secondary">正在重新核對基準，完成前不能選擇。</p> : null}
    {result.isError && <div role="alert" className="grid gap-3 text-sm text-danger"><p>{failureMessage(result.error)}</p>
      {result.data && <p>以下保留上次載入內容，尚未重新核對。</p>}
      <Button type="button" variant="outline" onClick={() => void result.refetch()}>重試載入可選基準</Button></div>}
    {result.data && <>{result.data.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有基準聲明；不代表沒有拆股或已確認完整歷史。</p>
      : <ul className="grid gap-4">{result.data.items.map((record: Basis) => {
        const selected = record.id.toLowerCase() === selectedId.trim().toLowerCase();
        const used = usedIds.some(id => id.trim().toLowerCase() === record.id.toLowerCase());
        return <li key={record.id}><article aria-label={`可選基準 ${record.id}`} className="grid min-w-0 gap-3 rounded-md border border-border p-3 text-sm leading-7">
          <p className="break-all">聲明 {record.id}；來源觀測 {record.observation_id}；原始雜湊 {record.source_sha256}。</p>
          <p>除淨日 {record.ex_date}；來源股數基準日 {record.share_basis_date}；擁有人聲稱完整拆股範圍，不是市場歷史或文件驗證。</p>
          <p>引用參考：{record.citation.citation}；聲明依據：{record.citation.reason}。{record.citation.url_withheld ? "私密引用連結未提供。" : "此處不開啟或抓取引用。"}</p>
          {record.actions.length === 0 ? <p>此聲明未列拆股行動；不代表已確認沒有拆股。</p>
            : <ul className="grid gap-2">{record.actions.map(action => <li key={action.action_id} className="break-all">行動 {action.action_id}；生效日 {action.effective_date}；{action.action_type === "SPLIT" ? "拆股" : "合股"}；精確比例 {action.ratio_numerator} / {action.ratio_denominator}。</li>)}</ul>}
          <p>行動版本由伺服器重新核對，此處不以瀏覽器數字顯示。回應標記不是目前或過往事件關聯查詢。</p>
          <div><Button type="button" variant="outline" disabled={!ready || selected || used} onClick={() => onSelect(record.id)}>{selected ? "已選此基準" : used ? "其他來源已選此基準" : "使用此基準聲明"}</Button></div>
        </article></li>;
      })}</ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={!ready || page.number === 1} onClick={page.previous}>上一頁基準</Button>
        <p className="text-sm text-secondary">第 {page.number} 頁；本頁 {result.data.items.length} 筆，非全部聲明。</p>
        <Button type="button" variant="outline" disabled={!ready || !result.data.next_cursor} onClick={() => { if (result.data?.next_cursor) page.next(result.data.next_cursor); }}>下一頁基準</Button></div></>}
  </section>;
}
