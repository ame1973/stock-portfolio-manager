import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { api } from "@spm/contracts";
import { describe, expect, it, vi } from "vitest";
import { SecuritySettingsPage } from "./SecuritySettingsPage";

const PASSWORD = "synthetic-security-passphrase-2026";
const NEW_PASSWORD = "synthetic-replacement-passphrase-2026";
const csrf = { csrf_token: "synthetic-csrf-token", expires_at: "2099-01-01T00:00:00Z" };
const session = { id: "77777777-7777-4777-8777-777777777777", current: true, created_at: new Date().toISOString(), last_seen_at: new Date().toISOString(), expires_at: "2099-01-01T00:00:00Z", revoked_at: null };
const reply = <T,>(data: T) => ({ data, response: new Response() });
function renderSettings() {
  vi.spyOn(api, "GET").mockImplementation(async path => path === "/api/v1/auth/csrf" ? reply(csrf) : reply({ items: [session], next_cursor: null }));
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  cache.setQueryData(["owner"], { id: "synthetic-owner" });
  cache.setQueryData(["accounts"], { private: "synthetic-only" });
  render(<QueryClientProvider client={cache}><SecuritySettingsPage /></QueryClientProvider>);
  return cache;
}

describe("password and session settings", () => {
  it("requires matching password confirmation before a request", async () => {
    const post = vi.spyOn(api, "POST");
    renderSettings();
    fireEvent.change(screen.getByLabelText("舊密碼"), { target: { value: PASSWORD } });
    fireEvent.change(screen.getByLabelText("新密碼", { exact: true }), { target: { value: NEW_PASSWORD } });
    fireEvent.change(screen.getByLabelText("再次輸入新密碼"), { target: { value: "different-synthetic-password" } });
    fireEvent.click(screen.getByRole("button", { name: "變更密碼並登出所有裝置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("新密碼不一致");
    expect(post).not.toHaveBeenCalled();
  });

  it("clears private state only after the server confirms a password change", async () => {
    const post = vi.spyOn(api, "POST").mockResolvedValue(reply({ ok: true, message: "Changed" }));
    const cache = renderSettings();
    fireEvent.change(screen.getByLabelText("舊密碼"), { target: { value: PASSWORD } });
    fireEvent.change(screen.getByLabelText("新密碼", { exact: true }), { target: { value: NEW_PASSWORD } });
    fireEvent.change(screen.getByLabelText("再次輸入新密碼"), { target: { value: NEW_PASSWORD } });
    fireEvent.click(screen.getByRole("button", { name: "變更密碼並登出所有裝置" }));
    await waitFor(() => expect(cache.getQueryData(["owner"])).toBeNull());
    expect(cache.getQueryData(["accounts"])).toBeUndefined();
    expect(screen.getByLabelText("舊密碼")).toHaveValue("");
    expect(screen.getByLabelText("新密碼", { exact: true })).toHaveValue("");
    expect(post).toHaveBeenCalledWith("/api/v1/auth/change-password", { params: { header: { "X-CSRF-Token": csrf.csrf_token, "Idempotency-Key": expect.any(String) } }, body: { old_password: PASSWORD, new_password: NEW_PASSWORD } });
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state.data))).not.toContain("synthetic-security-passphrase");
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
  });

  it("keeps a valid session and input after an incorrect reauthentication password", async () => {
    vi.spyOn(api, "POST").mockResolvedValue({ error: { code: "UNAUTHORIZED", message: "Wrong password", request_id: "test", details: {} }, response: new Response(null, { status: 401 }) });
    const cache = renderSettings();
    const expired = vi.fn();
    window.addEventListener("spm:session-expired", expired);
    try {
      fireEvent.change(screen.getByLabelText("目前密碼"), { target: { value: "incorrect-synthetic-password" } });
      fireEvent.click(screen.getByRole("button", { name: "確認目前密碼" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("密碼不正確");
      expect(cache.getQueryData(["owner"])).not.toBeNull();
      expect(expired).not.toHaveBeenCalled();
      expect(screen.getByLabelText("目前密碼")).toHaveValue("incorrect-synthetic-password");
    } finally { window.removeEventListener("spm:session-expired", expired); }
  });

  it("clears the password and reports only confirmed reauthentication", async () => {
    vi.spyOn(api, "POST").mockResolvedValue(reply({ ok: true, message: "OK" }));
    const cache = renderSettings();
    fireEvent.change(screen.getByLabelText("目前密碼"), { target: { value: PASSWORD } });
    fireEvent.click(screen.getByRole("button", { name: "確認目前密碼" }));
    expect(await screen.findByText(/已重新驗證/)).toBeVisible();
    expect(screen.getByLabelText("目前密碼")).toHaveValue("");
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    expect(JSON.stringify(cache.getQueryCache().getAll().map(query => query.state.data))).not.toContain(PASSWORD);
  });

  it("keeps a failed revocation dialog open and retries with the same intent key", async () => {
    const remove = vi.spyOn(api, "DELETE").mockRejectedValueOnce(new TypeError("Connection failed")).mockResolvedValueOnce(reply({ ok: true, message: "OK" }));
    const cache = renderSettings();
    fireEvent.click(await screen.findByRole("button", { name: "撤銷目前登入" }));
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "確認" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("網絡連線中斷");
    expect(cache.getQueryData(["owner"])).not.toBeNull();
    fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "確認" }));
    await waitFor(() => expect(cache.getQueryData(["owner"])).toBeNull());
    expect(remove).toHaveBeenCalledTimes(2);
    expect(remove.mock.calls[0]).toEqual(remove.mock.calls[1]);
    expect(cache.getQueryData(["accounts"])).toBeUndefined();
  });
});
