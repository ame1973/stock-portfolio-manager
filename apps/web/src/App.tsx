import { useQuery } from "@tanstack/react-query";
import { api } from "@spm/contracts";
import { Button } from "./components/ui/button";
import { AuthGate, type AuthSession } from "./features/auth/AuthGate";

export function App() {
  return <AuthGate>{session => <SignedInApp session={session} />}</AuthGate>;
}

function SignedInApp({ session }: { session: AuthSession }) {
  const health = useQuery({
    queryKey: ["health", "ready"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/health/ready");
      if (error || !data) throw new Error("服務未就緒，請檢查資料庫遷移與伺服器連線。");
      return data;
    },
  });

  return (
    <main className="mx-auto flex min-h-dvh max-w-5xl flex-col px-5 py-8 sm:px-10 sm:py-14">
      <header className="flex items-center gap-3 border-b border-border pb-6">
        <span aria-hidden="true" className="grid size-11 place-items-center rounded-lg bg-primary text-xl text-primary-foreground">簿</span>
        <div>
          <p className="text-xl font-semibold">持倉簿</p>
          <p className="text-sm text-secondary">Stock Portfolio Manager</p>
        </div>
        <Button className="ml-auto" variant="outline" onClick={() => void session.logout()} disabled={session.loggingOut}>
          {session.loggingOut ? "正在登出…" : "登出"}
        </Button>
      </header>
      {session.logoutError && <p role="alert" className="mt-4 text-sm text-danger">{session.logoutError}</p>}
      <section className="my-auto grid gap-10 py-14 md:grid-cols-[1.2fr_1fr] md:gap-16">
        <div>
          <h1 className="text-3xl leading-tight font-semibold sm:text-4xl">每一筆紀錄，<br />都有依據。</h1>
          <p className="mt-6 max-w-md leading-8 text-secondary">為港股與美股整理交易、成本和股息。資料留在自己的伺服器，未知的數字不會被當成零。</p>
        </div>
        <section aria-labelledby="status-title" className="self-start rounded-xl border border-border bg-surface p-6 sm:p-8">
          <h2 id="status-title" className="text-lg font-semibold">服務連線</h2>
          {health.isPending ? (
            <p role="status" className="my-5 text-secondary">正在檢查服務狀態…</p>
          ) : health.isError ? (
            <div role="alert" className="my-5 text-danger">
              <p>暫時無法使用</p>
              <p className="mt-2 text-sm leading-6">{health.error.message}</p>
            </div>
          ) : (
            <div role="status" className="my-5">
              <p className="font-medium text-success">已連線，資料庫已就緒</p>
              <p className="mt-2 text-sm leading-6 text-secondary">目前為開發版本，尚未開放投資紀錄功能。</p>
            </div>
          )}
          <Button variant="outline" onClick={() => void health.refetch()} disabled={health.isFetching}>
            重新檢查
          </Button>
        </section>
      </section>
      <footer className="border-t border-border pt-5 text-sm text-secondary">私人部署 · 不提供下單或投資建議</footer>
    </main>
  );
}
