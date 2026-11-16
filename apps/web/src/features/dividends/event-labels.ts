import type { components } from "@spm/contracts";

type Event = components["schemas"]["DividendEventRecord"];
export const statuses: Record<Event["workflow_status"], string> = {
  PROVISIONAL: "暫定，待核對", CORROBORATED: "來源條款相符，非文件驗證", VERIFIED: "人工接受（非文件驗證）",
  CONFLICT: "來源條款有衝突", INCOMPLETE: "條款不完整", WITHDRAWN: "已撤回，歷史保留",
};
export const eventTypeLabels: Record<Event["event_type"], string> = {
  REGULAR: "一般股息", SPECIAL: "特別股息", INTERIM: "中期股息", FINAL: "末期股息", SUPPLEMENTAL: "補充股息",
  ETF_DISTRIBUTION: "ETF 分派", REIT_DISTRIBUTION: "REIT 分派", UNKNOWN: "類型未知",
};
export const basisLabels: Record<Event["adjustment_basis"], string> = {
  AS_DECLARED: "記錄為宣告口徑，不代表已驗證", SPLIT_ADJUSTED: "拆股調整後，不能直接當作宣告金額", UNKNOWN: "未知，不會猜測",
};
