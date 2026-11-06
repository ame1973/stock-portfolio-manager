import { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Field, FormFrame } from "../forms/fields";
import { FilterChips } from "../forms/filter-chips";
import { useDesktop } from "../../lib/use-desktop";
import { useFormAction } from "../../features/settings/data";
import { Button } from "./button";
import { EditorSheet } from "./editor-sheet";
import { FinancialTable, type LedgerColumn } from "./financial-table";

afterEach(() => vi.unstubAllGlobals());

const rows = [
  { id: "newer", date: "2050-01-02", amount: "9007199254740.000001", ratio: "9223372036854775807", booked: null },
  { id: "older", date: "2026-01-02", amount: "2.000001", ratio: "9223372036854775806", booked: "0.000000" },
];
const columns: LedgerColumn<typeof rows[number]>[] = [
  { accessorKey: "date", header: "日期" },
  { accessorKey: "amount", header: "原幣金額" },
  { accessorKey: "ratio", header: "精確比例" },
  { id: "booked", header: "歷史 HKD", cell: ({ row }) => row.original.booked ?? "未知，不是零" },
];

describe("server-owned financial table", () => {
  it("renders exact strings and the supplied page order without sorting, totaling or inventing missing amounts", () => {
    render(<FinancialTable title="測試事實" data={rows} columns={columns}>{record => <article>{record.id}</article>}</FinancialTable>);
    const table = screen.getByRole("table");
    expect(within(table).getByRole("caption")).toHaveTextContent("本頁 2 筆，非全部紀錄");
    expect(within(table).getByRole("columnheader", { name: "日期" })).toHaveAttribute("aria-sort", "descending");
    const rendered = within(table).getAllByRole("row").slice(1);
    expect(rendered[0]).toHaveTextContent("2050-01-02");
    expect(rendered[0]).toHaveTextContent("9007199254740.000001");
    expect(rendered[0]).toHaveTextContent("9223372036854775807");
    expect(rendered[0]).toHaveTextContent("未知，不是零");
    expect(rendered[1]).toHaveTextContent("9223372036854775806");
    expect(rendered[1]).toHaveTextContent("0.000000");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("describes ascending source orders without changing the server rows or financial defaults", () => {
    render(<FinancialTable title="來源診斷" data={rows} columns={columns} order={{ description: "來源 ID／證券 ID 升序", direction: "ascending" }}>{record => <article>{record.id}</article>}</FinancialTable>);
    expect(screen.getByRole("caption")).toHaveTextContent("來源 ID／證券 ID 升序");
    expect(screen.getByRole("columnheader", { name: "日期" })).toHaveAttribute("aria-sort", "ascending");
    expect(screen.getAllByRole("row")[1]).toHaveTextContent("2050-01-02");
  });

  it("expands only one physical record and retains the same detail/action renderer", () => {
    render(<FinancialTable title="測試事實" data={rows} columns={columns}>{record => <article aria-label={record.id}>原值 {record.ratio}<Button>依據更正</Button></article>}</FinancialTable>);
    fireEvent.click(screen.getAllByRole("button", { name: "查看原值與操作" })[0]!);
    const collapse = screen.getByRole("button", { name: "收合原值與操作" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById(collapse.getAttribute("aria-controls")!)).toContainElement(screen.getByRole("article", { name: "newer" }));
    fireEvent.click(screen.getByRole("button", { name: "查看原值與操作" }));
    expect(screen.queryByRole("article", { name: "newer" })).not.toBeInTheDocument();
    expect(screen.getByRole("article", { name: "older" })).toHaveTextContent("9223372036854775806");
    fireEvent.click(screen.getByRole("button", { name: "收合原值與操作" }));
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });
});

function ScreenSize() { return <p>{useDesktop() ? "桌面表格" : "手機卡片"}</p>; }

describe("responsive ledger breakpoint", () => {
  it("uses cards when matchMedia is unavailable", () => {
    vi.stubGlobal("matchMedia", undefined);
    render(<ScreenSize />);
    expect(screen.getByText("手機卡片")).toBeVisible();
  });

  it("tracks media changes and removes its subscription when unmounted", () => {
    const target = new EventTarget();
    let desktop = false;
    const remove = vi.fn(target.removeEventListener.bind(target));
    const media = vi.fn((query: string) => ({
      get matches() { return desktop; }, media: query,
      addEventListener: target.addEventListener.bind(target), removeEventListener: remove,
    }));
    vi.stubGlobal("matchMedia", media);
    const mounted = render(<ScreenSize />);
    expect(media).toHaveBeenCalledWith("(min-width: 1024px)");
    act(() => { desktop = true; target.dispatchEvent(new Event("change")); });
    expect(screen.getByText("桌面表格")).toBeVisible();
    act(() => { desktop = false; target.dispatchEvent(new Event("change")); });
    expect(screen.getByText("手機卡片")).toBeVisible();
    mounted.unmount();
    expect(remove).toHaveBeenCalled();
  });
});

function SavedForm({ pending, fail, close }: { pending: boolean; fail: boolean; close: () => void }) {
  const action = useFormAction(close);
  return <FormFrame title="原始測試表單" error={action.error} pending={pending} onChange={action.changed} onCancel={close} onSubmit={event => {
    event.preventDefault();
    void action.run(async () => { if (fail) throw new TypeError("Synthetic write failure"); return undefined; });
  }}><Field label="精確輸入" defaultValue="9223372036854775807" /></FormFrame>;
}
function SheetExample({ pending = false, fail = false, refreshOnClose = false }: { pending?: boolean; fail?: boolean; refreshOnClose?: boolean }) {
  const [open, setOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const close = () => { setOpen(false); if (refreshOnClose) setRefreshing(true); };
  return <main id="content" tabIndex={-1}>
    <Button data-editor-create disabled={refreshing} onClick={() => setOpen(true)}>新增測試</Button>
    <Button disabled={refreshing} onClick={() => setOpen(true)}>更正測試</Button>
    {open && <EditorSheet title="編輯測試事實" onClose={close}><SavedForm pending={pending} fail={fail} close={close} /></EditorSheet>}
  </main>;
}
async function openSheet(label = "更正測試") {
  const opener = screen.getByRole("button", { name: label });
  opener.focus();
  fireEvent.click(opener);
  await screen.findByRole("dialog", { name: "編輯測試事實" });
  return opener;
}

describe("financial editor sheet lifecycle", () => {
  it("uses a labelled modal and returns cancellation/Escape focus to the observed opener", async () => {
    render(<SheetExample />);
    const opener = await openSheet();
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-describedby");
    expect(screen.getByRole("button", { name: "關閉編輯" })).toHaveFocus();
    fireEvent.change(screen.getByLabelText("精確輸入"), { target: { value: "9223372036854775806" } });
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
    await openSheet();
    expect(screen.getByLabelText("精確輸入")).toHaveValue("9223372036854775807");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("refuses top close and Escape while a real child form reports a pending write", async () => {
    const mounted = render(<SheetExample pending />);
    await openSheet();
    expect(screen.getByRole("form")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "關閉編輯" }));
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("請等候回應後再關閉");
    expect(screen.getByLabelText("精確輸入")).toHaveValue("9223372036854775807");
    mounted.rerender(<SheetExample pending={false} />);
    fireEvent.keyDown(document.activeElement!, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("moves confirmed success to a stable create control, not a removed/revised physical row", async () => {
    render(<SheetExample />);
    await openSheet();
    fireEvent.submit(screen.getByRole("form"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "新增測試" })).toHaveFocus());
  });

  it("returns focus to stable content when a necessary refetch temporarily disables the opener", async () => {
    render(<SheetExample refreshOnClose />);
    const opener = await openSheet();
    fireEvent.click(screen.getByRole("button", { name: "關閉編輯" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(opener).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
  });

  it("does not treat a rejected write as saved and retains input before explicit cancellation", async () => {
    render(<SheetExample fail />);
    const opener = await openSheet();
    fireEvent.submit(screen.getByRole("form"));
    expect(await screen.findByRole("alert")).toHaveTextContent("網絡連線中斷");
    expect(screen.getByLabelText("精確輸入")).toHaveValue("9223372036854775807");
    fireEvent.click(screen.getByRole("button", { name: "關閉編輯" }));
    await waitFor(() => expect(opener).toHaveFocus());
  });
});

describe("explicit filter chips", () => {
  it("omits inactive filters and removes exactly the selected string/boolean criterion", () => {
    const remove = vi.fn();
    render(<FilterChips values={{ date: "2026-01-02", history: true, empty: "", inactive: false }} labels={{ date: "起日", history: "歷史修訂", empty: "空值", inactive: "停用" }} onRemove={remove} />);
    expect(screen.getAllByRole("button")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "清除此篩選：歷史修訂" }));
    expect(remove).toHaveBeenCalledWith("history");
    fireEvent.click(screen.getByRole("button", { name: "清除此篩選：起日" }));
    expect(remove).toHaveBeenLastCalledWith("date");
  });
});
