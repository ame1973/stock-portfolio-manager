import { Button } from "../ui/button";

export function FilterChips<T extends Record<string, string | boolean>>({ values, labels, onRemove }: {
  values: T; labels: Record<keyof T, string>; onRemove: (key: keyof T) => void;
}) {
  const keys = (Object.keys(values) as (keyof T & string)[]).filter(key => values[key] !== "" && values[key] !== false);
  if (keys.length === 0) return null;
  return <ul aria-label="已套用篩選（可逐項清除）" className="flex min-w-0 flex-wrap gap-2">
    {keys.map(key => <li key={key} className="min-w-0 max-w-full"><Button variant="outline" className="max-w-full gap-2 text-left" aria-label={`清除此篩選：${labels[key]}`} onClick={() => onRemove(key)}>
      <span className="min-w-0 break-all">{labels[key]}：{values[key] === true ? "已啟用" : values[key]}</span><span aria-hidden="true">×</span>
    </Button></li>)}
  </ul>;
}
