import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import type { ReactNode } from "react";
import type { AuthSession } from "./features/auth/AuthGate";

vi.mock("./features/auth/AuthGate", () => ({
  AuthGate: ({ children }: { children: (session: AuthSession) => ReactNode }) => children({
    owner: { id: "11111111-1111-4111-8111-111111111111", created_at: "2026-11-04T00:00:00Z", is_active: true },
    logout: vi.fn(), loggingOut: false, logoutError: null,
  }),
}));

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><App /></QueryClientProvider>);
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
