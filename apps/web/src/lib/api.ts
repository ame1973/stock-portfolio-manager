import { api } from "@spm/contracts";

const messages: Record<string, string> = {
  UNAUTHORIZED: "密碼不正確或登入已過期，請重新登入。",
  INVALID_SETUP_TOKEN: "初始化 token 無效或已過期，請從本機 CLI 重新產生。",
  ALREADY_INITIALIZED: "帳戶已建立，請重新載入並登入。",
  RATE_LIMITED: "嘗試次數過多，請稍候再試。",
  ORIGIN_REJECTED: "網站位址與伺服器設定不一致，請確認 PUBLIC_ORIGIN。",
  HTTPS_REQUIRED: "請使用設定的 HTTPS 安全位址。",
  CSRF_REJECTED: "安全驗證已更新，請重試操作。",
  VALIDATION_ERROR: "輸入格式不正確，請檢查欄位與密碼要求。",
  INVALID_PATCH: "請提供有效且非空白的修改內容。",
  PRECONDITION_FAILED: "紀錄已變更；請取消編輯、重新載入後再核對，不會覆蓋新版資料。",
  IDEMPOTENCY_CONFLICT: "此操作識別碼已用於不同內容，請重新檢查。",
  REAUTH_REQUIRED: "請再次輸入密碼以確認這項敏感操作。",
  CONFLICT: "紀錄重複或識別碼有效期重疊，請重新核對。",
  NOT_FOUND: "找不到紀錄，或你無權存取這項資料。",
  MISSING_FX: "缺少認列當日的歷史匯率，不能使用現價或預設匯率入帳。",
  FINANCIAL_OVERFLOW: "計算結果超過可儲存範圍，請核對金額與歷史匯率。",
  INSUFFICIENT_SHARES: "持倉不足，或更正會使後續賣出／轉倉超額；沒有任何金融紀錄被更動。",
  NEEDS_REVIEW: "財務歷史含未分配的企業行動、零碎股或不完整來源，請先核對；不會猜測金額或股數。",
  INACTIVE_FACT: "此版本已作廢或被新修訂取代，請重新載入目前版本。",
  SOURCE_MANAGED: "此現金項目由來源交易或期初流程管理，不能直接改寫。",
  IMMUTABLE_IDENTITY: "此修訂不能改變穩定身分，請核對原始事件。",
  INVALID_FINANCIAL_VALUE: "數值不符合財務規則，請核對正負號、數量及實際交收依據。",
  EXECUTION_DATE_MISMATCH: "成交時間的交易所當地日期與交易日期不符，請核對時區與日期。",
  PORTFOLIO_MISMATCH: "來源與目標帳戶必須屬於同一所選組合；請重新核對，不能藉轉倉跨組合換幣。",
  FUTURE_TRANSFER: "股票轉倉只記錄證券交易所當地日期已發生的移動；未來日期不會建立自動生效草稿。",
  ARCHIVED: "這項紀錄已封存，請先重新啟用。",
  EXCHANGE_MISMATCH: "識別碼必須屬於同一交易所。",
  CURRENCY_MISMATCH: "此市場與報價幣別不符，請重新核對。",
  UNSUPPORTED_MARKET: "目前僅支援已列出的香港與美國交易所。",
  INVALID_CURSOR: "分頁狀態已失效，請重新開始查詢。",
  MIGRATION_REQUIRED: "資料庫版本不符，請先完成遷移。",
  DATABASE_UNAVAILABLE: "資料庫暫時無法使用，請稍候重試。",
};

export function errorMessage(error: { code?: string } | undefined) {
  return messages[error?.code ?? ""] ?? "操作未完成，請檢查連線後重試。";
}

export class ApiRequestError extends Error {
  readonly code: string;
  constructor(error?: { code?: string }) {
    super(errorMessage(error));
    this.code = error?.code ?? "UNKNOWN";
  }
}

export function checked<T>(result: { data?: T; error?: { code?: string } }, expireSession = true): T {
  if (result.error || result.data === undefined) {
    if (expireSession && result.error?.code === "UNAUTHORIZED") {
      window.dispatchEvent(new Event("spm:session-expired"));
    }
    throw new ApiRequestError(result.error);
  }
  return result.data;
}

export function failureMessage(cause: unknown) {
  if (cause instanceof TypeError) return "網絡連線中斷，請檢查連線後重試。";
  return cause instanceof Error ? cause.message : "操作未完成，請重試。";
}

export async function mutationHeaders(key: string = crypto.randomUUID()) {
  const data = checked(await api.GET("/api/v1/auth/csrf"));
  return { "X-CSRF-Token": data.csrf_token, "Idempotency-Key": key };
}
