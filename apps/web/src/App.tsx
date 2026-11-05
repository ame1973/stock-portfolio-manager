import { lazy, Suspense, useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, Navigate, NavLink, Outlet, Route, Routes, useLocation } from "react-router";
import { api } from "@spm/contracts";
import { Button } from "./components/ui/button";
import { AuthGate, type AuthSession } from "./features/auth/AuthGate";
import { AccountsPage, BrokersPage, PortfoliosPage, SecuritiesPage } from "./features/settings/MasterPages";
import { SecurityDetailPage } from "./features/settings/SecurityDetailPage";
import { SecuritySettingsPage } from "./features/settings/SecuritySettingsPage";

const TradesPage = lazy(() => import("./features/ledger/TradesPage").then(module => ({ default: module.TradesPage })));
const BookingPage = lazy(() => import("./features/ledger/BookingPage").then(module => ({ default: module.BookingPage })));
const AccountCashPage = lazy(() => import("./features/ledger/AccountCashPage").then(module => ({ default: module.AccountCashPage })));
const TransferPage = lazy(() => import("./features/ledger/TransferPage").then(module => ({ default: module.TransferPage })));

export function App() {
  return <AuthGate>{session => <SignedInApp session={session} />}</AuthGate>;
}

function SignedInApp({ session }: { session: AuthSession }) {
  const main = useRef<HTMLElement>(null);
  const location = useLocation();
  useEffect(() => { main.current?.focus(); }, [location.pathname]);
  return <div className="mx-auto flex min-h-dvh max-w-6xl flex-col px-4 py-6 sm:px-8 sm:py-10">
    <a href="#content" className="sr-only focus:not-sr-only focus:mb-4">跳至內容</a>
    <header className="flex flex-wrap items-center gap-4 border-b border-border pb-5">
      <Link to="/" className="flex items-center gap-3">
        <span aria-hidden="true" className="grid size-11 place-items-center rounded-lg bg-primary text-xl text-primary-foreground">簿</span>
        <div><p className="text-xl font-semibold">持倉簿</p><p className="text-xs text-secondary">Stock Portfolio Manager</p></div>
      </Link>
      <nav aria-label="主要導覽" className="order-3 flex w-full flex-wrap gap-2 sm:order-none sm:w-auto">
        <NavLink to="/" end className="inline-flex min-h-11 items-center rounded-md px-3 text-sm aria-[current=page]:bg-muted">服務連線</NavLink>
        <NavLink to="/transactions" className="inline-flex min-h-11 items-center rounded-md px-3 text-sm aria-[current=page]:bg-muted">交易紀錄</NavLink>
        <NavLink to="/cash" className="inline-flex min-h-11 items-center rounded-md px-3 text-sm aria-[current=page]:bg-muted">現金帳</NavLink>
        <NavLink to="/transfers" className="inline-flex min-h-11 items-center rounded-md px-3 text-sm aria-[current=page]:bg-muted">股票轉倉</NavLink>
        <NavLink to="/settings" className="inline-flex min-h-11 items-center rounded-md px-3 text-sm aria-[current=page]:bg-muted">管理設定</NavLink>
      </nav>
      <Button className="ml-auto" variant="outline" onClick={() => void session.logout()} disabled={session.loggingOut}>{session.loggingOut ? "正在登出…" : "登出"}</Button>
    </header>
    {session.logoutError && <p role="alert" className="mt-4 text-sm text-danger">{session.logoutError}</p>}
    <main id="content" ref={main} tabIndex={-1} className="min-w-0 flex-1 py-8 focus:outline-none sm:py-10">
      <Routes>
        <Route path="/" element={<ServiceStatus />} />
        <Route path="/transactions" element={<Suspense fallback={<p role="status">正在載入交易介面…</p>}><TradesPage /></Suspense>} />
        <Route path="/cash" element={<Suspense fallback={<p role="status">正在載入現金介面…</p>}><BookingPage key="cash" mode="cash" /></Suspense>} />
        <Route path="/cash/expenses" element={<Suspense fallback={<p role="status">正在載入費用介面…</p>}><BookingPage key="expense" mode="expense" /></Suspense>} />
        <Route path="/cash/transfers" element={<Suspense fallback={<p role="status">正在載入成對轉帳…</p>}><TransferPage key="cash" mode="cash" /></Suspense>} />
        <Route path="/transfers" element={<Suspense fallback={<p role="status">正在載入股票轉倉…</p>}><TransferPage key="security" mode="security" /></Suspense>} />
        <Route path="/cash/accounts/:id" element={<Suspense fallback={<p role="status">正在載入原幣餘額與期初…</p>}><AccountCashPage /></Suspense>} />
        <Route path="/settings" element={<SettingsLayout />}>
          <Route index element={<Navigate to="portfolios" replace />} />
          <Route path="portfolios" element={<PortfoliosPage />} />
          <Route path="brokers" element={<BrokersPage />} />
          <Route path="accounts" element={<AccountsPage />} />
          <Route path="securities" element={<SecuritiesPage />} />
          <Route path="securities/:id" element={<SecurityDetailPage />} />
          <Route path="security" element={<SecuritySettingsPage />} />
        </Route>
        <Route path="*" element={<div className="grid gap-5"><h1 className="text-2xl font-semibold">找不到頁面</h1><Link to="/settings" className="text-primary underline">前往管理設定</Link></div>} />
      </Routes>
    </main>
    <footer className="border-t border-border pt-5 text-sm text-secondary">私人部署 · 不提供下單或投資建議</footer>
  </div>;
}

function SettingsLayout() {
  const links = [["portfolios", "投資組合"], ["brokers", "券商"], ["accounts", "帳戶"], ["securities", "證券"], ["security", "帳戶安全"]];
  return <div className="grid gap-8">
    <nav aria-label="設定分類" className="flex flex-wrap gap-2 border-b border-border pb-4">
      {links.map(([path, label]) => <NavLink key={path} to={`/settings/${path}`} className="inline-flex min-h-11 items-center rounded-md border border-transparent px-4 text-sm font-medium aria-[current=page]:border-border aria-[current=page]:bg-surface aria-[current=page]:text-primary">{label}</NavLink>)}
    </nav>
    <Outlet />
  </div>;
}

function ServiceStatus() {
  const health = useQuery({
    queryKey: ["health", "ready"],
    queryFn: async ({ signal }) => {
      const { data, error } = await api.GET("/api/v1/health/ready", { signal });
      if (error || !data) throw new Error("服務未就緒，請檢查資料庫遷移與伺服器連線。");
      return data;
    },
  });
  return <div className="grid items-center gap-10 py-6 md:grid-cols-[1.2fr_1fr] md:gap-16 sm:py-10">
    <section>
      <h1 className="text-3xl leading-tight font-semibold sm:text-4xl">每一筆紀錄，<br />都有依據。</h1>
      <p className="mt-6 max-w-md leading-8 text-secondary">為港股與美股整理交易、成本和股息。資料留在自己的伺服器，未知的數字不會被當成零。</p>
      <Link to="/settings" className="mt-6 inline-flex min-h-11 items-center font-semibold text-primary underline underline-offset-4">管理投資組合與帳戶</Link>
    </section>
    <section aria-labelledby="status-title" className="panel self-start">
      <h2 id="status-title" className="text-lg font-semibold">服務連線</h2>
      {health.isPending ? <p role="status" className="my-5 text-secondary">正在檢查服務狀態…</p>
        : health.isError ? <div role="alert" className="my-5 text-danger"><p>暫時無法使用</p><p className="mt-2 text-sm leading-6">{health.error.message}</p></div>
          : <div role="status" className="my-5"><p className="font-medium text-success">已連線，資料庫已就緒</p><p className="mt-2 text-sm leading-6 text-secondary">目前為開發版本，已提供交易、現金／費用、轉倉、期初與主檔安全設定；企業行動介面及估值仍在開發。</p></div>}
      <Button variant="outline" onClick={() => void health.refetch()} disabled={health.isFetching}>重新檢查</Button>
    </section>
  </div>;
}
