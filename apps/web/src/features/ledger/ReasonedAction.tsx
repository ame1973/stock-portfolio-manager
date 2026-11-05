import { useState } from "react";
import { Field } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { ConfirmAction } from "../../components/ui/confirm-action";
import { reason } from "./inputs";

export function ReasonedAction({ title, label, reasonLabel, description, onConfirm, onCancel }: {
  title: string; label: string; reasonLabel: string; description: string;
  onConfirm: (reason: string, key: string) => Promise<void>; onCancel: () => void;
}) {
  const [explanation, setExplanation] = useState("");
  const valid = reason.safeParse(explanation);
  return <section aria-label={title} className="panel grid gap-5">
    <h3 className="font-semibold">{title}</h3>
    <p className="text-sm leading-7 text-secondary">{description}</p>
    <Field label={reasonLabel} value={explanation} maxLength={1000} onChange={event => setExplanation(event.target.value)} help="至少 10 個字元的具體原因；原始金融紀錄及審計不會刪除。" autoFocus />
    <div className="flex flex-wrap gap-3">
      <ConfirmAction label={label} title={`確認${title}？`} description={description} disabled={!valid.success}
        onConfirm={key => onConfirm(reason.parse(explanation), key)} />
      <Button variant="outline" onClick={onCancel}>取消操作</Button>
    </div>
  </section>;
}
