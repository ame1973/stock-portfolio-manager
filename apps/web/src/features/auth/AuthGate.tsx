import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type components } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { checked, errorMessage, failureMessage, mutationHeaders } from "../../lib/api";
import { clearSession } from "../../lib/session";

type Owner = components["schemas"]["Owner"];
export type AuthSession = {
  owner: Owner;
  logout: () => Promise<void>;
  loggingOut: boolean;
  logoutError: string | null;
};

export function AuthGate({ children }: { children: (session: AuthSession) => ReactNode }) {
  const cache = useQueryClient();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const setup = useQuery({
    queryKey: ["setup"], retry: false,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/setup/status");
      if (!data || error) throw new Error(errorMessage(error));
      return data;
    },
  });
  const owner = useQuery({
    queryKey: ["owner"], enabled: setup.data?.initialized === true, retry: false, staleTime: 30_000,
    queryFn: async ({ signal }) => {
      const { data, error, response } = await api.GET("/api/v1/auth/me", { signal });
      if (response.status === 401) return null;
      if (!data || error) throw new Error(errorMessage(error));
      return data;
    },
  });

  useEffect(() => {
    const expired = () => {
      setError("登入已過期，請重新登入；未儲存的內容不會自動入帳。");
      void clearSession(cache);
    };
    window.addEventListener("spm:session-expired", expired);
    return () => window.removeEventListener("spm:session-expired", expired);
  }, [cache]);

  useEffect(() => {
    if (owner.data === null) void clearSession(cache);
  }, [cache, owner.data]);

  async function logout() {
    setLoggingOut(true);
    setLogoutError(null);
    try {
      const headers = await mutationHeaders();
      const result = await api.POST("/api/v1/auth/logout", { params: { header: headers } });
      checked(result);
      await clearSession(cache);
    } catch (cause) {
      setLogoutError(failureMessage(cause));
    } finally {
      setLoggingOut(false);
    }
  }

  async function authenticate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const form = event.currentTarget;
    const values = new FormData(form);
    const password = String(values.get("password") ?? "");
    const initializing = setup.data?.initialized === false;
    if (initializing && password !== values.get("confirm")) {
      setError("兩次輸入的密碼不一致。");
      return;
    }
    setPending(true);
    setError(null);
    try {
      // Keep passwords out of query/mutation caches and persistent browser storage.
      const result = initializing
        ? await api.POST("/api/v1/setup/initialize", { body: {
          setup_token: String(values.get("setup_token") ?? ""), password,
        } })
        : await api.POST("/api/v1/auth/login", { body: { password } });
      const authenticated = checked(result, false);
      form.reset();
      await clearSession(cache);
      cache.setQueryData(["setup"], { initialized: true, requires_token: false });
      cache.setQueryData(["owner"], authenticated);
    } catch (cause) {
      setError(failureMessage(cause));
    } finally {
      setPending(false);
    }
  }

  if (owner.data) return children({ owner: owner.data, logout, loggingOut, logoutError });

  const initializing = setup.data?.initialized === false;
  const loading = setup.isPending || (setup.data?.initialized && owner.isPending);
  const loadError = setup.error ?? owner.error;
  return (
    <main className="mx-auto flex min-h-dvh max-w-5xl flex-col px-5 py-8 sm:px-10 sm:py-14">
      <header className="border-b border-border pb-6 text-xl font-semibold">持倉簿</header>
      <div className="my-auto grid gap-10 py-12 md:grid-cols-[1fr_1fr] md:gap-16">
        <section className="self-center">
          <h1 className="text-3xl leading-tight font-semibold sm:text-4xl">投資紀錄，<br />留在自己手上。</h1>
          <p className="mt-6 leading-8 text-secondary">這是你的私人帳簿。帳戶由你管理，任何未知或待核實的數字都會清楚標示。</p>
        </section>
        <section aria-labelledby="auth-title" className="rounded-xl border border-border bg-surface p-6 sm:p-8">
          {loading ? <p role="status">正在連接私人帳簿…</p> : loadError ? (
            <div role="alert">
              <h2 id="auth-title" className="text-lg font-semibold">無法連接帳簿</h2>
              <p className="my-4 text-sm leading-6 text-danger">{loadError.message}</p>
              <Button onClick={() => void (setup.isError ? setup.refetch() : owner.refetch())}>重新連接</Button>
            </div>
          ) : (
            <form onSubmit={event => void authenticate(event)} className="grid gap-5">
              <div>
                <h2 id="auth-title" className="text-xl font-semibold">{initializing ? "建立私人帳戶" : "登入持倉簿"}</h2>
                <p className="mt-2 text-sm leading-6 text-secondary">{initializing ? "先從本機 CLI 取得一次性 token，再設定你的密碼。" : "使用帳戶密碼開啟私人紀錄。"}</p>
              </div>
              {initializing && <label className="grid gap-2 text-sm font-medium">
                初始化 token
                <input name="setup_token" type="password" autoComplete="off" spellCheck={false} minLength={12} maxLength={200} required className="field" />
                <span className="font-normal text-secondary">執行 spm-manage setup-token；10 分鐘後過期。</span>
              </label>}
              <label className="grid gap-2 text-sm font-medium">
                {initializing ? "設定密碼" : "密碼"}
                <input name="password" type="password" autoComplete={initializing ? "new-password" : "current-password"} minLength={initializing ? 12 : 1} maxLength={200} required className="field" />
                {initializing && <span className="font-normal text-secondary">至少 12 個字元，請使用獨立且不常見的密碼或長密語。</span>}
              </label>
              {initializing && <label className="grid gap-2 text-sm font-medium">
                再次輸入密碼
                <input name="confirm" type="password" autoComplete="new-password" minLength={12} maxLength={200} required className="field" />
              </label>}
              {error && <p role="alert" className="text-sm leading-6 text-danger">{error}</p>}
              <Button type="submit" disabled={pending}>{pending ? "正在驗證…" : initializing ? "建立帳戶" : "登入"}</Button>
            </form>
          )}
        </section>
      </div>
      <footer className="border-t border-border pt-5 text-sm text-secondary">私人部署 · 不提供下單或投資建議</footer>
    </main>
  );
}
