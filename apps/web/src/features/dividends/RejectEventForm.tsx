import { useContext, useId, useRef, useState } from "react";
import { replaceEqualDeep } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { SecurityName } from "../ledger/ReferenceName";
import { useFormAction } from "../settings/data";
import { decisionCitation, decisionReason, decisionUrl, validTarget } from "./event-validation";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
const schema = z.object({
  citation: decisionCitation, evidence_url: decisionUrl, reason: decisionReason,
  acknowledged: z.boolean().refine(Boolean, "請確認拒絕所示事件版本及其限制。"),
});

export function RejectEventForm({ target: observed, onSaved, onCancel, disabled = false, onPendingChange }: {
  target: Event; onSaved: (record: Event) => void; onCancel: () => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void;
}) {
  const [target] = useState(() => structuredClone(observed));
  const action = useFormAction(onSaved);
  const submitted = useRef<string | null>(null);
  const closeEditor = useContext(EditorCloseContext);
  const confirmationErrorId = useId();
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema), defaultValues: { citation: "", evidence_url: "", reason: "", acknowledged: false },
  });
  return <FormFrame title="拒絕股息事件版本" submitLabel="記錄拒絕決定" error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在記錄…"} onCancel={closeEditor ?? onCancel}
    onSubmit={event => {
      if (disabled) { event.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        const body: Schemas["RejectDividendRevision"] = {
          revision_id: target.current_revision_id, decision: "REJECT_REVISION", citation: values.citation,
          evidence_url: values.evidence_url || null, reason: values.reason,
        };
        const intent = JSON.stringify({ version: target.version, body });
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (!validTarget({ event: target, rejection: null })) throw new Error("固定的事件範圍不符或版本已達上限；請取消並重新載入核對。");
          const header = await mutationHeaders(key);
          const result = checked(await api.POST("/api/v1/dividends/{event_id}/verify", {
            params: { path: { event_id: target.id }, header: { ...header, "If-Match": `"${target.version}"` } }, body,
          }));
          if (replaceEqualDeep(target, result) !== target) throw new Error("伺服器回覆與固定的事件版本不符；請保留輸入並核對拒絕歷史，不會假定未儲存。");
          return result;
        });
      })(event).finally(() => onPendingChange?.(false));
    }}>
    <div className="grid gap-3 text-sm leading-7 sm:col-span-2"><p>這次決定只針對開啟時所示的事件與實體版本；伺服器會再次核對，事件版本和 ETag 不會因拒絕而改變。</p>
      <p className="break-all">事件 {target.id}；版本 {target.version}；實體修訂 {target.current_revision_id}。</p>
      <p>除淨日 {target.ex_date}；每股 {target.currency} {target.dps}；這是事件條款，不是收入。</p>
      <div>固定證券：<SecurityName id={target.security_id} /><span className="break-all text-secondary">{target.security_id}</span></div>
      <p>拒絕不等於撤回事件、核實文件、確認持倉或沖銷實收。已有應收或投影歷史時，伺服器會阻止此決定；不會改寫現金或歷史匯率。</p>
      <p>回應未確認時請保留視窗，以相同內容及操作識別碼重試；若關閉，請先核對拒絕歷史，不要假定未曾記錄。</p></div>
    <Field label="拒絕引用參考" maxLength={602} {...register("citation")} error={errors.citation?.message} help="填寫文件名稱及頁碼等依據；不代表系統已驗證文件。" />
    <Field label="拒絕引用網址（選填）" maxLength={4098} {...register("evidence_url")} error={errors.evidence_url?.message} help="不會抓取；含查詢或片段的連結在讀取畫面會隱藏。不要輸入帳密或金鑰。" />
    <Field label="拒絕決定依據" maxLength={2002} {...register("reason")} error={errors.reason?.message} help="至少 10 個字元；請說明拒絕此版本的原因。" />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" aria-invalid={!!errors.acknowledged} aria-describedby={errors.acknowledged ? confirmationErrorId : undefined} {...register("acknowledged")} />我確認拒絕所示事件版本，並明白這不是撤回、文件驗證、持倉資格或實收沖銷。</label>
      {errors.acknowledged && <p id={confirmationErrorId} role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
