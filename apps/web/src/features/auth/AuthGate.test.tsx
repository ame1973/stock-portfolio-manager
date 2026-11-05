import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { AuthGate } from "./AuthGate";

const owner = { id: "11111111-1111-4111-8111-111111111111", created_at: "2026-11-04T00:00:00Z", is_active: true };
const setupData = (initialized: boolean) => ({ data: { initialized, requires_token: !initialized }, response: new Response() });
const unauthorized = { error: { code: "UNAUTHORIZED", message: "Sign in", request_id: "test", details: {} }, response: new Response(null, { status: 401 }) };

function renderGate() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={cache}><AuthGate>{session => (
    <div>
      <p>私人內容 {session.owner.id}</p>
      <button onClick={() => void session.logout()} disabled={session.loggingOut}>登出</button>
      {session.logoutError && <p role="alert">{session.logoutError}</p>}
    </div>
  )}</AuthGate></QueryClientProvider>);
  return cache;
}

describe("owner authentication", () => {
  it("does not render private content while loading or when the server fails", async () => {
    vi.spyOn(api, "GET").mockRejectedValue(new Error("連線中斷"));
    renderGate();
    expect(screen.queryByText(/私人內容/)).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("無法連接帳簿");
    expect(screen.getByRole("button", { name: "重新連接" })).toBeVisible();
  });

  it("onboards with an explicit token and does not cache the password", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(setupData(false));
    const post = vi.spyOn(api, "POST").mockResolvedValue({ data: owner, response: new Response() });
    const cache = renderGate();
    await screen.findByRole("heading", { name: "建立私人帳戶" });
    fireEvent.change(screen.getByLabelText(/初始化 token/), { target: { value: "synthetic-setup-token" } });
    fireEvent.change(screen.getByLabelText(/設定密碼/), { target: { value: "synthetic-passphrase-2026" } });
    fireEvent.change(screen.getByLabelText("再次輸入密碼"), { target: { value: "synthetic-passphrase-2026" } });
    fireEvent.click(screen.getByRole("button", { name: "建立帳戶" }));
    expect(await screen.findByText(/私人內容/)).toBeVisible();
    expect(post).toHaveBeenCalledWith("/api/v1/setup/initialize", { body: { setup_token: "synthetic-setup-token", password: "synthetic-passphrase-2026" } });
    const cached = JSON.stringify(cache.getQueryCache().getAll().map(query => query.state.data));
    expect(cached).not.toContain("synthetic-passphrase");
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
  });

  it("rejects mismatched password confirmation before contacting the API", async () => {
    vi.spyOn(api, "GET").mockResolvedValue(setupData(false));
    const post = vi.spyOn(api, "POST");
    renderGate();
    await screen.findByRole("heading", { name: "建立私人帳戶" });
    fireEvent.change(screen.getByLabelText(/初始化 token/), { target: { value: "synthetic-setup-token" } });
    fireEvent.change(screen.getByLabelText(/設定密碼/), { target: { value: "synthetic-passphrase-2026" } });
    fireEvent.change(screen.getByLabelText("再次輸入密碼"), { target: { value: "different-passphrase-2026" } });
    fireEvent.click(screen.getByRole("button", { name: "建立帳戶" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("兩次輸入的密碼不一致");
    expect(post).not.toHaveBeenCalled();
  });

  it("handles login rejection without showing private content", async () => {
    vi.spyOn(api, "GET").mockResolvedValueOnce(setupData(true)).mockResolvedValueOnce(unauthorized);
    vi.spyOn(api, "POST").mockResolvedValue(unauthorized);
    renderGate();
    await screen.findByRole("heading", { name: "登入持倉簿" });
    fireEvent.change(screen.getByLabelText("密碼"), { target: { value: "incorrect-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登入" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("密碼不正確");
    expect(screen.queryByText(/私人內容/)).not.toBeInTheDocument();
  });

  it("logs out through CSRF and clears private query data", async () => {
    vi.spyOn(api, "GET")
      .mockResolvedValueOnce(setupData(true))
      .mockResolvedValueOnce({ data: owner, response: new Response() })
      .mockResolvedValueOnce({ data: { csrf_token: "synthetic-csrf-token", expires_at: "2026-12-01T00:00:00Z" }, response: new Response() });
    const post = vi.spyOn(api, "POST").mockResolvedValue({ data: { ok: true, message: "OK" }, response: new Response() });
    const cache = renderGate();
    await screen.findByText(/私人內容/);
    cache.setQueryData(["private-holdings"], { secret: "synthetic-only" });
    fireEvent.click(screen.getByRole("button", { name: "登出" }));
    expect(await screen.findByRole("heading", { name: "登入持倉簿" })).toBeVisible();
    expect(cache.getQueryData(["private-holdings"])).toBeUndefined();
    expect(post).toHaveBeenCalledWith("/api/v1/auth/logout", { params: { header: { "X-CSRF-Token": "synthetic-csrf-token", "Idempotency-Key": expect.any(String) } } });
    await waitFor(() => expect(cache.getQueryData(["owner"])).toBeNull());
  });

  it("purges private data on an expired-session event", async () => {
    vi.spyOn(api, "GET").mockResolvedValueOnce(setupData(true)).mockResolvedValueOnce({ data: owner, response: new Response() });
    const cache = renderGate();
    await screen.findByText(/私人內容/);
    cache.setQueryData(["accounts"], { private: "synthetic-only" });
    act(() => { window.dispatchEvent(new Event("spm:session-expired")); });
    expect(await screen.findByRole("heading", { name: "登入持倉簿" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("未儲存的內容不會自動入帳");
    expect(cache.getQueryData(["accounts"])).toBeUndefined();
    expect(cache.getQueryData(["owner"])).toBeNull();
  });

  it("drops stale private state when session revalidation returns 401", async () => {
    vi.spyOn(api, "GET").mockResolvedValueOnce(setupData(true)).mockResolvedValueOnce(unauthorized);
    const cache = renderGate();
    cache.setQueryData(["accounts"], { private: "synthetic-only" });
    await screen.findByRole("heading", { name: "登入持倉簿" });
    await waitFor(() => expect(cache.getQueryData(["accounts"])).toBeUndefined());
  });

  it("does not claim logout succeeded when the network failed", async () => {
    vi.spyOn(api, "GET")
      .mockResolvedValueOnce(setupData(true))
      .mockResolvedValueOnce({ data: owner, response: new Response() })
      .mockResolvedValueOnce({ data: { csrf_token: "synthetic-csrf-token", expires_at: "2026-12-01T00:00:00Z" }, response: new Response() });
    vi.spyOn(api, "POST").mockRejectedValue(new TypeError("Network failed"));
    const cache = renderGate();
    await screen.findByText(/私人內容/);
    cache.setQueryData(["accounts"], { private: "synthetic-only" });
    fireEvent.click(screen.getByRole("button", { name: "登出" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("網絡連線中斷");
    expect(screen.getByText(/私人內容/)).toBeVisible();
    expect(cache.getQueryData(["accounts"])).toBeDefined();
  });
});
