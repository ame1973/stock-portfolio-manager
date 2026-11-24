import type { components } from "@spm/contracts";
import { account, event } from "./projection-test-data";

type Record = components["schemas"]["RecordedReconciliation"];

export const reconciliation: Record = {
  event_id: event.id, revision_id: event.current_revision_id, revision_no: event.version, account_id: account.id,
  projection_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", projection_no: "9007199254740993", projection_as_of: "2026-11-03",
  projection_state: "EXPECTED", projection_quality: "OK", comparison_quality: "OK", currency: "USD",
  recorded_expected: "11.000000", recorded_received: "6.000000", recorded_outstanding: "5.000000",
  active_receipts: "1", void_receipts: "0", unverified_origins: "1", unavailable_cash_links: "0", state: "RECORDED_PARTIAL",
  scope: "UNVERIFIED_RECORDED_RECONCILIATION_ONLY", history_completeness: "UNVERIFIED", current_use_scope: "REVALIDATION_REQUIRED",
  eligibility_scope: "NOT_ESTABLISHED", payment_scope: "OWNER_RECORDED_NOT_VERIFIED",
};
