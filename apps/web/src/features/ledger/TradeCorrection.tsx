import { useContext, useRef } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api } from "@spm/contracts";
import { Field, FormFrame } from "../../components/forms/fields";
import { checked, mutationHeaders } from "../../lib/api";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { useFormAction } from "../settings/data";
import { reason } from "./inputs";
import { checkedCorrection, validCorrectionScope, type CorrectionScope } from "./trade-correction-validation";
import type { Trade } from "./TradeForm";

export function CorrectionScopeSummary({ scope }: { scope: CorrectionScope }) {
  return <section aria-label="已固定的未驗證範圍" className="grid gap-3 rounded-md border border-border p-4 text-sm leading-7">
    <h3 className="font-semibold">已固定的未驗證範圍</h3>
    <p className="text-secondary">這些審核只供本次更正使用，不是所有權、完整持倉或股息資格證明；伺服器會再次核對全部版本。</p>
    <ul className="grid gap-2">{scope.reviews.map(pin => <li key={pin.trade_id} className="break-all">
      交易 {pin.trade_id}；版本 {pin.trade_version}；審核版本 {pin.review_revision}
    </li>)}</ul>
  </section>;
}

const voidSchema = z.object({
  reason: reason.refine(value => !value.includes("\0"), "依據不能包含 NUL 字元。"),
  acknowledged: z.boolean().refine(value => value, "請先確認這次未驗證作廢的範圍。"),
});

export function UnverifiedVoidForm({ record, scope, onSaved, onCancel, disabled = false, onPendingChange }: {
  record: Trade; scope: CorrectionScope; onSaved: (trade: Trade) => void; onCancel: () => void;
  disabled?: boolean; onPendingChange?: (pending: boolean) => void;
}) {
  const action = useFormAction(onSaved);
  const submitted = useRef<string | null>(null);
  const closeEditor = useContext(EditorCloseContext);
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<z.infer<typeof voidSchema>>({
    resolver: zodResolver(voidSchema), defaultValues: { reason: "", acknowledged: false },
  });
  return <FormFrame title="未驗證作廢交易" submitLabel="確認未驗證作廢" error={action.error} pending={isSubmitting || disabled}
    pendingLabel={disabled ? "等候密碼確認…" : "正在儲存…"} onCancel={closeEditor ?? onCancel}
    onSubmit={event => {
      if (disabled) { event.preventDefault(); return; }
      onPendingChange?.(true);
      void handleSubmit(values => {
        const intent = JSON.stringify({ scope, reason: values.reason });
        if (submitted.current !== null && submitted.current !== intent) action.changed();
        submitted.current = intent;
        return action.run(async key => {
          if (!validCorrectionScope(record, scope)) throw new Error("固定的交易或審核範圍不符；請取消並重新核對。");
          const header = { ...await mutationHeaders(key), "If-Match": `"${scope.trade_version}"` };
          const result = checked(await api.POST("/api/v1/trades/{id}/unverified-corrections/void", {
            params: { path: { id: record.id }, header },
            body: { confirmation: "UNVERIFIED_TRADE_CORRECTION_ONLY", reviews: scope.reviews, reason: values.reason },
          }));
          return checkedCorrection(result, record, "VOID");
        });
      })(event).finally(() => onPendingChange?.(false));
    }}>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">作廢會保留原紀錄及審計，已入帳交收會以原幣和歷史匯率精確沖銷；若影響後續持倉會整筆拒絕。</p>
    <p className="text-sm leading-7 text-secondary sm:col-span-2">回應未確認時請保持視窗，以相同內容和操作識別碼重試；關閉後請先核對歷史，不要假定未曾儲存。</p>
    <Field label="未驗證作廢依據" maxLength={1000} {...register("reason")} error={errors.reason?.message} help="請說明具體原因；不要輸入密碼或 API 金鑰。" />
    <div className="grid gap-2 sm:col-span-2"><label className="flex min-h-11 items-start gap-3 text-sm leading-7"><input type="checkbox" className="mt-2 shrink-0" {...register("acknowledged")} />我明白這次作廢使用未驗證的交易歷史；它不證明所有權、完整持倉或股息資格。</label>
      {errors.acknowledged && <p role="alert" className="text-sm text-danger">{errors.acknowledged.message}</p>}</div>
  </FormFrame>;
}
