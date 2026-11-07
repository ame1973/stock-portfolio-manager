import { useQuery, type QueryClient } from "@tanstack/react-query";
import { api, type components } from "@spm/contracts";
import { checked } from "../../lib/api";

type Schemas = components["schemas"];
export type Provider = Schemas["ProviderInfo"];
export type ProviderId = Provider["provider_id"];
export type Job = Schemas["SourceJob"];
export type JobItem = Schemas["SourceJobItem"];
export type CreatedJob = Schemas["JobCreated"];
export type Schedule = Schemas["SourceScheduleRecord"];
export type SchedulePolicy = Schemas["SourceSchedulePolicy"];
export type ScheduleOccurrence = Schemas["SourceScheduleOccurrence"];
export const cadenceLabels = { DAILY: "每日（包括週末）", WEEKDAYS: "星期一至五（不是交易所日曆）", WEEKLY: "每週指定星期" } as const;
export const weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"];
export function scheduleTiming(policy: SchedulePolicy) {
  return `${cadenceLabels[policy.cadence]}${policy.cadence === "WEEKLY" ? `／${weekdays[policy.weekday ?? -1] ?? "星期未確認"}` : ""} ${policy.local_run_time}`;
}
export type Operation = JobItem["operation"];
export type Observation = Schemas["DividendObservationRecord"] | Schemas["SplitObservationRecord"];
export type Coverage = Schemas["ProviderCoverage"] & { id: string };
export type ObservationKind = "dividends" | "splits";
export const citationTitle = (mode: ObservationKind) => mode === "dividends" ? "引用股息來源條款" : "引用拆股來源因子";
export const isDividend = (row: Observation): row is Schemas["DividendObservationRecord"] => "dps" in row;
// Preserve wire strings: source amounts and rational factors never pass through Number or math.
export function observationTerms(record: Observation) {
  return isDividend(record) ? `${record.dps ?? "未知，不是零"} ${record.currency ?? "幣別未知"}` : `${record.ratio_numerator} : ${record.ratio_denominator}`;
}
export const networkProviders = ["eodhd", "alphavantage", "massive", "yahoo", "yfinance"] as const;
export const providerNames: Record<ProviderId, string> = { eodhd: "EODHD", alphavantage: "Alpha Vantage", massive: "Massive", yahoo: "Yahoo", yfinance: "yfinance（Yahoo 包裝）", hkex_evidence: "HKEX 人工引用", sec_evidence: "SEC 人工引用", manual: "手動證據" };
export const jobLabels: Record<Job["status"], string> = { QUEUED: "已排隊，未完成擷取", LEASED: "已取得執行權", RUNNING: "正在擷取來源", RETRYING: "等待重試／配額／熔斷期限", PARTIAL: "部分來源已保留，未完整完成", SUCCEEDED: "來源處理完成，不代表股息已驗證", FAILED: "未能完成來源擷取", CANCEL_REQUESTED: "已提出取消，尚未確認停止", CANCELLED: "已停止未完成工作，保留既有來源" };
export const itemLabels: Record<JobItem["status"], string> = { PENDING: "待處理", RUNNING: "處理中", RETRYING: "等待期限／重試", SUCCEEDED: "來源處理完成", PARTIAL: "保留部分來源", FAILED: "來源處理失敗", SKIPPED: "已略過，未宣稱擷取成功", CANCELLED: "已取消未完成工作" };
export const fetchLabels: Record<Schemas["FetchState"] | "NOT_TESTED", string> = { OK_WITH_DATA: "已記錄有效來源列，未採納", NO_DATA: "來源該次未回傳資料，不代表沒有股息", EMPTY_CONFIRMED: "保留的空資料狀態，並非確認無股息", UNSUPPORTED: "來源不支援此次要求", UNCONFIGURED: "未設定或本機依賴不可用", RATE_LIMITED: "該次來源要求受限流", AUTH_ERROR: "該次來源授權遭拒，非登入狀態", TRANSIENT_ERROR: "該次來源暫時失敗", PARSE_ERROR: "該次來源未能完整解析", STALE: "保留的未探測／過時狀態", NOT_TESTED: "尚無可用的已記錄頁面" };
export const workLabels: Record<Schemas["WorkError"], string> = { UNCONFIGURED: "未設定／未就緒；沒有自動啟用來源", UNSUPPORTED: "不支援市場、操作或尚缺人工引用輸入", SYMBOL_NOT_FOUND: "缺少涵蓋整段日期的明確代號", SYMBOL_AMBIGUOUS: "代號期間或穩定身分有歧義", ALIAS_CHANGED: "代號已變更；不會把回應套用到新身分", CONFIG_CHANGED: "設定已變更；請建立明確新工作", PROVIDER_FAILED: "來源未能完成要求", PARSE_ERROR: "來源格式未能安全解析", RATE_LIMITED: "來源限流，保留要求的等候期限", TRANSIENT_ERROR: "來源暫時失敗，依工作規則重試", QUOTA_WAIT: "等待共用上游軟體配額", CIRCUIT_OPEN: "來源熔斷中，等待可用期限", LEASE_EXPIRED: "執行權已到期，等待安全復原", RETRIES_EXHAUSTED: "此工作項目已耗盡四輪嘗試", WORKER_ERROR: "worker 未能完成，未宣稱成功", PAGE_LIMIT: "已到安全分頁上限，保留有效前綴", CURSOR_CYCLE: "來源分頁循環，保留有效前綴" };
export const operationLabels: Record<Operation, string> = { DIVIDENDS: "股息來源觀測", SPLITS: "provider-only 拆股因子" };
export const incompleteLabels: Record<string, string> = { MISSING_EX_DATE: "除淨日未知", MISSING_DPS: "每股金額未知", MISSING_CURRENCY: "股息幣別未知", UNRESOLVED_ADJUSTMENT: "調整基準待核對" };

export function pollJob(status: Job["status"] | undefined): number | false {
  return status && ["QUEUED", "LEASED", "RUNNING", "CANCEL_REQUESTED"].includes(status) ? 2000 : false;
}
export function canCancel(status: Job["status"]) {
  return ["QUEUED", "LEASED", "RUNNING", "RETRYING"].includes(status);
}
export function refreshSources(cache: QueryClient) {
  return cache.invalidateQueries({ queryKey: ["sources"] });
}
export function useProviders() {
  return useQuery({ queryKey: ["sources", "providers"], queryFn: async ({ signal }) => checked(await api.GET("/api/v1/providers", { signal })) });
}
export function useProviderHealth(provider: ProviderId) {
  return useQuery({ queryKey: ["sources", "health", provider], queryFn: async ({ signal }) => checked(await api.GET("/api/v1/providers/{provider_id}/health", { params: { path: { provider_id: provider } }, signal })) });
}
export type PageInput = { q: string; cursor: string | undefined };
export type SourceFilters = { security_id: string; provider_id: "" | ProviderId; from_date: string; to_date: string };
type ObservationPage = { items: Observation[]; next_cursor?: string | null; legacy_records_omitted: number; scope: "IMMUTABLE_SOURCE_V1" };
export function useObservations(mode: ObservationKind, page: PageInput, filters: SourceFilters) {
  return useQuery({ queryKey: ["sources", "observations", mode, filters, page.q, page.cursor], queryFn: async ({ signal }): Promise<ObservationPage> => {
    const query = { q: page.q, cursor: page.cursor, limit: 25, security_id: filters.security_id || undefined, provider_id: filters.provider_id || undefined, from_date: filters.from_date || undefined, to_date: filters.to_date || undefined };
    return mode === "dividends" ? checked(await api.GET("/api/v1/dividends/observations", { params: { query }, signal }))
      : checked(await api.GET("/api/v1/providers/split-observations", { params: { query }, signal }));
  } });
}
export function useCoverage(page: PageInput, filters: { security_id: string; provider_id: "" | ProviderId; operation: Operation }) {
  return useQuery({ queryKey: ["sources", "coverage", filters, page.q, page.cursor], enabled: !!filters.security_id, queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/providers/coverage", { params: { query: { security_id: filters.security_id, provider_id: filters.provider_id || undefined, operation: filters.operation, q: page.q, cursor: page.cursor, limit: 25 } }, signal })),
  select: data => ({ ...data, items: data.items.map(row => ({ ...row, id: `${row.provider_id}:${row.security_id}` })) }) });
}
export function useSchedules(page: PageInput, enabled: "" | "true" | "false") {
  return useQuery({ queryKey: ["sources", "schedules", enabled, page.q, page.cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/source-schedules", { params: { query: { q: page.q, cursor: page.cursor, limit: 25, enabled: enabled === "" ? undefined : enabled === "true" } }, signal })) });
}
export function useSchedule(id: string) {
  return useQuery({ queryKey: ["sources", "schedule", id], enabled: !!id, queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/source-schedules/{id}", { params: { path: { id } }, signal })) });
}
export function useScheduleOccurrences(record: Schedule, page: PageInput) {
  return useQuery({ queryKey: ["sources", "occurrences", record.id, record.version, page.q, page.cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/source-schedules/{id}/occurrences", { params: { path: { id: record.id }, query: { q: page.q, cursor: page.cursor, limit: 25 } }, signal })) });
}
export function useJobs(page: PageInput, status: "" | Job["status"]) {
  return useQuery({ queryKey: ["sources", "jobs", status, page.q, page.cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/jobs", { params: { query: { status: status || undefined, q: page.q, cursor: page.cursor, limit: 25 } }, signal })),
  refetchInterval: query => query.state.data?.items.some(row => pollJob(row.status)) ? 5000 : false });
}
export function useJob(id: string) {
  return useQuery({ queryKey: ["sources", "job", id], enabled: !!id, queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/jobs/{id}", { params: { path: { id } }, signal })), refetchInterval: query => pollJob(query.state.data?.status) });
}
export function useJobItems(job: Job, page: PageInput, status: "" | JobItem["status"]) {
  return useQuery({ queryKey: ["sources", "items", job.id, job.version, status, page.q, page.cursor], queryFn: async ({ signal }) =>
    checked(await api.GET("/api/v1/jobs/{id}/items", { params: { path: { id: job.id }, query: { q: page.q, cursor: page.cursor, limit: 25, status: status || undefined } }, signal })), refetchInterval: pollJob(job.status) });
}
