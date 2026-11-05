import { Fragment, useId, useState, type ReactNode } from "react";
import { tableFeatures, useTable, type ColumnDef } from "@tanstack/react-table";
import { Button } from "./button";

// Server keyset pagination owns navigation; no client pagination/sort/aggregate feature is enabled.
const features = tableFeatures({});
export type LedgerColumn<T extends { id: string }> = ColumnDef<typeof features, T>;

export function FinancialTable<T extends { id: string }>({ title, data, columns, children }: {
  title: string; data: T[]; columns: LedgerColumn<T>[]; children: (record: T) => ReactNode;
}) {
  const prefix = useId();
  const [expanded, setExpanded] = useState<string | null>(null);
  const table = useTable({ features, data, columns, getRowId: row => row.id });
  return <div role="region" aria-label={`${title}伺服器分頁表格`} tabIndex={0} className="min-w-0 overflow-x-auto rounded-lg border border-border bg-surface">
    <table className="w-full min-w-[48rem] border-collapse text-left text-sm">
      <caption className="border-b border-border p-4 text-left leading-6 text-secondary">{title}：只列伺服器回傳的本頁 {data.length} 筆，非全部紀錄。生效日期／ID 倒序；不在瀏覽器重排或加總金融數值。</caption>
      <thead className="bg-muted">{table.getHeaderGroups().map(group => <tr key={group.id}>
        {group.headers.map((header, index) => <th key={header.id} scope="col" aria-sort={index === 0 ? "descending" : undefined} className="border-b border-border px-4 py-3 font-semibold">{header.isPlaceholder ? null : <table.FlexRender header={header} />}</th>)}
        <th scope="col" className="border-b border-border px-4 py-3 font-semibold">原值與操作</th>
      </tr>)}</thead>
      <tbody>{table.getRowModel().rows.map(row => <Fragment key={row.id}>
        <tr data-record-id={row.id} className="border-b border-border align-top">
          {row.getAllCells().map((cell, index) => <td key={cell.id} className={`px-4 py-4 leading-6 ${index === 0 ? "whitespace-nowrap" : "break-words"}`}><table.FlexRender cell={cell} /></td>)}
          <td className="px-4 py-3"><Button variant="outline" aria-expanded={expanded === row.id} aria-controls={`${prefix}-${row.id}`} onClick={() => setExpanded(previous => previous === row.id ? null : row.id)}>{expanded === row.id ? "收合原值與操作" : "查看原值與操作"}</Button></td>
        </tr>
        {expanded === row.id && <tr id={`${prefix}-${row.id}`}><td colSpan={columns.length + 1} className="border-b border-border bg-muted/30 p-5">{children(row.original)}</td></tr>}
      </Fragment>)}</tbody>
    </table>
  </div>;
}
