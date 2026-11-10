import { useContext, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, failureMessage, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { RecordList } from "../settings/RecordList";
import { useFormAction, usePageState } from "../settings/data";
import { reason } from "./inputs";

type Status = components["schemas"]["TradeHistoryReviewStatus"];
type ReviewRecord = components["schemas"]["TradeHistoryReviewRecord"];
type Write = components["schemas"]["TradeHistoryReviewWrite"];
export type ReviewIntent = Pick<Status, "trade_id" | "trade_version"> & Omit<Write, "reason">;

const gaps: Record<Write["expected_gap"], string> = {
  MISSING_CREATION_AUDIT: "找不到原始建立或修訂稽核；不能補造原始證據。",
  MISSING_RETIRED_ORIGIN: "已退役交易缺少可重建原始狀態的來源；不能猜測原始時間。",
};
const states: Record<Status["state"], string> = {
  NOT_REQUIRED: "目前沒有可記錄的缺口。",
  NOT_REVIEWED: "目前缺口尚未記錄審核。",
  ACKNOWLEDGED_UNVERIFIED: "已記錄未驗證的審核。",
  WITHDRAWN: "先前的審核決定已撤回。",
  EVIDENCE_CHANGED: "稽核證據已變更；先前的決定不代表目前證據已獲確認。",
};

function ordinal(value: string, advance = false) {
  return /^(0|[1-9][0-9]{0,18})$/.test(value) && BigInt(value) <= 9223372036854775807n - (advance ? 1n : 0n);
}

function coherent(record: Status, tradeId: string) {
  if (record.trade_id !== tradeId || !ordinal(record.review_revision)) return false;
  const latest = record.latest;
  if (!latest) return record.review_revision === "0" && (
    (record.state === "NOT_REVIEWED" && record.current_gap !== null) ||
    (record.state === "NOT_REQUIRED" && record.current_gap === null)
  );
  if (latest.trade_id !== tradeId || latest.revision !== record.review_revision) return false;
  if (record.state === "WITHDRAWN") return latest.decision === "WITHDRAW";
  if (latest.decision !== "ACKNOWLEDGE_UNVERIFIED") return false;
  return (record.state === "ACKNOWLEDGED_UNVERIFIED" && latest.gap === record.current_gap) ||
    (record.state === "EVIDENCE_CHANGED" && latest.gap !== record.current_gap);
}

function ReviewHistory({ tradeId, revision }: { tradeId: string; revision: string }) {
  const page = usePageState();
  const result = useQuery({ queryKey: ["ledger", "trade-history-review", tradeId, "revisions", revision, page.cursor],
    queryFn: async ({ signal }) => {
      const response = checked(await api.GET("/api/v1/trades/{id}/history-review/revisions", {
        params: { path: { id: tradeId }, query: { cursor: page.cursor, limit: 25 } }, signal,
      }));
      if (response.items.some(item => item.trade_id !== tradeId)) throw new Error("審核歷史的交易識別碼不符；請重新載入。");
      return response;
    } });
  return <RecordList title="審核歷史" result={result} page={page} searchable={false} emptyMessage="目前沒有審核決定。">{item =>
    <article className="grid gap-2 text-sm leading-7"><h4 className="font-semibold">版本 {item.revision}：{item.decision === "WITHDRAW" ? "撤回" : "記錄未驗證審核"}</h4>
      <p>{gaps[item.gap]}</p><p className="break-words">依據：{item.reason}</p>
      <p className="text-secondary">記錄時間（UTC）：{item.created_at}</p></article>
  }</RecordList>;
}

export function TradeHistoryReviewPanel({ tradeId, disabled, onReview }: {
  tradeId: string; disabled: boolean; onReview: (intent: ReviewIntent) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [history, setHistory] = useState(false);
  const review = useQuery({ queryKey: ["ledger", "trade-history-review", tradeId], enabled: expanded,
    queryFn: async ({ signal }) => checked(await api.GET("/api/v1/trades/{id}/history-review", { params: { path: { id: tradeId } }, signal })) });
  const record = review.data;
  const valid = record && coherent(record, tradeId);
  const decision = record?.state === "NOT_REVIEWED" || record?.state === "WITHDRAWN"
    ? "ACKNOWLEDGE_UNVERIFIED" : record?.state === "ACKNOWLEDGED_UNVERIFIED" || record?.state === "EVIDENCE_CHANGED" ? "WITHDRAW" : null;
  const gap = decision === "WITHDRAW" ? record?.latest?.gap : record?.current_gap;
  const safeVersion = record && Number.isSafeInteger(record.trade_version) && record.trade_version > 0 && ordinal(record.review_revision, true);
  return <details className="rounded-md border border-border p-4" onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary className="min-h-11 cursor-pointer py-2 text-sm font-medium">未驗證交易歷史審核</summary>
    <div className="grid gap-4 text-sm leading-7">
      <p className="text-secondary">只記錄你對缺失證據的決定，不是所有權、完整持倉或股息收入證明。目前不會解除交易更正或作廢限制，也不會新增現金或股息。</p>
      <div><Button type="button" variant="outline" onClick={() => void review.refetch()} disabled={!expanded || review.isFetching}>重新載入審核狀態</Button></div>
      {review.isPending && expanded && <p role="status">正在核對交易歷史審核…</p>}
      {review.isError && <div role="alert" className="text-danger"><p>無法安全核對審核狀態；不能視為已確認。{failureMessage(review.error)}</p>
        <Button type="button" variant="outline" onClick={() => void review.refetch()}>重試核對審核狀態</Button></div>}
      {record && !review.isError && !valid && <p role="alert" className="text-danger">審核狀態與交易識別碼或歷史版本不符；請重新載入，不會採用這個回覆。</p>}
      {record && !review.isError && valid && <>
        <p>{states[record.state]}</p>
        {record.current_gap && <p>{gaps[record.current_gap]}</p>}
        {record.latest && <p className="break-words text-secondary">最近決定：{record.latest.decision === "WITHDRAW" ? "撤回" : "未驗證審核"}；版本 {record.latest.revision}；依據：{record.latest.reason}</p>}
        {decision && gap && (safeVersion
          ? <div><Button type="button" variant="outline" disabled={disabled || review.isFetching || review.isPaused} onClick={() => onReview({ trade_id: record.trade_id, trade_version: record.trade_version, expected_revision: record.review_revision, expected_gap: gap, decision })}>
            {decision === "WITHDRAW" ? "撤回未驗證審核" : "記錄未驗證審核"}</Button></div>
          : <p role="alert" className="text-danger">交易或審核版本超出可精確處理的範圍；請以受保護 API 核對，不會猜測版本。</p>)}
        <details onToggle={event => setHistory(event.currentTarget.open)}><summary className="min-h-11 cursor-pointer py-2 font-medium">查看審核決定歷史</summary>
          {history && <ReviewHistory key={record.review_revision} tradeId={tradeId} revision={record.review_revision} />}</details>
      </>}
    </div>
  </details>;
}

const schema = z.object({
  reason: reason.refine(value => !value.includes("\0"), "依據不能包含 NUL 字元。"),
  acknowledged: z.boolean().refine(value => value, "請先確認這項決定的範圍。"),
});

export function TradeHistoryReviewForm({ intent, onSaved, onCancel, disabled = false, onPendingChange }: {
  intent: ReviewIntent; onSaved: (record: ReviewRecord) => void; onCancel: () => void;
  disabled?: boolean; onPendingChange?: (pending: boolean) => void;
}) {
  const action = useFormAction(onSaved);
  const submittedReason = useRef<string | null>(null);
  const closeEditor = useContext(EditorCloseContext);
  const withdrawing = intent.decision === "WITHDRAW";
  const title = withdrawing ? "撤回未驗證審核" : "記錄未驗證審核";
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema), defaultValues: { reason: "", acknowledged: false },
  });
  return <FormFrame title={title} submitLabel={title} error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在儲存…"} onCancel={closeEditor ?? onCancel}
    onSubmit={event => {
      if (disabled) { event.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        if (submittedReason.current !== null && submittedReason.current !== values.reason) action.changed();
        submittedReason.current = values.reason;
        return action.run(async key => {
          if (!Number.isSafeInteger(intent.trade_version) || intent.trade_version < 1 || !ordinal(intent.expected_revision, true)) {
            throw new Error("交易或審核版本超出可精確處理的範圍；請以受保護 API 核對。");
          }
          const header = { ...await mutationHeaders(key), "If-Match": `"${intent.trade_version}"` };
          return checked(await api.POST("/api/v1/trades/{id}/history-review", {
            params: { path: { id: intent.trade_id }, header },
            body: { decision: intent.decision, expected_revision: intent.expected_revision, expected_gap: intent.expected_gap, reason: values.reason },
          }));
        });
      })(event).finally(() => onPendingChange?.(false));
    }}>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">{gaps[intent.expected_gap]} 這項操作只留下可撤回的決定及審計，不會補造歷史或改寫交易、現金與股息。</p>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">回覆未確認時請保持視窗並以相同內容重試。關閉視窗會清除輸入及操作識別碼；如已關閉，請先核對審核歷史。</p>
    <Field label="審核依據" maxLength={1000} {...register("reason")} error={errors.reason?.message}
      help={`使用開啟時觀察的交易版本 ${intent.trade_version}、審核版本 ${intent.expected_revision} 和缺口；未確認的重試保留原操作識別碼。請勿填寫密碼或 API 金鑰。`} />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input type="checkbox" className="mt-2 shrink-0" {...register("acknowledged")} />我明白這項決定{withdrawing ? "只撤回先前審核，不會抹除歷史或證明原始稽核存在。" : "不是所有權、完整持倉或股息實收證明，也不會解除交易更正或作廢限制。"}</label>
      {errors.acknowledged && <p role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
