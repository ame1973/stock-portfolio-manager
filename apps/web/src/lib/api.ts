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
  PRECONDITION_FAILED: "紀錄已變更，請重新載入後再編輯。",
  IDEMPOTENCY_CONFLICT: "此操作識別碼已用於不同內容，請重新檢查。",
  REAUTH_REQUIRED: "請再次輸入密碼以確認這項敏感操作。",
};

export function errorMessage(error: { code?: string } | undefined) {
  return messages[error?.code ?? ""] ?? "操作未完成，請檢查連線後重試。";
}

export async function mutationHeaders(key = crypto.randomUUID()) {
  const { data, error } = await api.GET("/api/v1/auth/csrf");
  if (!data || error) throw new Error(errorMessage(error));
  return { "X-CSRF-Token": data.csrf_token, "Idempotency-Key": key };
}
