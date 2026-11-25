import { useEffect, useRef, useState } from "react";
import { onlineManager } from "@tanstack/react-query";
import { useForm, useWatch, type Control, type FieldErrors, type UseFormRegister } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { api, type components } from "@spm/contracts";
import { Field, SelectField } from "../../components/forms/fields";
import { Button } from "../../components/ui/button";
import { ApiRequestError, checked, failureMessage, mutationHeaders } from "../../lib/api";
import { date, fx, money, price } from "../ledger/inputs";
import { estimateRate, validEstimateEvent } from "./estimate-validation";
import { source } from "./projection-validation";
import { checkedRevisionPreview } from "./revision-preview-validation";
import { sameId } from "./receipt-validation";
import { usePrivatePage } from "./use-private-page";

type Schemas = components["schemas"];
type Event = Schemas["DividendEventRecord"];
type Account = Schemas["Account"];
type PreviewRequest = Schemas["PreviewRequest"];
type PreviewPage = Schemas["PreviewPage"];
type Item = Schemas["PreviewAccount"];
const choice = z.enum(["", "UNKNOWN", "PROVIDED"]);
const currencyChoice = z.enum(["", "UNKNOWN", "HKD", "USD"]);
const draft = z.object({ accountId: z.uuid(), taxMode: choice, taxRate: z.string(), feeMode: choice, fee: z.string(),
  feeCurrency: currencyChoice, feeRateMode: choice, feeRate: z.string(), feeSource: z.string(),
  hkdMode: choice, hkdRate: z.string(), hkdSource: z.string() });
const schema = z.object({ exDate: date, asOf: date, rule: z.enum(["", "ORDINARY_PRE_EX_DATE", "REQUIRES_REVIEW"]),
  dps: price.max(50), currency: z.enum(["HKD", "USD"]), basis: z.enum(["AS_DECLARED", "SPLIT_ADJUSTED", "UNKNOWN"]),
  accounts: z.array(draft) }).superRefine((value, context) => {
  if (!value.rule) context.addIssue({ code: "custom", path: ["rule"], message: "請明確選擇持股規則。" });
  const issue = (index: number, field: string, message: string) => context.addIssue({ code: "custom", path: ["accounts", index, field], message });
  for (const [index, item] of value.accounts.entries()) {
    if (!item.taxMode) issue(index, "taxMode", "請明確選擇稅率估算。")
    else if (item.taxMode === "PROVIDED" && !estimateRate.safeParse(item.taxRate).success) issue(index, "taxRate", "請輸入 0 至 1 的稅率小數。")
    if (!item.feeMode) issue(index, "feeMode", "請明確選擇費用估算。")
    else if (item.feeMode === "PROVIDED" && !money.max(50).safeParse(item.fee).success) issue(index, "fee", "請輸入有效費用，不會自動捨入。")
    if (!item.feeCurrency || (item.feeMode === "PROVIDED" && item.feeCurrency === "UNKNOWN")) issue(index, "feeCurrency", "請明確選擇費用幣別。")
    const foreignFee = item.feeCurrency !== "" && item.feeCurrency !== "UNKNOWN" && item.feeCurrency !== value.currency;
    for (const [needed, mode, amount, reference, modeField, amountField, sourceField] of [
      [value.currency !== "HKD", item.hkdMode, item.hkdRate, item.hkdSource, "hkdMode", "hkdRate", "hkdSource"],
      [foreignFee, item.feeRateMode, item.feeRate, item.feeSource, "feeRateMode", "feeRate", "feeSource"],
    ] as const) {
      if (!needed) continue;
      if (!mode) issue(index, modeField, "請明確選擇是否提供匯率。")
      if (mode === "PROVIDED" && !fx.max(50).safeParse(amount).success) issue(index, amountField, "請輸入有效匯率，不會自動捨入。")
      if (mode === "PROVIDED" && (!source.safeParse(reference).success || reference === "IDENTITY")) issue(index, sourceField, "請輸入明確匯率來源。")
    }
  }
});
type Values = z.infer<typeof schema>;
const accountShape = z.object({ id: z.uuid(), portfolio_id: z.uuid(), broker_id: z.uuid(), name: z.string().min(1),
  status: z.enum(["ACTIVE", "ARCHIVED"]) });
const labels: Record<Item["state"], string> = {
  EVENT_BASIS_REVIEW: "擬議金額口徑需核對，沒有可用估算。",
  LEDGER_AUDIT_REVIEW: "交易歷史稽核需核對，沒有可用估算。",
  ELIGIBILITY_REVIEW: "持股紀錄需要核對，沒有可用估算。",
  NO_RECORDED_POSITION: "沒有已記錄持倉，不代表已確認零持倉。",
  NO_RECORDED_POSITION_CASH_REVIEW: "沒有已記錄持倉，但有收款或缺少現金關聯，請核對。",
  ESTIMATES_UNAVAILABLE: "稅費估算未提供，不以零代替。",
  FEE_FX_UNAVAILABLE: "外幣費用缺少獨立換算率，沒有可用估算。",
  CASH_LINK_UNAVAILABLE: "收款或沖銷缺少現金關聯，不能比較。",
  NO_RECORDED_ACTIVE_RECEIPTS: "沒有已記錄的未作廢收款，不代表付款為零。",
  CURRENCY_REVIEW: "已記錄收款與擬議幣別不同，不能直接相減。",
  RECORDED_PARTIAL: "已記錄收款少於擬議淨額，仍須核對。",
  RECORDED_RECEIVED: "已記錄金額相等，不代表付款已確認。",
  RECORDED_OVER_RECEIVED: "已記錄收款超過擬議淨額，請核對。",
  RECORDED_NEEDS_REVIEW: "已記錄金額需要核對。",
};
const qualities: Record<NonNullable<Item["comparison_quality"]>, string> = {
  OK: "可計算，未核實付款或來源", MISSING_QUOTE: "缺少報價", MISSING_FX: "缺少港幣匯率", NOT_COMPUTABLE: "無法計算", NEEDS_REVIEW: "需要核對",
};
const reviewReasons: Record<Item["eligibility_reasons"][number], string> = {
  ELIGIBILITY_RULE_REQUIRES_REVIEW: "持股規則需要核對", UNALLOCATED_ACTION: "企業行動尚未分配", SPLIT_FRACTIONAL_RESIDUE: "拆股後有零碎股待核對",
};
const blank = (account: Account): Values["accounts"][number] => ({ accountId: account.id, taxMode: "", taxRate: "", feeMode: "", fee: "",
  feeCurrency: "", feeRateMode: "", feeRate: "", feeSource: "", hkdMode: "", hkdRate: "", hkdSource: "" });

async function allAccounts(_cursor: string | undefined, signal: AbortSignal): Promise<Account[]> {
  const accounts: Account[] = []; const ids = new Set<string>(); const cursors = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = checked(await api.GET("/api/v1/accounts", { params: { query: { limit: 100, cursor } }, signal }));
    if (!page || !Array.isArray(page.items) || page.items.length > 100 || (page.next_cursor != null
      && (typeof page.next_cursor !== "string" || !page.next_cursor || page.next_cursor.length > 1500 || page.items.length === 0))) throw new Error("帳戶清單無法安全核對；請重新載入。")
    for (const account of page.items) {
      if (!accountShape.safeParse(account).success || ids.has(account.id.toLowerCase())) throw new Error("帳戶清單無法安全核對；請重新載入。")
      ids.add(account.id.toLowerCase()); accounts.push(account);
    }
    cursor = page.next_cursor ?? undefined;
    if (cursor && cursors.has(cursor)) throw new Error("帳戶分頁無法安全核對；請重新載入。")
    if (cursor) cursors.add(cursor);
  } while (cursor);
  return accounts;
}

function EstimateFields({ index, account, control, register, errors, currency }: {
  index: number; account: Account; control: Control<Values>; register: UseFormRegister<Values>;
  errors: FieldErrors<Values>; currency: Values["currency"];
}) {
  const current = useWatch({ control, name: `accounts.${index}` });
  const error = errors.accounts?.[index];
  const foreignFee = current?.feeCurrency && current.feeCurrency !== "UNKNOWN" && current.feeCurrency !== currency;
  return <fieldset aria-label={`估算 ${account.name}`} className="grid min-w-0 gap-4 border-t border-border pt-4 sm:col-span-2 sm:grid-cols-2">
    <legend className="px-1 font-medium">{account.name} {account.status === "ARCHIVED" ? "（已封存）" : ""}</legend>
    <p className="break-all text-sm text-secondary sm:col-span-2">帳戶 {account.id}；每項估算由你明確提供或選擇未知。</p>
    <SelectField label="稅率估算" {...register(`accounts.${index}.taxMode`)} error={error?.taxMode?.message}>
      <option value="">請選擇</option><option value="UNKNOWN">未知</option><option value="PROVIDED">輸入估算</option></SelectField>
    {current?.taxMode === "PROVIDED" && <Field label="稅率小數" inputMode="decimal" {...register(`accounts.${index}.taxRate`)} error={error?.taxRate?.message} help="例如 0.1 表示 10%；不會猜測實際扣稅。" />}
    <SelectField label="費用估算" {...register(`accounts.${index}.feeMode`)} error={error?.feeMode?.message}>
      <option value="">請選擇</option><option value="UNKNOWN">未知</option><option value="PROVIDED">輸入估算</option></SelectField>
    {current?.feeMode === "PROVIDED" && <Field label="估算費用" inputMode="decimal" {...register(`accounts.${index}.fee`)} error={error?.fee?.message} />}
    <SelectField label="費用幣別" {...register(`accounts.${index}.feeCurrency`)} error={error?.feeCurrency?.message}>
      <option value="">請選擇</option><option value="UNKNOWN">未知</option><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>
    {foreignFee && <><SelectField label="費用換算率" {...register(`accounts.${index}.feeRateMode`)} error={error?.feeRateMode?.message}>
      <option value="">請選擇</option><option value="UNKNOWN">未知</option><option value="PROVIDED">輸入匯率及來源</option></SelectField>
      {current?.feeRateMode === "PROVIDED" && <><Field label="費用兌事件幣別匯率" inputMode="decimal" {...register(`accounts.${index}.feeRate`)} error={error?.feeRate?.message} />
        <Field label="費用匯率來源" {...register(`accounts.${index}.feeSource`)} error={error?.feeSource?.message} /></>}</>}
    {currency === "USD" ? <><SelectField label="港幣估值匯率" {...register(`accounts.${index}.hkdMode`)} error={error?.hkdMode?.message}>
      <option value="">請選擇</option><option value="UNKNOWN">未知</option><option value="PROVIDED">輸入匯率及來源</option></SelectField>
      {current?.hkdMode === "PROVIDED" && <><Field label="美元兌港幣匯率" inputMode="decimal" {...register(`accounts.${index}.hkdRate`)} error={error?.hkdRate?.message} />
        <Field label="港幣匯率來源" {...register(`accounts.${index}.hkdSource`)} error={error?.hkdSource?.message} /></>}</>
      : <p className="text-sm text-secondary">港幣兌港幣使用恆等匯率 1，不從外部推定。</p>}
  </fieldset>;
}

type ReadPage = { value: PreviewPage; request: PreviewRequest; token: string; cursors: (string | null)[]; readVersion: number };

function safeFailure(cause: unknown) {
  return cause instanceof ApiRequestError || cause instanceof TypeError ? failureMessage(cause) : "資料無法安全核對；請重新載入後再試。";
}

function sameAccounts(frozen: Account[], current: Account[]) {
  const byId = new Map(current.map(account => [account.id.toLowerCase(), account]));
  return frozen.length === current.length && frozen.every(account => {
    const match = byId.get(account.id.toLowerCase());
    return match && sameId(account.portfolio_id, match.portfolio_id) && sameId(account.broker_id, match.broker_id)
      && account.status === match.status && account.name === match.name;
  });
}

export function DividendRevisionPreviewPanel({ event: observed, onClose = () => {} }: { event: Event; onClose?: () => void }) {
  const [event] = useState(() => structuredClone(observed));
  const [readVersion, setReadVersion] = useState(0);
  const accountRead = usePrivatePage(`revision-preview:${event.id}:${event.current_revision_id}:${event.version}`, undefined, allAccounts);
  const accounts = accountRead.data;
  const available = !!accounts && !accountRead.isFetching && !accountRead.isPaused && !accountRead.isError;
  if (!validEstimateEvent(event)) return <p role="alert" className="text-sm text-danger">事件版本無法安全核對；請重新載入。</p>;
  return <section aria-label="擬議股息修訂預覽" className="grid min-w-0 gap-4">
    <p className="text-sm leading-7 text-secondary">固定事件版本 {event.version}／{event.current_revision_id}。這只是比較，不會接受修訂或改寫收款；不代表付款、持倉資格或收入已確認。</p>
    {accountRead.isPaused ? <p role="status" className="text-sm text-secondary">連線已暫停；重新連線後必須重新讀取帳戶。</p>
      : accountRead.isFetching ? <p role="status" className="text-sm text-secondary">正在讀取全部目前帳戶…</p> : null}
    {accountRead.isError && <p role="alert" className="text-sm text-danger">{safeFailure(accountRead.error)}</p>}
    <div className="flex flex-wrap gap-3"><Button type="button" variant="outline" disabled={accountRead.isFetching || accountRead.isPaused}
      onClick={() => { setReadVersion(value => value + 1); accountRead.refetch(); }}>重新讀取全部帳戶</Button>
      {!accounts && <Button type="button" variant="outline" onClick={onClose}>取消</Button>}</div>
    {accounts && <PreviewForm event={event} currentAccounts={accounts} available={available} readVersion={readVersion} onClose={onClose} />}
  </section>;
}

function PreviewForm({ event, currentAccounts, available, readVersion, onClose }: {
  event: Event; currentAccounts: Account[]; available: boolean; readVersion: number; onClose: () => void;
}) {
  const [accounts] = useState(() => structuredClone(currentAccounts));
  const matches = sameAccounts(accounts, currentAccounts);
  const ready = available && matches;
  const [preview, setPreview] = useState<ReadPage | null>(null); const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false); const active = useRef<AbortController | null>(null);
  const { register, control, handleSubmit, formState: { errors } } = useForm<Values>({ resolver: zodResolver(schema),
    defaultValues: { exDate: event.ex_date, asOf: "", rule: "", dps: event.dps, currency: event.currency, basis: event.adjustment_basis,
      accounts: accounts.map(blank) } });
  const currency = useWatch({ control, name: "currency" });
  useEffect(() => () => active.current?.abort(), []);
  useEffect(() => onlineManager.subscribe(online => {
    if (!online) { active.current?.abort(); active.current = null; setPending(false); setPreview(null); }
  }), []);
  const visiblePreview = ready && !pending && preview?.readVersion === readVersion ? preview : null;

  async function read(body: PreviewRequest, cursors: (string | null)[], expectedToken: string | null) {
    if (!ready || pending || active.current) return;
    const controller = new AbortController(); active.current = controller; setPending(true); setError(null);
    try {
      const header = { ...await mutationHeaders(), "If-Match": `"${event.version}"` };
      if (controller.signal.aborted) return;
      const value = checked(await api.POST("/api/v1/dividends/events/{event_id}/revision-preview", {
        params: { path: { event_id: event.id }, header }, body, signal: controller.signal,
      }));
      if (controller.signal.aborted) return;
      checkedRevisionPreview(value, event, accounts, body);
      if (expectedToken !== null && value.snapshot_token !== expectedToken) {
        setPreview(null); setError("預覽已變更；請重新計算，不會混合不同快照。"); return;
      }
      setPreview({ value, request: body, token: expectedToken ?? value.snapshot_token, cursors, readVersion });
    } catch (cause) {
      if (!controller.signal.aborted) { setPreview(null); setError(safeFailure(cause)); }
    } finally {
      if (!controller.signal.aborted) { active.current = null; setPending(false); }
    }
  }

  function submit(values: Values) {
    if (!ready || pending || values.accounts.length !== accounts.length
      || values.accounts.some((item, index) => !sameId(item.accountId, accounts[index]!.id))) {
      setError("帳戶範圍已變更；請重新載入並逐一核對。"); return;
    }
    const estimates: Schemas["AccountEstimate"][] = values.accounts.map(item => {
      const feeCurrency = item.feeCurrency === "UNKNOWN" || item.feeCurrency === "" ? null : item.feeCurrency;
      const foreignFee = feeCurrency !== null && feeCurrency !== values.currency;
      return { account_id: item.accountId, tax_rate: item.taxMode === "PROVIDED" ? item.taxRate : null,
        fee: item.feeMode === "PROVIDED" ? item.fee : null, fee_currency: feeCurrency,
        fee_rate_event: foreignFee && item.feeRateMode === "PROVIDED" ? item.feeRate : null,
        fee_rate_source: foreignFee && item.feeRateMode === "PROVIDED" ? item.feeSource : null,
        fx_rate_hkd: values.currency === "USD" && item.hkdMode === "PROVIDED" ? item.hkdRate : null,
        fx_source: values.currency === "USD" && item.hkdMode === "PROVIDED" ? item.hkdSource : null };
    });
    if (!values.rule) return;
    const body: PreviewRequest = { revision_id: event.current_revision_id, limit: 25, after_account_id: null, snapshot_token: null,
      proposal: { ex_date: values.exDate, as_of: values.asOf, eligibility_rule: values.rule, dps: values.dps,
        currency: values.currency, adjustment_basis: values.basis, accounts: estimates } };
    void read(structuredClone(body), [null], null);
  }

  function navigate(next: boolean) {
    if (!visiblePreview || pending) return;
    const cursors = next ? [...visiblePreview.cursors, visiblePreview.value.next_after_account_id] : visiblePreview.cursors.slice(0, -1);
    const after = cursors.at(-1) ?? null;
    if (next && after === null) return;
    const request = { ...visiblePreview.request, after_account_id: after, snapshot_token: after === null ? null : visiblePreview.token };
    void read(request, cursors, visiblePreview.token);
  }

  return <>
    {!matches && <p role="alert" className="text-sm text-danger">帳戶清單已變更；輸入仍保留。請先記下需要的內容，再關閉並重新開啟預覽以核對所有帳戶。</p>}
    <form aria-label="擬議修訂預覽" aria-busy={pending} className="panel grid gap-5"
      onChange={() => { setPreview(null); setError(null); }} onSubmit={event => { void handleSubmit(submit)(event); }}>
      <h3 className="text-lg font-semibold">擬議修訂預覽</h3>
      <fieldset disabled={pending} className="grid gap-5 sm:grid-cols-2">
      <Field label="擬議除淨日" type="date" {...register("exDate")} error={errors.exDate?.message} />
      <Field label="查閱日" type="date" {...register("asOf")} error={errors.asOf?.message} help="請明確輸入查閱日；未來除淨日僅顯示已記錄持倉。" />
      <SelectField label="持股規則" {...register("rule")} error={errors.rule?.message}>
        <option value="">請選擇</option><option value="ORDINARY_PRE_EX_DATE">一般除淨日前</option><option value="REQUIRES_REVIEW">需要另行核對</option></SelectField>
      <Field label="擬議每股股息" inputMode="decimal" {...register("dps")} error={errors.dps?.message} />
      <SelectField label="擬議幣別" {...register("currency")}><option value="HKD">HKD</option><option value="USD">USD</option></SelectField>
      <SelectField label="擬議金額口徑" {...register("basis")}><option value="AS_DECLARED">宣告條款</option><option value="SPLIT_ADJUSTED">拆股調整</option><option value="UNKNOWN">未知</option></SelectField>
      {accounts.length === 0 && <p className="text-sm text-secondary sm:col-span-2">目前沒有帳戶；這不是已確認零持倉。</p>}
      {accounts.map((account, index) => <EstimateFields key={account.id} index={index} account={account} control={control} register={register} errors={errors} currency={currency} />)}
      </fieldset>
      {error && <p role="alert" className="text-sm leading-6 text-danger">{error}</p>}
      <div className="flex flex-wrap gap-3"><Button type="submit" disabled={pending || !ready}>{pending ? "正在計算…" : "計算預覽"}</Button>
        <Button type="button" variant="outline" disabled={pending} onClick={onClose}>取消</Button></div>
    </form>
    {visiblePreview && <section aria-label="擬議修訂比較結果" className="grid min-w-0 gap-3 border-t border-border pt-4">
      <h4 className="font-semibold">擬議修訂比較結果</h4>
      <p className="text-sm leading-7 text-secondary">不代表付款、持倉資格或收入已確認。已記錄差額為擬議淨額減已記錄收款；歷史完整性未驗證。</p>
      {visiblePreview.value.items.length === 0 ? <p className="text-sm text-secondary">此頁沒有帳戶；不代表已確認零持倉。</p> : <ul className="grid gap-4">
        {visiblePreview.value.items.map(item => <li key={item.account_id} className="grid min-w-0 gap-2 border-b border-border pb-4 text-sm leading-7">
          <h5 className="font-medium">{accounts.find(value => sameId(value.id, item.account_id))?.name ?? item.account_id}</h5>
          <p>{labels[item.state]}</p><p>已記錄股數：{item.recorded_quantity ?? "未提供，不是零"}；有效收款 {item.active_receipts} 筆；已作廢 {item.void_receipts} 筆；來源未核對 {item.unverified_origins} 筆；缺少現金關聯 {item.unavailable_cash_links} 筆。</p>
          {item.eligibility_state === "UPCOMING_RECORDED_HOLDINGS" && <p>擬議除淨日尚未到達；股數只是查閱日的已記錄持倉，不是已產生收入。</p>}
          {item.eligibility_reasons.length > 0 && <p>持股紀錄待核對：{item.eligibility_reasons.map(reason => reviewReasons[reason]).join("、")}。</p>}
          {item.pending_action_ids.length > 0 && <p className="break-all">待核對行動：{item.pending_action_ids.join("、")}。</p>}
          {item.previous_projection_id && <p className="break-all">舊投影 {item.previous_projection_id}；事件修訂 {item.previous_revision_id}；截至 {item.previous_as_of}；金額 {item.previous_expected === null ? "未提供" : `${item.previous_currency} ${item.previous_expected}`}。</p>}
          {item.proposed_amounts && <><p>擬議金額 {item.proposed_amounts.currency}：毛額 {item.proposed_amounts.gross_local}；稅額 {item.proposed_amounts.tax_local}；費用 {item.proposed_amounts.fee_local}；淨額 {item.proposed_amounts.net_local}；捨入差額 {item.proposed_amounts.rounding_delta_local}。</p>
            <p>港幣估值 {item.proposed_amounts.estimated_hkd ?? "未知"}；投影品質：{qualities[item.proposed_amounts.quality]}。</p>
            <p>擬議減舊投影 {item.expected_delta ?? "無法比較"}。</p>
            <p>已記錄收款 {item.recorded_received ?? "未提供，不是零"}；已記錄差額 {item.recorded_outstanding ?? "未提供，不是零"}；比較品質：{item.comparison_quality ? qualities[item.comparison_quality] : "未提供"}。</p></>}
        </li>)}
      </ul>}
      <div className="flex flex-wrap items-center gap-3"><Button type="button" variant="outline" disabled={pending || !ready || visiblePreview.cursors.length === 1} onClick={() => navigate(false)}>上一頁預覽</Button>
        <p className="text-sm text-secondary">第 {visiblePreview.cursors.length} 頁；範圍變更時必須重新計算。</p>
        <Button type="button" variant="outline" disabled={pending || !ready || visiblePreview.value.next_after_account_id === null} onClick={() => navigate(true)}>下一頁預覽</Button></div>
    </section>}
  </>;
}
