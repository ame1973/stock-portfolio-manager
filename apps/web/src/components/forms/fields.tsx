import { useId, type ComponentProps, type ReactNode } from "react";
import { Button } from "../ui/button";

export function Field({ label, error, help, id, ...props }: ComponentProps<"input"> & {
  label: string; error?: string; help?: string;
}) {
  const generated = useId();
  const identifier = id ?? generated;
  return <div className="grid gap-2">
    <label htmlFor={identifier} className="text-sm font-medium">{label}</label>
    <input {...props} id={identifier} className="field" aria-invalid={!!error}
      aria-describedby={error ? `${identifier}-error` : help ? `${identifier}-help` : undefined} />
    {help && <p id={`${identifier}-help`} className="text-sm leading-6 text-secondary">{help}</p>}
    {error && <p role="alert" id={`${identifier}-error`} className="text-sm text-danger">{error}</p>}
  </div>;
}

export function SelectField({ label, error, help, id, children, ...props }: ComponentProps<"select"> & {
  label: string; error?: string; help?: string;
}) {
  const generated = useId();
  const identifier = id ?? generated;
  return <div className="grid gap-2">
    <label htmlFor={identifier} className="text-sm font-medium">{label}</label>
    <select {...props} id={identifier} className="field" aria-invalid={!!error}
      aria-describedby={error ? `${identifier}-error` : help ? `${identifier}-help` : undefined}>{children}</select>
    {help && <p id={`${identifier}-help`} className="text-sm leading-6 text-secondary">{help}</p>}
    {error && <p role="alert" id={`${identifier}-error`} className="text-sm text-danger">{error}</p>}
  </div>;
}

export function FormFrame({ title, children, error, pending, onCancel, onSubmit, onChange }: {
  title: string; children: ReactNode; error: string | null; pending: boolean;
  onCancel: () => void; onSubmit: ComponentProps<"form">["onSubmit"]; onChange?: () => void;
}) {
  return <form aria-label={title} onSubmit={onSubmit} onChange={onChange} className="panel grid gap-5">
    <h3 className="text-lg font-semibold">{title}</h3>
    <fieldset disabled={pending} className="grid gap-5 sm:grid-cols-2">{children}</fieldset>
    {error && <p role="alert" className="text-sm leading-6 text-danger">{error}</p>}
    <div className="flex flex-wrap gap-3">
      <Button type="submit" disabled={pending}>{pending ? "正在儲存…" : "儲存"}</Button>
      <Button type="button" variant="outline" disabled={pending} onClick={onCancel}>取消</Button>
    </div>
  </form>;
}
