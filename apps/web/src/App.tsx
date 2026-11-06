import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, Navigate, NavLink, Outlet, Route, Routes, useLocation } from "react-router";
import { api } from "@spm/contracts";
import { Button } from "./components/ui/button";
import { AuthGate, type AuthSession } from "./features/auth/AuthGate";
import { ViewContext, type Views } from "./lib/view-state";

const PortfoliosPage = lazy(() => import("./features/settings/MasterPages").then(module => ({ default: module.PortfoliosPage })));
const BrokersPage = lazy(() => import("./features/settings/MasterPages").then(module => ({ default: module.BrokersPage })));
const AccountsPage = lazy(() => import("./features/settings/MasterPages").then(module => ({ default: module.AccountsPage })));
const SecuritiesPage = lazy(() => import("./features/settings/MasterPages").then(module => ({ default: module.SecuritiesPage })));
const SecurityDetailPage = lazy(() => import("./features/settings/SecurityDetailPage").then(module => ({ default: module.SecurityDetailPage })));
const SecuritySettingsPage = lazy(() => import("./features/settings/SecuritySettingsPage").then(module => ({ default: module.SecuritySettingsPage })));
const TradesPage = lazy(() => import("./features/ledger/TradesPage").then(module => ({ default: module.TradesPage })));
const BookingPage = lazy(() => import("./features/ledger/BookingPage").then(module => ({ default: module.BookingPage })));
const AccountCashPage = lazy(() => import("./features/ledger/AccountCashPage").then(module => ({ default: module.AccountCashPage })));
const TransferPage = lazy(() => import("./features/ledger/TransferPage").then(module => ({ default: module.TransferPage })));
const ActionPage = lazy(() => import("./features/ledger/ActionPage").then(module => ({ default: module.ActionPage })));
const DataCenterLayout = lazy(() => import("./features/sources/SourcePages").then(module => ({ default: module.DataCenterLayout })));
const ProvidersPage = lazy(() => import("./features/sources/SourcePages").then(module => ({ default: module.ProvidersPage })));
const CoveragePage = lazy(() => import("./features/sources/SourcePages").then(module => ({ default: module.CoveragePage })));
const ObservationsPage = lazy(() => import("./features/sources/SourcePages").then(module => ({ default: module.ObservationsPage })));
const JobsPage = lazy(() => import("./features/sources/JobsPage").then(module => ({ default: module.JobsPage })));
const JobDetailPage = lazy(() => import("./features/sources/JobsPage").then(module => ({ default: module.JobDetailPage })));
const SchedulesPage = lazy(() => import("./features/sources/SchedulesPage").then(module => ({ default: module.SchedulesPage })));
const ScheduleDetailPage = lazy(() => import("./features/sources/SchedulesPage").then(module => ({ default: module.ScheduleDetailPage })));

const navigation = [
  { to: "/", label: "服務連線", icon: "M3 11 12 3l9 8M5 10v11h5v-7h4v7h5V10" },
  { to: "/transactions", label: "交易紀錄", icon: "M4 5h16M4 12h10M4 19h16m-3-10 3 3-3 3" },
  { to: "/cash", label: "現金帳", icon: "M3 5h18v15H3V5Zm12 5h6v5h-6v-5ZM7 5V3h11v2" },
  { to: "/transfers", label: "股票轉倉", icon: "M3 7h17m-4-4 4 4-4 4M21 17H4m4-4-4 4 4 4" },
  { to: "/corporate-actions", label: "企業行動", icon: "M12 3v7m0 0H5v8m7-8h7v8M2 18h6v4H2v-4Zm14 0h6v4h-6v-4Z" },
  { to: "/data-center", label: "資料中心", icon: "M4 3h16v18H4V3Zm4 5h8M8 12h8M8 16h5" },
  { to: "/settings", label: "管理設定", icon: "M4 6h16M4 12h16M4 18h16M8 3v6m8 0v6M10 15v6" },
];

function Brand({ compact = false }: { compact?: boolean }) {
  return <Link to="/" aria-label="持倉簿服務連線" className="flex min-h-11 items-center gap-3 rounded-md">
    <span aria-hidden="true" className="grid size-11 shrink-0 place-items-center rounded-lg bg-primary text-xl text-primary-foreground">簿</span>
    {!compact && <div><p className="text-xl font-semibold">持倉簿</p><p className="text-xs text-secondary">Stock Portfolio Manager</p></div>}
  </Link>;
}

function NavIcon({ path }: { path: string }) {
  return <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" className="size-5 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d={path} /></svg>;
}

export function App() {
  return <AuthGate>{session => <SignedInApp key={session.owner.id} session={session} />}</AuthGate>;
}

function SignedInApp({ session }: { session: AuthSession }) {
  const main = useRef<HTMLElement>(null);
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(false);
  const [views, setViews] = useState<Views>({});
  useEffect(() => { main.current?.focus(); }, [location.pathname]);
  return <ViewContext value={{ views, setViews }}><div className="workspace-frame min-h-dvh lg:flex">
    <a href="#content" className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50 focus:rounded-md focus:bg-surface focus:p-3">跳至內容</a>
    <aside className={`hidden shrink-0 border-r border-border bg-surface lg:block ${collapsed ? "w-24" : "w-60"}`}>
      <div className="sticky top-0 flex min-h-dvh flex-col gap-6 p-4">
        <Brand compact={collapsed} />
        <Button variant="outline" aria-expanded={!collapsed} aria-controls="workspace-navigation" onClick={() => setCollapsed(value => !value)} aria-label={collapsed ? "展開導覽" : "收合導覽"}>{collapsed ? "展開" : "收合導覽"}</Button>
        <nav id="workspace-navigation" aria-label="主要導覽" className="grid gap-2">
          {navigation.map(item => <NavLink key={item.to} to={item.to} end={item.to === "/"} aria-label={item.label} title={collapsed ? item.label : undefined} className={`flex min-h-11 items-center gap-3 rounded-md px-3 py-3 text-sm aria-[current=page]:bg-muted aria-[current=page]:font-semibold aria-[current=page]:text-primary ${collapsed ? "justify-center" : ""}`}>
            <NavIcon path={item.icon} /><span className={collapsed ? "sr-only" : ""}>{item.label}</span>
          </NavLink>)}
        </nav>
        {!collapsed && <p className="mt-auto max-w-44 text-xs leading-6 text-secondary">原始事實保留，未知數字不補零。所有寫入以伺服器確認為準。</p>}
      </div>
    </aside>
    <div className="min-w-0 flex-1">
      <header className="flex min-h-24 flex-wrap items-center gap-4 border-b border-border bg-surface px-4 py-4 sm:px-8">
        <div className="lg:hidden"><Brand /></div><p className="hidden text-sm font-medium text-secondary lg:block">財務工作區</p>
        <Button className="ml-auto" variant="outline" onClick={() => void session.logout()} disabled={session.loggingOut}>{session.loggingOut ? "正在登出…" : "登出"}</Button>
      </header>
      <div className="mx-auto flex min-h-[calc(100dvh-6rem)] max-w-[90rem] flex-col px-4 sm:px-8">
        {session.logoutError && <p role="alert" className="mt-4 text-sm text-danger">{session.logoutError}</p>}
        <main id="content" ref={main} tabIndex={-1} className="min-w-0 flex-1 py-8 focus:outline-none sm:py-10">
          <Suspense fallback={<p role="status">正在載入工作區…</p>}><Routes>
            <Route path="/" element={<ServiceStatus />} />
            <Route path="/transactions" element={<TradesPage />} />
            <Route path="/cash" element={<BookingPage key="cash" mode="cash" />} />
            <Route path="/cash/expenses" element={<BookingPage key="expense" mode="expense" />} />
            <Route path="/cash/transfers" element={<TransferPage key="cash" mode="cash" />} />
            <Route path="/transfers" element={<TransferPage key="security" mode="security" />} />
            <Route path="/cash/accounts/:id" element={<AccountCashPage key={location.pathname} />} />
            <Route path="/corporate-actions" element={<ActionPage />} />
            <Route path="/data-center" element={<DataCenterLayout />}>
              <Route index element={<Navigate to="providers" replace />} />
              <Route path="providers" element={<ProvidersPage />} />
              <Route path="coverage" element={<CoveragePage />} />
              <Route path="dividends" element={<ObservationsPage key="dividends" mode="dividends" />} />
              <Route path="splits" element={<ObservationsPage key="splits" mode="splits" />} />
              <Route path="jobs" element={<JobsPage />} />
              <Route path="jobs/:id" element={<JobDetailPage key={location.pathname} />} />
              <Route path="schedules" element={<SchedulesPage />} />
              <Route path="schedules/:id" element={<ScheduleDetailPage key={location.pathname} />} />
            </Route>
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
          </Routes></Suspense>
        </main>
        <footer className="border-t border-border py-5 text-sm text-secondary">私人部署 · 不提供下單或投資建議</footer>
      </div>
    </div>
    <nav aria-label="手機主要導覽" className="mobile-workspace-nav fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t border-border bg-surface lg:hidden">
      {navigation.filter(item => item.to !== "/" && item.to !== "/data-center").map(item => <NavLink key={item.to} to={item.to} aria-label={item.label} className="flex min-h-16 min-w-0 flex-col items-center justify-center gap-1 px-1 py-2 text-xs aria-[current=page]:bg-muted aria-[current=page]:font-semibold aria-[current=page]:text-primary"><NavIcon path={item.icon} /><span>{item.label}</span></NavLink>)}
    </nav>
  </div></ViewContext>;
}

function SettingsLayout() {
  const links = [["portfolios", "投資組合"], ["brokers", "券商"], ["accounts", "帳戶"], ["securities", "證券"], ["security", "帳戶安全"]];
  return <div className="grid gap-8">
    <nav aria-label="設定分類" className="flex flex-wrap gap-2 border-b border-border pb-4">
      {links.map(([path, label]) => <NavLink key={path} to={`/settings/${path}`} className="inline-flex min-h-11 items-center rounded-md border border-transparent px-4 text-sm font-medium aria-[current=page]:border-border aria-[current=page]:bg-surface aria-[current=page]:text-primary">{label}</NavLink>)}
      <NavLink to="/data-center" className="inline-flex min-h-11 items-center rounded-md border border-transparent px-4 text-sm font-medium text-primary underline">資料中心與來源擷取</NavLink>
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
          : <div role="status" className="my-5"><p className="font-medium text-success">已連線，資料庫已就緒</p><p className="mt-2 text-sm leading-6 text-secondary">目前為開發版本，已提供交易、現金／費用、轉倉、企業行動、期初設定及來源擷取／觀測診斷；股息採納／實收、估值與完整離線／備份功能仍在開發。</p></div>}
      <Button variant="outline" onClick={() => void health.refetch()} disabled={health.isFetching}>重新檢查</Button>
    </section>
  </div>;
}
