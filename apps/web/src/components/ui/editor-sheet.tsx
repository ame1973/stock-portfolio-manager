import { useRef, useState, type ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { FormSavedContext } from "../../lib/form-lifecycle";
import { EditorCloseContext } from "../../lib/editor-lifecycle";
import { Button } from "./button";

export function EditorSheet({ title, children, onClose, successFocus = "[data-editor-create]" }: {
  title: string; children: ReactNode; onClose: () => void; successFocus?: string;
}) {
  const [trigger] = useState(() => document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const content = useRef<HTMLDivElement>(null);
  const saved = useRef(false);
  const [waiting, setWaiting] = useState(false);
  const busy = () => !!content.current?.querySelector('form[aria-busy="true"]');
  function close() { if (busy()) setWaiting(true); else onClose(); }
  return <FormSavedContext value={() => { saved.current = true; }}>
    <Dialog.Root open onOpenChange={value => { if (!value) close(); }}>
      <Dialog.Portal>
        {/* A plain backdrop avoids Radix Overlay's CSP-blocked scroll-lock injection. */}
        <div aria-hidden="true" className="fixed inset-0 z-40 bg-black/50" />
        <Dialog.Content ref={content} data-editor-sheet="" className="fixed inset-0 z-50 flex flex-col overflow-hidden bg-surface lg:left-auto lg:w-[min(48rem,70vw)] lg:border-l lg:border-border lg:shadow-xl"
          onOpenAutoFocus={event => { event.preventDefault(); content.current?.querySelector<HTMLButtonElement>("[data-editor-close]")?.focus(); }}
          onEscapeKeyDown={event => { if (busy()) { event.preventDefault(); setWaiting(true); } }}
          onInteractOutside={event => { event.preventDefault(); if (busy()) setWaiting(true); }}
          onCloseAutoFocus={event => {
            event.preventDefault();
            const stable = document.querySelector<HTMLElement>(successFocus);
            const candidate = saved.current ? stable : trigger?.isConnected ? trigger : stable;
            const target = candidate === document.body || candidate?.matches(":disabled,[aria-disabled='true']") ? null : candidate;
            target?.focus();
            if (!target || document.activeElement !== target) document.querySelector<HTMLElement>("#content")?.focus();
          }}>
          <div className="grid shrink-0 gap-3 border-b border-border p-4 sm:p-6">
            <div className="flex items-start justify-between gap-4"><Dialog.Title className="text-xl font-semibold">{title}</Dialog.Title><Button data-editor-close variant="outline" onClick={close}>關閉編輯</Button></div>
            <Dialog.Description className="text-sm leading-6 text-secondary">取消或 Escape 會離開編輯；儲存中請等待伺服器確認，不會離線排程或自動宣稱成功。</Dialog.Description>
            {waiting && <p role="status" className="text-sm text-secondary">如正在處理，請等候回應後再關閉；輸入與操作識別碼會保留。</p>}
          </div>
          <div className="editor-sheet-body min-h-0 flex-1 overflow-y-auto overscroll-contain p-4 sm:p-6"><EditorCloseContext value={close}>{children}</EditorCloseContext></div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  </FormSavedContext>;
}
