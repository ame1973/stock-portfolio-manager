import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router";
import { Button } from "../../components/ui/button";
import { EditorSheet } from "../../components/ui/editor-sheet";
import { providerNames, refreshSources } from "../sources/data";
import { SourceReadState, SourceValues } from "../sources/details";
import { ProviderCredentialForm, ProviderPolicyForm } from "./ProviderForms";
import { RecordList } from "./RecordList";
import { PasswordForm } from "./SecuritySettingsPage";
import { usePageState } from "./data";
import { acceptsCredential, providerId, useProfileHistory, useProviderProfile, useProviderProfiles, type ProviderProfile } from "./provider-data";

type Editor = { kind: "policy"; record?: ProviderProfile } | { kind: "credential"; record: ProviderProfile };
function ProfileEditor({ editor, onSaved, onClose }: { editor: Editor; onSaved: (record: ProviderProfile) => void; onClose: () => void }) {
  return <EditorSheet title={editor.kind === "credential" ? "匯入私密金鑰" : editor.record ? "修訂來源政策" : "建立停用來源政策"} onClose={onClose} successFocus={editor.record ? "[data-profile-edit]" : "[data-editor-create]"}>
    <div className="mb-6"><PasswordForm /></div>
    {editor.kind === "credential" ? <ProviderCredentialForm record={editor.record} onSaved={onSaved} onCancel={onClose} /> : <ProviderPolicyForm record={editor.record} onSaved={onSaved} onCancel={onClose} />}
  </EditorSheet>;
}
function ProfileValues({ record }: { record: ProviderProfile }) {
  const policy = record.policy;
  return <div className="grid min-w-0 gap-4">
    <h3 className="font-semibold">{providerNames[policy.provider_id]}：版本 {record.version}</h3>
    <p className="text-sm leading-7">{policy.enabled ? "此回覆已啟用明確來源存取，不代表憑證有效或已擷取。" : "此回覆為停用決定；不會回退舊版本或安裝層金鑰。"}</p>
    <SourceValues values={[
      ["金鑰參考", record.credential_reference_present ? "已保存參考，未確認檔案可用或金鑰有效；不回傳原值" : "沒有保存參考；不代表已檢查安裝層檔案"],
      ["資料權利聲明", policy.terms_acknowledged ? "使用者已確認，未經獨立查核" : "未確認，不推定獲授權"],
      ["非秘密權利參考", policy.terms_reference || "未提供"], ["資料權利依據", policy.terms_reason ?? "未提供"],
      ["每分鐘／滾動 24 小時軟體上限", `${policy.requests_per_minute ?? "未指定"} / ${policy.requests_per_day ?? "未指定"}；不是供應商剩餘配額`],
      ["原始回應儲存", "停用，不授予再散布權"], ["此版本變更依據", record.reason],
      ["此版本建立時間（UTC）", record.created_at], ["實體修訂識別碼", record.id],
      ["回覆程式能力", record.runtime_selection_installed ? "已安裝個人政策選擇路徑，不是 worker 健康或來源成功" : "此原始回覆未宣告安裝，不會改寫舊回覆"],
    ]} />
  </div>;
}
const savedNotice = (record: ProviderProfile) => `已確認 ${providerNames[record.policy.provider_id]} 政策回覆版本 ${record.version}，該回覆為${record.policy.enabled ? "啟用" : "停用"}；不是來源驗證或擷取成功。目前決定以重新載入為準。`;

export function ProviderSettingsPage() {
  const cache = useQueryClient();
  const page = usePageState("settings:provider-profiles");
  const result = useProviderProfiles();
  const [editor, setEditor] = useState<Editor | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  return <div className="grid min-w-0 gap-5">
    <div className="flex flex-wrap items-start justify-between gap-4"><div className="max-w-3xl"><h1 className="text-2xl font-semibold">來源授權與金鑰</h1><p className="mt-2 text-sm leading-7 text-secondary">只列出這次登入明確建立的個人政策。先保存停用政策，需要金鑰的來源另行匯入，再核對權利與上限並明確啟用。此頁不探測上游、不建立工作或預設授權。</p></div><Button data-editor-create onClick={() => setEditor({ kind: "policy" })}>建立停用來源政策</Button></div>
    <Link className="inline-flex min-h-11 items-center text-sm text-primary underline" to="/data-center/providers">查看目前本機診斷與候選來源</Link>
    {notice && <p role="status" className="text-sm leading-7">{notice}</p>}
    {editor && <ProfileEditor editor={editor} onClose={() => setEditor(null)} onSaved={record => { setEditor(null); setNotice(savedNotice(record)); void refreshSources(cache); }} />}
    <RecordList title="個人來源政策" result={result} page={page} searchable={false} emptyMessage="尚無個人政策；僅在沒有個人決定時允許安裝層檔案回退。沒有紀錄不是來源停用或沒有股息；可到資料中心核對本機診斷。" order={{ description: "來源 ID 升序（最多八個明確決定）", direction: "ascending" }} columns={[
      { id: "provider", header: "來源", cell: ({ row }) => providerNames[row.original.policy.provider_id] },
      { id: "enabled", header: "保存的存取決定", cell: ({ row }) => row.original.policy.enabled ? "啟用，非健康保證" : "停用，無安裝層回退" },
      { id: "version", header: "觀察版本", cell: ({ row }) => String(row.original.version) },
      { id: "credential", header: "金鑰參考，非有效性", cell: ({ row }) => row.original.credential_reference_present ? "有參考，未確認可用" : "無參考" },
    ]}>{record => <article aria-label={record.id} className="grid min-w-0 gap-4"><ProfileValues record={record} /><Link className="inline-flex min-h-11 items-center text-sm text-primary underline" to={`/settings/providers/${record.policy.provider_id}`}>管理{providerNames[record.policy.provider_id]}政策、金鑰與歷史</Link></article>}</RecordList>
  </div>;
}
function ProfileHistory({ record }: { record: ProviderProfile }) {
  const page = usePageState(`settings:provider-history:${record.policy.provider_id}`);
  const result = useProfileHistory(record, page);
  return <RecordList title="來源政策歷史" result={result} page={page} emptyMessage="尚無可顯示的政策修訂；不會回填或推定舊金鑰。" order={{ description: "版本／ID 倒序", direction: "descending" }} columns={[
    { id: "version", header: "原始版本", cell: ({ row }) => String(row.original.version) },
    { id: "created", header: "原始建立時間（UTC）", cell: ({ row }) => row.original.created_at },
    { id: "enabled", header: "該次保存決定", cell: ({ row }) => row.original.policy.enabled ? "啟用" : "停用" },
    { id: "reason", header: "變更依據", cell: ({ row }) => row.original.reason },
  ]}>{revision => <article aria-label={revision.id}><ProfileValues record={revision} /></article>}</RecordList>;
}
export function ProviderSettingsDetailPage() {
  const { id = "" } = useParams();
  const provider = providerId(id);
  const cache = useQueryClient();
  const result = useProviderProfile(provider);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  return <div className="grid min-w-0 gap-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><h1 className="text-2xl font-semibold">{provider ? providerNames[provider] : "未知來源"}個人政策與歷史</h1><Link className="inline-flex min-h-11 items-center text-sm text-primary underline" to="/settings/providers">返回來源授權與金鑰</Link></div>
    <p className="max-w-3xl text-sm leading-7 text-secondary">最新決定包括停用、清除或不可用狀態，不恢復舊金鑰。匯入及輪替只保存停用版本；停用／清除不刪除檔案、不取消工作，也不能撤回已送出的請求。已完成證據與原始重試回覆保留不變。</p>
    {provider ? <SourceReadState result={result} label="個人來源政策" /> : <p role="alert">來源未在已登記目錄中；沒有猜測或送出讀寫要求。</p>}
    {notice && <p role="status" className="text-sm leading-7">{notice}</p>}
    {editor && <ProfileEditor editor={editor} onClose={() => setEditor(null)} onSaved={record => { setEditor(null); setNotice(savedNotice(record)); void refreshSources(cache); }} />}
    {result.data && <>
      <section className="panel grid min-w-0 gap-5" aria-label="目前保存的個人政策"><ProfileValues record={result.data} /><div className="flex flex-wrap gap-3"><Button data-profile-edit variant="outline" onClick={() => setEditor({ kind: "policy", record: structuredClone(result.data!) })}>修訂、啟用或停用政策</Button>
        {acceptsCredential(result.data.policy.provider_id) && <Button variant="outline" onClick={() => setEditor({ kind: "credential", record: structuredClone(result.data!) })}>匯入或輪替私密金鑰</Button>}
        <Button variant="outline" disabled={result.isFetching} onClick={() => void refreshSources(cache)}>重新載入目前政策與歷史</Button>
        <Link className="inline-flex min-h-11 items-center text-sm text-primary underline" to="/data-center/providers">核對目前本機診斷</Link></div></section>
      <ProfileHistory record={result.data} />
    </>}
  </div>;
}
