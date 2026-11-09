import { useContext, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { checked, failureMessage, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { useFormAction } from "../settings/data";
import { reason } from "./inputs";

export type TradeOrigin = components["schemas"]["TradeAuditOriginRecord"];
type Trade = components["schemas"]["TradeRecord"];

const labels: Record<TradeOrigin["origin"], string> = {
  MISSING: "尚未留存稽核來源；不能推定原始稽核或原始回應是否存在。",
  AT_CREATION: "已保存建立時的稽核來源。",
  BEFORE_RETIRE: "已保存更正或作廢前的稽核來源。",
  RETAINED_RESPONSE: "已保存與歷史稽核雜湊相符的原始回應來源。",
};

export function TradeOriginPanel({ tradeId, disabled, onRecover }: {
  tradeId: string; disabled: boolean; onRecover: (origin: TradeOrigin) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const origin = useQuery({ queryKey: ["ledger", "trade-audit-origin", tradeId], enabled: expanded,
    queryFn: async ({ signal }) => checked(await api.GET("/api/v1/trades/{id}/audit-origin", { params: { path: { id: tradeId } }, signal })) });
  const record = origin.data;
  return <details className="rounded-md border border-border p-4" onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary className="min-h-11 cursor-pointer py-2 text-sm font-medium">交易稽核來源</summary>
    <div className="grid gap-4 text-sm leading-7">
      <p className="text-secondary">只檢查保留的資料庫紀錄，不是外部所有權證明、完整持倉、股息應收或實收。</p>
      {origin.isPending && expanded && <p role="status">正在核對稽核來源…</p>}
      {origin.isError && <div role="alert" className="text-danger"><p>無法核對稽核來源；不能當成缺失或已驗證。{failureMessage(origin.error)}</p>
        <Button type="button" variant="outline" onClick={() => void origin.refetch()}>重試核對稽核來源</Button></div>}
      {record && !origin.isError && record.trade_id !== tradeId && <p role="alert" className="text-danger">稽核來源的交易識別碼不符；請重新載入，不會採用這個回覆。</p>}
      {record && !origin.isError && record.trade_id === tradeId && <>
        <p>{labels[record.origin]}</p>
        {record.captured_at && <p className="text-secondary">留存時間（UTC）：{record.captured_at}</p>}
        {record.origin === "MISSING" && (record.trade_status === "SUPERSEDED" || record.trade_status === "VOID") &&
          (Number.isSafeInteger(record.trade_version) && record.trade_version > 0
            ? <div><Button type="button" variant="outline" disabled={disabled} onClick={() => onRecover(record)}>恢復交易稽核來源</Button></div>
            : <p role="alert" className="text-danger">交易版本超出瀏覽器精確整數範圍；請以原始伺服器回應和受保護 API 核對，不會猜測版本。</p>)}
        {record.origin === "MISSING" && record.trade_status !== "SUPERSEDED" && record.trade_status !== "VOID" &&
          <p className="text-secondary">目前版本不能使用已退役回應恢復；若原始稽核缺失，須另行明確審核，不會補造證據。</p>}
      </>}
    </div>
  </details>;
}

function originalObject(value: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null;
  } catch { return null; }
}

function recoverySchema(tradeId: string) {
  return z.object({
    original: z.string().min(1, "請貼上原始交易回應。")
      .max(24_000_000, "原始回應過大；請核對伺服器的請求上限。")
      .refine(value => originalObject(value) !== null, "請提供完整 JSON 物件。"),
    reason: reason.refine(value => !value.includes("\0"), "依據不能包含 NUL 字元。"),
    acknowledged: z.boolean().refine(value => value, "請先確認這不是所有權或收入證明。"),
  }).superRefine((values, context) => {
    const object = originalObject(values.original);
    if (object && object.id !== tradeId) context.addIssue({ code: "custom", path: ["original"], message: "原始回應的交易識別碼不符。" });
  });
}

export function TradeOriginRecoveryForm({ origin, onSaved, onCancel, disabled = false, onPendingChange }: {
  origin: TradeOrigin; onSaved: (origin: TradeOrigin) => void; onCancel: () => void;
  disabled?: boolean; onPendingChange?: (pending: boolean) => void;
}) {
  const action = useFormAction(onSaved);
  const closeEditor = useContext(EditorCloseContext);
  const schema = recoverySchema(origin.trade_id);
  const identifier = useId();
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema), defaultValues: { original: "", reason: "", acknowledged: false },
  });
  return <FormFrame title="恢復交易稽核來源" submitLabel="比對並留存稽核來源" error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在儲存…"} onCancel={closeEditor ?? onCancel}
    onSubmit={event => {
      if (disabled) { event.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => action.run(async key => {
        if (!Number.isSafeInteger(origin.trade_version) || origin.trade_version < 1) {
          throw new Error("交易版本超出瀏覽器精確整數範圍；請以原始伺服器回應和受保護 API 核對。");
        }
        const parsed = originalObject(values.original);
        if (!parsed || parsed.id !== origin.trade_id) throw new Error("原始回應的交易識別碼不符。");
        const header = { ...await mutationHeaders(key), "If-Match": `"${origin.trade_version}"` };
        // Keep the supplied JSON text: parsing and re-stringifying can round an INT64 sequence.
        return checked(await api.POST("/api/v1/trades/{id}/audit-origin/recover", {
          params: { path: { id: origin.trade_id }, header },
          body: { original_response: parsed as unknown as Trade, reason: values.reason },
          bodySerializer: () => `{"original_response":${values.original},"reason":${JSON.stringify(values.reason)}}`,
        }));
      }))(event).finally(() => onPendingChange?.(false));
    }}>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">請貼上建立或修訂當時收到的完整原始交易 JSON。不能使用現在的歷史列猜測原始時間；伺服器會逐項核對與後續修訂／作廢雜湊，不會改寫交易、現金或舊審計。</p>
    <div className="grid content-start gap-2 sm:col-span-2">
      <label htmlFor={identifier} className="text-sm font-medium">原始交易回應 JSON</label>
      <textarea id={identifier} className="field min-h-40 resize-y" spellCheck={false} autoComplete="off"
        aria-invalid={!!errors.original} aria-describedby={`${identifier}-help${errors.original ? ` ${identifier}-error` : ""}`}
        {...register("original", { onChange: action.changed })} />
      <p id={`${identifier}-help`} className="text-sm leading-6 text-secondary">只留在這個表單記憶體與送出的私密請求，不寫入離線儲存；請勿貼上密碼或 API 金鑰。</p>
      {errors.original && <p role="alert" id={`${identifier}-error`} className="text-sm text-danger">{errors.original.message}</p>}
    </div>
    <Field label="恢復依據" maxLength={1000} {...register("reason", { onChange: action.changed })} error={errors.reason?.message} help={`使用開啟時觀察的實體交易版本 ${origin.trade_version}；未確認的重試保留原操作識別碼。`} />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input type="checkbox" className="mt-2 shrink-0" {...register("acknowledged")} />我明白比對資料庫雜湊不是外部所有權、完整持倉或股息實收的證明。</label>
      {errors.acknowledged && <p role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
