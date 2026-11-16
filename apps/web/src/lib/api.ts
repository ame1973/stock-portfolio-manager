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
  TRADE_AUDIT_UNAVAILABLE: "交易稽核資料無法安全核對；沒有自動修補，請檢查有效備份及伺服器狀態。",
  TRADE_ORIGIN_REVIEW_REQUIRED: "原始回應或歷史稽核無法相符，請核對原始資料；不會猜測時間或補造證據。",
  TRADE_ORIGIN_ALREADY_RETAINED: "此交易已有稽核來源；請重新載入核對，不能推定本次未確認的操作已完成。",
  INVALID_TRADE_ORIGIN: "交易稽核恢復資料不符，請核對原始回應與依據。",
  INVALID_TRADE_VERSION: "交易版本超出可用範圍，請重新載入核對。",
  TRADE_REVIEW_CHANGED: "稽核缺口或證據已變更；請保留輸入並重新核對，不會猜測你要審核的內容。",
  TRADE_REVIEW_DECISION_CONFLICT: "審核決定已變更；請重新載入核對，不能推定先前操作是否完成。",
  TRADE_REVIEW_CONFLICT: "審核操作識別碼或內容衝突；請先核對已記錄的歷史。",
  TRADE_REVIEW_NOT_REQUIRED: "目前沒有可記錄的缺口；請重新核對稽核來源與審核歷史。",
  TRADE_REVIEW_TIME_CONFLICT: "審核時間與既有歷史衝突；請核對伺服器時間與紀錄。",
  TRADE_REVIEW_UNAVAILABLE: "交易審核歷史無法安全核對；沒有自動修補，請檢查有效備份及伺服器狀態。",
  TRADE_REVIEW_REQUIRED: "至少一筆缺口尚未記錄有效的未驗證審核，或先前審核已撤回；請先核對交易修訂鏈。",
  TRADE_REVIEW_SCOPE_CHANGED: "交易缺口範圍已變更；請重新載入並逐筆核對，不會猜測遺漏的交易。",
  TRADE_CORRECTION_SCOPE_TOO_LARGE: "此交易修訂鏈的缺口超過目前支援數量；不能省略缺口，請先核對歷史。",
  TRADE_CORRECTION_UNAVAILABLE: "交易更正或其原始回應無法安全核對；請保留操作並檢查歷史，不會假定未曾儲存。",
  TRADE_CORRECTION_CONFLICT: "這次未驗證更正與已記錄的操作衝突；請先核對交易修訂歷史。",
  INVALID_TRADE_CORRECTION_SCOPE: "未驗證更正的交易、審核版本或確認範圍不符；請重新核對。",
  INVALID_TRADE_REVIEW_SCOPE: "交易審核版本或決定不符，請重新載入並核對。",
  SOURCE_MANAGED: "此現金項目由來源交易或期初流程管理，不能直接改寫。",
  SYMBOL_HISTORY_DEPENDENCY: "後面已有採納的顯示代號變更，請先核對及處理較後事件，不能倒退覆寫。",
  FUTURE_SYMBOL_CHANGE: "未來的顯示代號只能保留待覆核；到交易所生效日期後再明確採納。",
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
  SOURCE_SCHEDULE_UNAVAILABLE: "排程或歷史紀錄未通過完整性檢查；沒有自動修補，請核對伺服器記錄與有效備份。",
  INVALID_SOURCE_SCHEDULE_SCOPE: "請核對所有證券的交易所市場與所選排程市場；不能用報價幣別推定。",
  SOURCE_SCHEDULE_CHANGE_REJECTED: "未能儲存來源排程；請核對時段、日期與已鎖定的首次回補，重新載入後再檢查。",
  SOURCE_EVIDENCE_UNAVAILABLE: "來源證據未通過完整性檢查；沒有顯示或修補不安全內容，請核對伺服器記錄與有效備份。",
  DIVIDEND_EVENT_HISTORY_UNAVAILABLE: "股息事件歷史無法安全核對；請重新載入或檢查伺服器狀態及有效備份。",
  DIVIDEND_REJECTION_HISTORY_UNAVAILABLE: "拒絕歷史無法安全核對；請重新載入或檢查伺服器狀態及有效備份。",
  DIVIDEND_EVENT_PROJECTION_REQUIRED: "此事件已有應收或投影歷史；原子重建與實收對帳尚未完成，不能記錄此決定或建立新修訂，也不會改寫現金或歷史匯率。",
  DIVIDEND_EVENT_WITHDRAWN: "此事件已撤回，不能直接接受；請重新載入並核對歷史。",
  DIVIDEND_EVENT_UNRESOLVED_BASIS: "事件金額口徑尚未明確記錄為宣告條款，不能人工接受；請先核對來源。",
  DIVIDEND_EVENT_SOURCE_CHANGED: "事件來源或候選依據已變更；請重新載入並核對來源，不能沿用舊決定。",
  CANDIDATE_HISTORY_UNAVAILABLE: "候選歷史無法安全核對；請重新載入或檢查伺服器狀態及有效備份。",
  INVALID_CANDIDATE_DECISION: "候選條款或來源關聯不符；請核對輸入及來源內容大小，保留輸入後重試。",
  CANDIDATE_SECURITY_UNAVAILABLE: "候選證券無法核對；請重新載入並確認穩定證券身分。",
  CANDIDATE_SOURCE_UNAVAILABLE: "來源觀測無法安全核對；請檢查觀測識別碼與保留的來源歷史。",
  CANDIDATE_BASIS_UNAVAILABLE: "基準聲明無法安全核對；請檢查識別碼及已記錄的聲明。",
  CANDIDATE_VERSION_CONFLICT: "候選版本或固定證券已變更；請保留輸入並重新核對候選歷史。",
  CANDIDATE_VERSION_EXHAUSTED: "候選版本已達上限；不能新增修訂，請核對歷史。",
  CANDIDATE_REVISION_CONFLICT: "候選修訂與已記錄歷史衝突；請保留輸入並重新核對。",
  CANDIDATE_TIME_CONFLICT: "候選記錄時間與既有歷史衝突；請檢查伺服器時間及歷史。",
  CANDIDATE_DISMISSED: "候選資料已標記排除；請重新載入並核對候選歷史。",
  DIVIDEND_EVENT_CANDIDATE_ALREADY_USED: "此候選已關聯另一事件；請核對事件與候選歷史，不能建立另一個事件。",
  DIVIDEND_EVENT_LOCKED: "事件已人工鎖定；請重新載入並核對事件歷史。",
  INVALID_DIVIDEND_EVENT_DECISION: "事件條款或候選來源不完整；請核對日期、金額、幣別及來源。",
  CANDIDATE_BASIS_CHANGED: "候選基準與來源不相符或已變更；請重新核對來源及基準聲明。",
  CANDIDATE_SOURCE_CHANGED: "候選來源與證券或雜湊不相符，或來源已變更；請重新核對原始觀測。",
  DIVIDEND_REVISION_ALREADY_REJECTED: "此版本已有拒絕；請保留輸入，重新載入並核對拒絕歷史，不能假定先前操作是否完成。",
  DIVIDEND_REJECTION_CONFLICT: "拒絕決定與既有紀錄衝突；請保留輸入，重新載入並核對拒絕歷史。",
  DIVIDEND_REJECTION_TIME_CONFLICT: "拒絕時間與既有歷史衝突；請核對伺服器時間與拒絕歷史。",
  DIVIDEND_EVENT_VERSION_EXHAUSTED: "事件版本已達上限，不能記錄拒絕或建立新修訂；請核對歷史。",
  DIVIDEND_EVENT_UNLOCK_CONFIRMATION_REQUIRED: "此版本已人工鎖定；請重新載入並明確確認是否解除鎖定。",
  DIVIDEND_EVENT_REOPEN_CONFIRMATION_REQUIRED: "此事件已撤回；請重新載入並明確確認是否重新開啟。",
  DIVIDEND_REJECTION_CONFIRMATION_REQUIRED: "此版本已有拒絕；請重新載入拒絕狀態，並明確核對是否以新修訂處理。",
  INVALID_CITED_ASSERTION: "引用條款或網址歸屬不符；請核對原始文件及人工說明，不會抓取或代為修正。",
  CITED_SOURCE_IDENTITY_UNRESOLVED: "請核對引用日期、穩定證券與該日明確來源代號；不會猜 ticker 或用擷取日代替。",
  CITED_SOURCE_CHANGED: "來源權限或代號已變更，未保存這次新引用；請核對目前設定及原始輸入後重試。",
  CITED_SOURCE_UNCONFIGURED: "目前來源存取／權利不可用；請另行核對來源政策。未確認的原鍵重試仍會由伺服器判斷。",
  CITED_SOURCE_UNAVAILABLE: "來源引用無法安全保存或讀取；沒有自動修補或新增假證據，請保留輸入並核對。",
  CITED_ASSERTION_RETRY_REQUIRED: "原回覆已無法安全重用；請先核對既有引用，再取消並另開明確新意圖，不能假定先前未寫入。",
  PROVIDER_PROFILE_UNAVAILABLE: "來源政策未通過完整性檢查；沒有修補或回退到舊金鑰，請核對伺服器記錄與有效備份。",
  PROVIDER_PROFILE_CHANGE_REJECTED: "未能儲存來源政策；請核對是否已有個人決定、版本及金鑰參考，不會自動恢復舊來源。",
  INVALID_PROVIDER_PROFILE: "來源政策格式或範圍不符；請核對明確權利、上限、停用與金鑰參考。",
  PROVIDER_CREDENTIAL_STORAGE_UNAVAILABLE: "伺服器私密金鑰儲存區不可用；未確認匯入成功，請由部署管理者核對權限與設定。",
  CREDENTIAL_RETRY_REQUIRED: "這次匯入無法安全重用原回覆；請重新核對目前版本，再建立明確新匯入意圖。",
  INVALID_SECURITY_SCOPE: "請核對明確選擇的穩定證券身分與數量；不會自動擴大擷取範圍。",
  JOB_REQUEST_REJECTED: "未能建立來源工作，請核對證券、來源及日期後重試；不會自動啟用來源。",
  JOB_CANCEL_REJECTED: "未能確認取消意圖，請重新載入工作狀態；不能視為已停止。",
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
