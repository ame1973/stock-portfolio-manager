import { useState, type ComponentType, type ReactNode } from "react";
import { useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Link } from "react-router";
import { api } from "@spm/contracts";
import { Button } from "../../components/ui/button";
import { ConfirmAction } from "../../components/ui/confirm-action";
import { checked, mutationHeaders } from "../../lib/api";
import { AccountForm, BrokerForm, PortfolioForm, SecurityForm } from "./forms";
import { RecordList } from "./RecordList";
import { useAccounts, useBrokers, usePageState, usePortfolios, useSecurities, type Account, type Broker } from "./data";

const cashLabels = { UNKNOWN: "現金帳完整性未知", PARTIAL: "只有部分現金紀錄", COMPLETE: "現金帳已完整對帳" };

function ManageRecords<T extends { id: string; name: string }>({ kind, title, description, result, page, Form, children }: {
  kind: string; title: string; description: string; result: UseQueryResult<{ items: T[]; next_cursor?: string | null }>;
  page: ReturnType<typeof usePageState>;
  Form: ComponentType<{ record?: T; onSaved: (record: T) => void; onCancel: () => void }>;
  children: (record: T) => ReactNode;
}) {
  const cache = useQueryClient();
  const [editor, setEditor] = useState<T | "new" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  function saved(record: T) {
    setNotice(`已儲存「${record.name}」。`);
    setEditor(null);
    page.reset();
    void cache.invalidateQueries({ queryKey: [kind] });
  }
  return <div className="grid gap-6">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold">{title}</h1>
        <p className="mt-2 text-sm leading-7 text-secondary">{description}</p>
      </div>
      <Button onClick={() => { setNotice(null); setEditor("new"); }} disabled={editor !== null}>新增{title}</Button>
    </div>
    {notice && <p role="status" className="text-sm text-success">{notice}</p>}
    {editor !== null && <Form key={editor === "new" ? "new" : editor.id} record={editor === "new" ? undefined : editor}
      onSaved={saved} onCancel={() => { setEditor(null); void result.refetch(); }} />}
    <RecordList title={title} result={result} page={page}>{record => <div className="flex flex-wrap items-center justify-between gap-4">
      <div className="min-w-0 flex-1 break-words">
        <h2 className="font-semibold">{record.name}</h2>
        <div className="mt-2 text-sm leading-7 text-secondary">{children(record)}</div>
      </div>
      <Button variant="outline" disabled={editor !== null} onClick={() => { setNotice(null); setEditor(record); }}>編輯<span className="sr-only">{record.name}</span></Button>
    </div>}</RecordList>
  </div>;
}

function ArchiveControl({ kind, record }: { kind: "brokers" | "accounts"; record: Broker | Account }) {
  const cache = useQueryClient();
  const archived = record.status === "ARCHIVED";
  return <div className="mt-3 flex flex-wrap items-center gap-3">
    <span>{archived ? "已封存；歷史紀錄仍保留" : "使用中"}</span>
    <ConfirmAction key={`${record.id}-${record.version}`} label={archived ? "重新啟用" : "封存"}
      title={`${archived ? "重新啟用" : "封存"}「${record.name}」？`}
      description={archived ? "這只會恢復主檔狀態；不會新增或改寫任何金融紀錄。" : "封存不是刪除。帳戶、交易、現金與股息歷史及關聯都會保留；之後可重新啟用。"}
      onConfirm={async key => {
        const header = { ...await mutationHeaders(key), "If-Match": `"${record.version}"` };
        if (kind === "brokers") {
          if (archived) checked(await api.PATCH("/api/v1/brokers/{id}", { params: { path: { id: record.id }, header }, body: { status: "ACTIVE" } }));
          else checked(await api.DELETE("/api/v1/brokers/{id}", { params: { path: { id: record.id }, header } }));
        } else {
          if (archived) checked(await api.PATCH("/api/v1/accounts/{id}", { params: { path: { id: record.id }, header }, body: { status: "ACTIVE" } }));
          else checked(await api.DELETE("/api/v1/accounts/{id}", { params: { path: { id: record.id }, header } }));
        }
        void cache.invalidateQueries({ queryKey: [kind] });
      }} />
  </div>;
}

export function PortfoliosPage() {
  const page = usePageState();
  const result = usePortfolios(page.q, page.cursor);
  return <ManageRecords kind="portfolios" title="投資組合" description="依用途分開管理帳戶；所有組合以 HKD 報告。現金帳完整性必須由你核對後明確設定。"
    result={result} page={page} Form={PortfolioForm}>{record => <p>HKD 報告 · {cashLabels[record.cash_completeness]}</p>}</ManageRecords>;
}

export function BrokersPage() {
  const page = usePageState();
  const result = useBrokers(page.q, page.cursor);
  return <ManageRecords kind="brokers" title="券商" description="設定券商名稱與未來股息估計費用。封存不會刪除帳戶或歷史紀錄。"
    result={result} page={page} Form={BrokerForm}>{record => <>
      <p>股息估計代收費 {record.dividend_fee_currency} {record.dividend_fee}</p>
      <ArchiveControl kind="brokers" record={record} />
    </>}</ManageRecords>;
}

export function AccountsPage() {
  const page = usePageState();
  const result = useAccounts(page.q, page.cursor);
  return <ManageRecords kind="accounts" title="帳戶" description="每個帳戶屬於一個投資組合與券商。先建立這兩項主檔，再設定帳戶幣別與可選的起始餘額。"
    result={result} page={page} Form={AccountForm}>{record => <>
      <p>{record.default_currency} · {cashLabels[record.cash_completeness]}</p>
      <p>起始餘額：{record.opening_cash === null ? "未知（不是零）" : `${record.default_currency} ${record.opening_cash}，${record.opening_cash_date}`}</p>
      <ArchiveControl kind="accounts" record={record} />
    </>}</ManageRecords>;
}

export function SecuritiesPage() {
  const page = usePageState();
  const result = useSecurities(page.q, page.cursor);
  return <ManageRecords kind="securities" title="證券" description="證券以交易所與穩定身分識別；同代碼可在不同交易所並存。手動新增不代表已核實或有供應商資料覆蓋。"
    result={result} page={page} Form={SecurityForm}>{record => <>
      <p className="font-medium text-foreground">{record.display_ticker} / {record.exchange_code} / {record.quote_currency}</p>
      <p>{record.verified ? "已人工核實身分" : "未核實"}；{({ ACTIVE: "有效", SUSPENDED: "暫停交易", DELISTED: "已退市", UNKNOWN: "狀態未知" })[record.status]}</p>
      <Link className="inline-flex min-h-11 items-center font-medium text-primary underline underline-offset-4" to={`/settings/securities/${record.id}`}>識別碼與供應商代碼<span className="sr-only">：{record.name}</span></Link>
    </>}</ManageRecords>;
}
