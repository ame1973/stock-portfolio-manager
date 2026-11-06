import { useState, type ComponentProps } from "react";
import { Button } from "../../components/ui/button";
import { SecurityName } from "../ledger/ReferenceName";
import { ReferenceSelect } from "../settings/ReferenceSelect";
import { networkProviders, providerNames, type useProviders } from "./data";
import { SourceReadState } from "./details";

export function SourceSecurities({ label, ids, onChange, error }: { label: string; ids: string[]; onChange: (ids: string[]) => void; error?: string }) {
  const [candidate, setCandidate] = useState("");
  return <div className="grid min-w-0 content-start gap-3 sm:col-span-2">
    <ReferenceSelect kind="securities" label={label} activeOnly={false} value={candidate} registration={{ onChange: event => setCandidate(event.target.value) }} clearSelection={() => setCandidate("")} />
    <div><Button type="button" variant="outline" disabled={!candidate || ids.includes(candidate) || ids.length >= 100} onClick={() => { onChange([...ids, candidate]); setCandidate(""); }}>加入證券</Button></div>
    <p className="text-sm leading-7 text-secondary">已加入 {ids.length} / 100 個；可擷取下市證券的明確歷史身分。更改候選搜尋不會移除已加入證券。</p>
    {ids.length > 0 && <ul aria-label={`已加入${label}`} className="grid gap-3">{ids.map(id => <li key={id} className="flex min-w-0 flex-wrap items-center justify-between gap-3 rounded-md border border-border p-3"><span className="min-w-0 break-all text-sm"><SecurityName id={id} /></span><Button type="button" variant="outline" aria-label={`移除此${label} ${id}`} onClick={() => onChange(ids.filter(value => value !== id))}>移除此證券</Button></li>)}</ul>}
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
  </div>;
}
export function SourceProviders({ result, registration, error }: { result: ReturnType<typeof useProviders>; registration: ComponentProps<"input">; error?: string }) {
  return <fieldset className="grid min-w-0 gap-3 sm:col-span-2"><legend className="mb-3 text-sm font-medium">明確選擇來源</legend>
    <SourceReadState result={result} label="來源設定" />
    {result.data?.items.filter(row => networkProviders.some(id => id === row.provider_id)).map(row => <label key={row.provider_id} className="flex min-h-11 items-start gap-3 rounded-md border border-border p-3 text-sm leading-7"><input className="mt-2 shrink-0" type="checkbox" value={row.provider_id} {...registration} /><span>{providerNames[row.provider_id]}：{row.configured ? "本機已設定，未作即時上游探測" : "未設定／本機不可用，工作可能略過"}<span className="block text-secondary">{row.origin_group}；{row.capabilities.length > 0 ? row.capabilities.join(" / ") : "未宣告可用操作"}。選取不會啟用來源或匯入密鑰。</span></span></label>)}
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    <p className="text-sm leading-7 text-secondary">Yahoo 與 yfinance 共用同一上游，不是兩票；實際權限與配額由伺服器檢查。HKEX／SEC／手動引用需人工證據輸入，這個擷取表單不會捏造文件。</p>
  </fieldset>;
}
export function SourceOperations({ registration, error }: { registration: ComponentProps<"input">; error?: string }) {
  return <fieldset className="grid gap-3 sm:col-span-2"><legend className="mb-3 text-sm font-medium">來源操作</legend>
    <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" value="DIVIDENDS" {...registration} />擷取股息觀測</label>
    <label className="flex min-h-11 items-center gap-3 text-sm"><input type="checkbox" value="SPLITS" {...registration} />擷取拆股因子</label>
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    <p className="text-sm leading-7 text-secondary">拆股因子只保留 PROVIDER_ADJUSTMENT_ONLY，不會採納公司條款或重算股數；不支援的操作由工作明確略過。</p>
  </fieldset>;
}
