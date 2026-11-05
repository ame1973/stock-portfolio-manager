import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { usePageState } from "../features/settings/data";
import { ViewContext, useViewState, type Views } from "./view-state";

function View({ name }: { name: string }) {
  const [filters, setFilters] = useViewState(`filters:${name}`, { date: "", history: false });
  const page = usePageState(name);
  return <section>
    <label>生效起日<input value={filters.date} onChange={event => setFilters(previous => ({ ...previous, date: event.target.value }))} /></label>
    <label>歷史修訂<input type="checkbox" checked={filters.history} onChange={event => setFilters(previous => ({ ...previous, history: event.target.checked }))} /></label>
    <label>搜尋<input value={page.q} onChange={event => page.search(event.target.value)} /></label>
    <p>第 {page.number} 頁；游標 {page.cursor ?? "首頁"}</p>
    <button onClick={() => page.next(`synthetic-cursor-${page.number}`)}>下一頁</button><button onClick={page.previous}>上一頁</button>
    <button onClick={page.reset}>重設分頁</button>
  </section>;
}
function WorkspaceState() {
  const [views, setViews] = useState<Views>({});
  const [name, setName] = useState("trades");
  return <ViewContext value={{ views, setViews }}>
    <button onClick={() => setName(value => value === "trades" ? "cash" : "trades")}>切換工作區</button>
    <View key={name} name={name} />
  </ViewContext>;
}

describe("authenticated memory-only view state", () => {
  it("retains independent filters/search/server cursor stacks across routes without persistent storage", () => {
    const store = vi.spyOn(Storage.prototype, "setItem");
    render(<WorkspaceState />);
    fireEvent.change(screen.getByLabelText("生效起日"), { target: { value: "2026-01-02" } });
    fireEvent.click(screen.getByLabelText("歷史修訂"));
    fireEvent.change(screen.getByLabelText("搜尋"), { target: { value: "synthetic trade" } });
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }));
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }));
    expect(screen.getByText("第 3 頁；游標 synthetic-cursor-2")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "切換工作區" }));
    expect(screen.getByLabelText("生效起日")).toHaveValue("");
    expect(screen.getByLabelText("歷史修訂")).not.toBeChecked();
    expect(screen.getByText("第 1 頁；游標 首頁")).toBeVisible();
    fireEvent.change(screen.getByLabelText("搜尋"), { target: { value: "independent cash" } });
    fireEvent.click(screen.getByRole("button", { name: "切換工作區" }));
    expect(screen.getByLabelText("生效起日")).toHaveValue("2026-01-02");
    expect(screen.getByLabelText("歷史修訂")).toBeChecked();
    expect(screen.getByLabelText("搜尋")).toHaveValue("synthetic trade");
    expect(screen.getByText("第 3 頁；游標 synthetic-cursor-2")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "上一頁" }));
    expect(screen.getByText("第 2 頁；游標 synthetic-cursor-1")).toBeVisible();
    fireEvent.change(screen.getByLabelText("搜尋"), { target: { value: "changed search" } });
    expect(screen.getByText("第 1 頁；游標 首頁")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重設分頁" }));
    expect(screen.getByLabelText("搜尋")).toHaveValue("");
    expect(store).not.toHaveBeenCalled();
  });

  it("drops all views when the authenticated workspace is unmounted or belongs to another owner", () => {
    const mounted = render(<WorkspaceState key="owner-one" />);
    fireEvent.change(screen.getByLabelText("生效起日"), { target: { value: "2026-01-02" } });
    fireEvent.click(screen.getByRole("button", { name: "下一頁" }));
    mounted.rerender(<WorkspaceState key="owner-two" />);
    expect(screen.getByLabelText("生效起日")).toHaveValue("");
    expect(screen.getByLabelText("搜尋")).toHaveValue("");
    expect(screen.getByText("第 1 頁；游標 首頁")).toBeVisible();
  });

  it("keeps standalone screens local when no authenticated view provider is mounted", () => {
    const mounted = render(<View name="standalone" />);
    fireEvent.change(screen.getByLabelText("生效起日"), { target: { value: "2026-01-02" } });
    expect(screen.getByLabelText("生效起日")).toHaveValue("2026-01-02");
    mounted.unmount();
    render(<View name="standalone" />);
    expect(screen.getByLabelText("生效起日")).toHaveValue("");
  });
});
