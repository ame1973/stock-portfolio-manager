import { NavLink } from "react-router";

export function CashNav() {
  return <nav aria-label="現金帳分類" className="flex flex-wrap gap-2 border-b border-border pb-4">
    <NavLink to="/cash" end className="inline-flex min-h-11 items-center rounded-md px-4 text-sm aria-[current=page]:bg-muted">現金日誌</NavLink>
    <NavLink to="/cash/expenses" className="inline-flex min-h-11 items-center rounded-md px-4 text-sm aria-[current=page]:bg-muted">費用與退款</NavLink>
    <NavLink to="/settings/accounts" className="inline-flex min-h-11 items-center rounded-md px-4 text-sm">帳戶、餘額與期初</NavLink>
  </nav>;
}
