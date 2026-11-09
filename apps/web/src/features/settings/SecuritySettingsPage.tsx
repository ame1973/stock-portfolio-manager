import { useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@spm/contracts";
import { Field } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { ConfirmAction } from "../../components/ui/confirm-action";
import { ApiRequestError, checked, failureMessage, mutationHeaders } from "../../lib/api";
import { clearSession } from "../../lib/session";
import { usePageState } from "./data";
import { RecordList } from "./RecordList";

export function PasswordForm({ changing = false, disabled = false, onPendingChange }: {
  changing?: boolean; disabled?: boolean; onPendingChange?: (pending: boolean) => void;
}) {
  const cache = useQueryClient();
  const key = useRef(crypto.randomUUID());
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending || disabled) return;
    const form = event.currentTarget;
    const values = new FormData(form);
    const password = String(values.get("password") ?? "");
    const replacement = String(values.get("replacement") ?? "");
    setError(null); setNotice(null);
    if (changing && replacement !== values.get("confirm")) {
      setError("兩次輸入的新密碼不一致。");
      return;
    }
    setPending(true); onPendingChange?.(true);
    try {
      const header = await mutationHeaders(key.current);
      // Do not put passwords into TanStack mutation/query state or persistent browser storage.
      const result = changing
        ? await api.POST("/api/v1/auth/change-password", { params: { header }, body: { old_password: password, new_password: replacement } })
        : await api.POST("/api/v1/auth/reauth", { params: { header }, body: { password } });
      // A wrong password also returns 401; revalidate the session instead of assuming it expired.
      checked(result, false);
      form.reset(); key.current = crypto.randomUUID();
      if (changing) await clearSession(cache);
      else setNotice("已重新驗證；敏感操作確認有效 5 分鐘。");
    } catch (cause) {
      setError(failureMessage(cause));
      if (cause instanceof ApiRequestError && cause.code === "UNAUTHORIZED") void cache.invalidateQueries({ queryKey: ["owner"] });
    } finally { setPending(false); onPendingChange?.(false); }
  }
  return <form aria-label={changing ? "變更密碼" : "重新驗證"} aria-busy={pending} onSubmit={event => void submit(event)}
    onChange={() => { key.current = crypto.randomUUID(); setError(null); setNotice(null); }} className="panel grid gap-5">
    <h2 className="text-lg font-semibold">{changing ? "變更密碼" : "重新驗證"}</h2>
    <p className="text-sm leading-7 text-secondary">{changing ? "成功後會立即撤銷所有裝置的登入，包括目前裝置。你需要使用新密碼重新登入。" : "執行敏感操作前再次確認密碼。驗證不會替你執行其他操作。"}</p>
    <fieldset disabled={pending || disabled} className="grid gap-5">
      <Field label={changing ? "舊密碼" : "目前密碼"} name="password" type="password" required maxLength={200} autoComplete="current-password" />
      {changing && <>
        <Field label="新密碼" name="replacement" type="password" required minLength={12} maxLength={200} autoComplete="new-password" help="至少 12 個字元；使用獨立、不常見的密碼或長密語。" />
        <Field label="再次輸入新密碼" name="confirm" type="password" required minLength={12} maxLength={200} autoComplete="new-password" />
      </>}
    </fieldset>
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    {notice && <p role="status" className="text-sm text-success">{notice}</p>}
    <Button type="submit" disabled={pending || disabled}>{pending ? "正在驗證…" : changing ? "變更密碼並登出所有裝置" : "確認目前密碼"}</Button>
  </form>;
}

function Sessions() {
  const cache = useQueryClient();
  const page = usePageState();
  const result = useQuery({ queryKey: ["sessions", page.cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/auth/sessions", { params: { query: { cursor: page.cursor, limit: 25 } }, signal })) });
  return <section className="grid gap-4" aria-labelledby="sessions-heading">
    <h2 id="sessions-heading" className="text-xl font-semibold">登入裝置與工作階段</h2>
    <p className="text-sm leading-7 text-secondary">只列出時間與狀態，不公開 session token。撤銷目前工作階段會立即登出。</p>
    <RecordList title="工作階段" result={result} page={page} searchable={false}>{record => {
      const expired = Date.parse(record.expires_at) <= result.dataUpdatedAt || Date.parse(record.last_seen_at) + 7 * 86_400_000 <= result.dataUpdatedAt;
      return <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="min-w-0 text-sm leading-7">
          <h3 className="font-semibold">{record.current ? "目前工作階段（此裝置）" : "其他工作階段"}</h3>
          <p className="text-secondary">建立：{new Date(record.created_at).toLocaleString("zh-HK")}</p>
          <p className="text-secondary">上次活動：{new Date(record.last_seen_at).toLocaleString("zh-HK")}</p>
          <p>{record.revoked_at ? "已撤銷" : expired ? "載入時已到期" : "載入時尚未到期"}</p>
        </div>
        {!record.revoked_at && !expired && <ConfirmAction label={record.current ? "撤銷目前登入" : "撤銷登入"}
          title={record.current ? "撤銷目前登入並登出？" : "撤銷這個工作階段？"}
          description="伺服器會立即拒絕這個 session 的後續請求；已入帳的金融紀錄不受影響。"
          onConfirm={async key => {
            const header = await mutationHeaders(key);
            checked(await api.DELETE("/api/v1/auth/sessions/{id}", { params: { path: { id: record.id }, header } }));
            if (record.current) await clearSession(cache);
            else void cache.invalidateQueries({ queryKey: ["sessions"] });
          }} />}
      </div>;
    }}</RecordList>
  </section>;
}

export function SecuritySettingsPage() {
  return <div className="grid gap-8">
    <div><h1 className="text-2xl font-semibold">帳戶安全</h1><p className="mt-2 text-sm leading-7 text-secondary">管理私人帳簿的登入密碼與可撤銷工作階段。</p></div>
    <div className="grid items-start gap-5 lg:grid-cols-2"><PasswordForm /><PasswordForm changing /></div>
    <Sessions />
  </div>;
}
