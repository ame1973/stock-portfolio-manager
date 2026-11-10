import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, failureMessage } from "../../lib/api";
import { coherentCorrectionScope, correctionScopeSchema, type CorrectionOperation, type CorrectionScope } from "./trade-correction-validation";
import type { Trade } from "./TradeForm";

const states: Record<components["schemas"]["TradeCorrectionScopeGap"]["state"], string> = {
  NOT_REVIEWED: "此交易的建立稽核缺口尚未記錄審核。",
  ACKNOWLEDGED_UNVERIFIED: "已記錄未驗證審核；不代表原始稽核已找到。",
  WITHDRAWN: "此交易先前的審核已撤回，不能用於新的更正。",
  EVIDENCE_CHANGED: "稽核證據已變更；不能以先前決定進行新的更正。",
};

export function TradeCorrectionScopePanel({ record, disabled, onSelect }: {
  record: Trade; disabled: boolean; onSelect: (operation: CorrectionOperation, scope: CorrectionScope) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const active = record.status !== "SUPERSEDED" && record.status !== "VOID";
  const query = useQuery({ queryKey: ["ledger", "trade-correction-scope", record.id], enabled: expanded && active, staleTime: 0,
    queryFn: async ({ signal }) => checked(await api.GET("/api/v1/trades/{id}/unverified-corrections/scope", {
      params: { path: { id: record.id } }, signal,
    })) });
  const parsed = correctionScopeSchema.safeParse(query.data);
  const scope = parsed.success && coherentCorrectionScope(parsed.data, record) ? parsed.data : null;
  const ready = scope && scope.gaps.length > 0 && scope.gaps.every(gap => gap.state === "ACKNOWLEDGED_UNVERIFIED" && gap.current_gap === "MISSING_CREATION_AUDIT");
  if (!active) return null;
  function select(operation: CorrectionOperation) {
    if (!scope || !ready || disabled || query.isFetching || query.isPaused || query.isError) return;
    onSelect(operation, {
      trade_version: scope.trade_version,
      reviews: scope.gaps.map(gap => ({ trade_id: gap.trade_id, trade_version: gap.trade_version,
        review_revision: gap.review_revision, gap: "MISSING_CREATION_AUDIT" })),
    });
  }
  return <details open={expanded} className="rounded-md border border-border p-4">
    <summary className="min-h-11 cursor-pointer py-2 text-sm font-medium" onClick={event => { event.preventDefault(); setExpanded(previous => !previous); }}>未驗證更正範圍</summary>
    <div className="grid gap-4 text-sm leading-7">
      <p className="text-secondary">這是整條交易修訂鏈的資料庫缺口，不是所有權或股息資格證明。每個缺口都須先記錄未驗證審核；更正仍需要另外確認。</p>
      <div><Button type="button" variant="outline" onClick={() => void query.refetch()} disabled={!expanded || query.isFetching}>重新載入更正範圍</Button></div>
      {query.isPending && expanded && <p role="status">正在核對整條交易修訂鏈…</p>}
      {query.isPaused && <p role="status">目前離線，不能採用先前範圍；請連線後重新核對。</p>}
      {query.isError && <div role="alert" className="text-danger"><p>無法安全核對更正範圍；請重新載入或檢查交易歷史。{failureMessage(query.error)}</p></div>}
      {query.data && !query.isError && !scope && <p role="alert" className="text-danger">無法安全核對更正範圍：交易、版本或審核資料不一致；請重新載入。</p>}
      {scope && !query.isError && <>
        {scope.gaps.length === 0 ? <p>目前沒有需要此流程處理的建立稽核缺口；這不代表歷史完整。</p> : <>
          <p>需核對 {scope.gaps.length} 筆交易缺口；版本及審核序號會在開啟表單時固定。</p>
          <ul className="grid gap-3">{scope.gaps.map(gap => <li key={gap.trade_id} className="rounded-md bg-muted p-3">
            <p className="break-all font-medium">交易 {gap.trade_id}</p>
            <p>交易版本 {gap.trade_version}；審核版本 {gap.review_revision}</p><p>{states[gap.state]}</p>
          </li>)}</ul>
          {!ready && <p className="text-secondary">請在交易修訂鏈中逐筆核對審核狀態；證據已變更時不能以舊決定繼續。</p>}
          {ready && <div className="flex flex-wrap gap-3">
            <Button type="button" variant="outline" disabled={disabled || query.isFetching || query.isPaused} onClick={() => select("REVISE")}>未驗證更正交易</Button>
            <Button type="button" variant="outline" disabled={disabled || query.isFetching || query.isPaused} onClick={() => select("VOID")}>未驗證作廢交易</Button>
          </div>}
        </>}
      </>}
    </div>
  </details>;
}
