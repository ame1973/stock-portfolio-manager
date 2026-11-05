import { useRef, useState } from "react";
import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { Button } from "./button";
import { failureMessage } from "../../lib/api";

export function ConfirmAction({ label, title, description, onConfirm, disabled = false }: {
  label: string; title: string; description: string; onConfirm: (key: string) => Promise<void>; disabled?: boolean;
}) {
  const key = useRef(crypto.randomUUID());
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return <AlertDialog.Root open={open} onOpenChange={value => {
    if (!pending) {
      if (value) key.current = crypto.randomUUID();
      setOpen(value); setError(null);
    }
  }}>
    <AlertDialog.Trigger asChild><Button variant="outline" disabled={disabled}>{label}</Button></AlertDialog.Trigger>
    <AlertDialog.Portal>
      {/* Radix Overlay injects scroll-lock CSS; external CSS keeps our strict CSP intact. */}
      <div aria-hidden="true" className="pointer-events-auto fixed inset-0 z-40 bg-black/50" />
      <AlertDialog.Content data-confirm-dialog="" className="fixed inset-x-4 top-1/2 z-50 mx-auto grid max-h-[85dvh] max-w-md -translate-y-1/2 gap-5 overflow-y-auto overscroll-contain rounded-xl border border-border bg-surface p-6 shadow-xl">
        <AlertDialog.Title className="text-lg font-semibold">{title}</AlertDialog.Title>
        <AlertDialog.Description className="text-sm leading-7 text-secondary">{description}</AlertDialog.Description>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <div className="flex flex-wrap gap-3">
          <AlertDialog.Cancel asChild><Button variant="outline" disabled={pending}>取消</Button></AlertDialog.Cancel>
          <AlertDialog.Action asChild><Button disabled={pending} onClick={async event => {
            event.preventDefault();
            setPending(true); setError(null);
            try { await onConfirm(key.current); setOpen(false); }
            catch (cause) { setError(failureMessage(cause)); }
            finally { setPending(false); }
          }}>{pending ? "正在處理…" : "確認"}</Button></AlertDialog.Action>
        </div>
      </AlertDialog.Content>
    </AlertDialog.Portal>
  </AlertDialog.Root>;
}
