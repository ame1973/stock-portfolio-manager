import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import { App } from "./App";
import type { ReactNode } from "react";
import type { AuthSession } from "./features/auth/AuthGate";

vi.mock("./features/auth/AuthGate", () => ({
  AuthGate: ({ children }: { children: (session: AuthSession) => ReactNode }) => children({
    owner: { id: "11111111-1111-4111-8111-111111111111", created_at: "2026-11-04T00:00:00Z", is_active: true },
    logout: vi.fn(), loggingOut: false, logoutError: null,
  }),
}));

function renderApp(path = "/") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>);
}

describe("service status", () => {
  it("shows a loading state without fabricated financial data", () => {
    vi.spyOn(api, "GET").mockImplementation(() => new Promise(() => {}));
    renderApp();
    expect(screen.getByRole("status")).toHaveTextContent("正在檢查");
    expect(screen.getByRole("button", { name: "重新檢查" })).toBeDisabled();
  });

  it("shows the actual ready response", async () => {
    vi.spyOn(api, "GET").mockResolvedValue({ data: { status: "ready" }, response: new Response() });
    renderApp();
    expect(await screen.findByText("已連線，資料庫已就緒")).toBeVisible();
    expect(screen.getByText(/目前為開發版本/)).toBeVisible();
  });

  it("keeps the page usable after failure and supports retry", async () => {
    const get = vi.spyOn(api, "GET")
      .mockRejectedValueOnce(new Error("網絡連線中斷"))
      .mockResolvedValueOnce({ data: { status: "ready" }, response: new Response() });
    renderApp();
    expect(await screen.findByRole("alert")).toHaveTextContent("網絡連線中斷");
    fireEvent.click(screen.getByRole("button", { name: "重新檢查" }));
    expect(await screen.findByText("已連線，資料庫已就緒")).toBeVisible();
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("does not treat an HTTP error as successful readiness", async () => {
    vi.spyOn(api, "GET").mockResolvedValue({
      error: { code: "NOT_READY", message: "Database is not ready", request_id: "test", details: {} },
      response: new Response(null, { status: 503 }),
    });
    renderApp();
    expect(await screen.findByRole("alert")).toHaveTextContent("服務未就緒");
  });
});

describe("financial workspace navigation", () => {
  function reads() {
    vi.spyOn(api, "GET").mockImplementation(async path => ({ data: path === "/api/v1/health/ready" ? { status: "ready" } : { items: [], next_cursor: null }, response: new Response() }));
  }

  it("has eight working desktop destinations and exactly five mobile destinations with labelled collapsed icons", async () => {
    // Load the real lazy route before measuring navigation, independently of Vite's cold transform time.
    await import("./features/ledger/TradesPage");
    reads();
    renderApp();
    await screen.findByText("已連線，資料庫已就緒");
    const desktop = screen.getByRole("navigation", { name: "主要導覽" });
    const mobile = screen.getByRole("navigation", { name: "手機主要導覽" });
    expect(within(desktop).getAllByRole("link")).toHaveLength(8);
    expect(within(mobile).getAllByRole("link")).toHaveLength(5);
    expect(within(mobile).getByRole("link", { name: "股息事件" })).toHaveAttribute("href", "/dividends");
    expect(within(desktop).getByRole("link", { name: "服務連線" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(screen.getByRole("button", { name: "收合導覽" }));
    expect(screen.getByRole("button", { name: "展開導覽" })).toHaveAttribute("aria-expanded", "false");
    expect(within(desktop).getByRole("link", { name: "交易紀錄" })).toHaveAttribute("href", "/transactions");
    fireEvent.click(within(desktop).getByRole("link", { name: "交易紀錄" }));
    expect(await screen.findByRole("heading", { name: "交易紀錄" })).toBeVisible();
    expect(within(desktop).getByRole("link", { name: "交易紀錄" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("keeps dividends and corporate actions reachable through five mobile destinations", async () => {
    reads();
    renderApp("/transactions");
    await screen.findByRole("heading", { name: "交易紀錄" });
    const mobile = screen.getByRole("navigation", { name: "手機主要導覽" });
    fireEvent.click(within(mobile).getByRole("link", { name: "股息事件" }));
    expect(await screen.findByRole("heading", { name: "股息事件" })).toBeVisible();
    expect(within(mobile).getByRole("link", { name: "股息事件" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(within(mobile).getByRole("link", { name: "交易紀錄" }));
    await screen.findByRole("heading", { name: "交易紀錄" });
    fireEvent.click(screen.getByRole("link", { name: "核對企業行動" }));
    expect(await screen.findByRole("heading", { name: "企業行動" })).toBeVisible();
  });

  it("opens the secondary candidate workspace without adding a mobile destination", async () => {
    await import("./features/dividends/DividendEventsPage");
    await import("./features/dividends/DividendCandidatesPage");
    reads(); renderApp("/dividends");
    fireEvent.click(await screen.findByRole("link", { name: "查看股息候選資料" }));
    expect(await screen.findByRole("heading", { name: "股息候選資料" })).toBeVisible();
    const mobile = screen.getByRole("navigation", { name: "手機主要導覽" });
    expect(within(mobile).getAllByRole("link")).toHaveLength(5);
    expect(within(mobile).getByRole("link", { name: "股息事件" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(screen.getByRole("link", { name: "返回股息事件" }));
    expect(await screen.findByRole("heading", { name: "股息事件" })).toBeVisible();
  });

  it("reaches the data center from settings on mobile and isolates dividend/split memory filters", async () => {
    reads();
    renderApp("/settings/portfolios");
    const settings = await screen.findByRole("navigation", { name: "設定分類" });
    fireEvent.click(within(settings).getByRole("link", { name: "資料中心與來源擷取" }));
    await screen.findByRole("heading", { name: "來源設定與候選能力" });
    const tabs = screen.getByRole("navigation", { name: "資料中心分類" });
    fireEvent.click(within(tabs).getByRole("link", { name: "股息觀測" }));
    await screen.findByRole("heading", { name: "股息來源觀測" });
    fireEvent.change(screen.getByLabelText("除淨起日"), { target: { value: "2026-01-02" } });
    fireEvent.change(screen.getByLabelText("搜尋股息來源觀測"), { target: { value: "frozen source" } });
    fireEvent.click(within(tabs).getByRole("link", { name: "拆股因子" }));
    await screen.findByRole("heading", { name: "provider-only 拆股因子" });
    expect(screen.getByLabelText("生效起日")).toHaveValue("");
    expect(screen.getByLabelText("搜尋provider-only 拆股因子")).toHaveValue("");
    fireEvent.click(within(tabs).getByRole("link", { name: "股息觀測" }));
    await screen.findByRole("heading", { name: "股息來源觀測" });
    expect(screen.getByLabelText("除淨起日")).toHaveValue("2026-01-02");
    expect(screen.getByLabelText("搜尋股息來源觀測")).toHaveValue("frozen source");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it("restores financial filters/search when navigating back and removes individual chips without browser persistence", async () => {
    reads();
    renderApp("/transactions");
    await screen.findByRole("heading", { name: "交易紀錄" });
    fireEvent.change(screen.getByLabelText("交易起日"), { target: { value: "2026-01-02" } });
    fireEvent.change(screen.getByLabelText("搜尋交易"), { target: { value: "synthetic evidence" } });
    fireEvent.click(screen.getByLabelText("包括已被取代的修訂"));
    const nav = screen.getByRole("navigation", { name: "主要導覽" });
    fireEvent.click(within(nav).getByRole("link", { name: "現金帳" }));
    await screen.findByRole("heading", { name: "現金紀錄" });
    fireEvent.click(within(nav).getByRole("link", { name: "交易紀錄" }));
    await screen.findByRole("heading", { name: "交易紀錄" });
    expect(screen.getByLabelText("交易起日")).toHaveValue("2026-01-02");
    expect(screen.getByLabelText("搜尋交易")).toHaveValue("synthetic evidence");
    expect(screen.getByLabelText("包括已被取代的修訂")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "清除此篩選：交易起日" }));
    expect(screen.getByLabelText("交易起日")).toHaveValue("");
    expect(screen.getByLabelText("包括已被取代的修訂")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "清除搜尋：synthetic evidence" }));
    expect(screen.getByLabelText("搜尋交易")).toHaveValue("");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
});
