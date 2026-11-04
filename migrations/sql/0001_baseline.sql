-- Stock Portfolio Manager 2.0 target SQLite DDL (v1.0.0), schema REFERENCE.
-- The actual application MUST implement and maintain this schema via Alembic migrations.
-- Money micro-units (1e-6), Shares 1e-8, Price/DPS 1e-8, FX 1e-12, Rate 1e-6.
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS owners (
  id TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  password_changed_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
  token_sha256 TEXT NOT NULL UNIQUE,
  csrf_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  user_agent_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_owner_exp ON sessions(owner_id,expires_at);
CREATE TABLE IF NOT EXISTS settings (
  owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL CHECK(json_valid(value_json)),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(owner_id,key)
);
CREATE TABLE IF NOT EXISTS portfolios (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
  name TEXT NOT NULL CHECK(length(trim(name))>0),
  base_currency TEXT NOT NULL DEFAULT 'HKD' CHECK(base_currency='HKD'),
  cash_completeness TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK(cash_completeness IN ('UNKNOWN','PARTIAL','COMPLETE')),
  active_projection_version INTEGER NOT NULL DEFAULT 0 CHECK(active_projection_version>=0),
  data_revision INTEGER NOT NULL DEFAULT 0 CHECK(data_revision>=0),
  version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1),
  created_at TEXT NOT NULL,
  UNIQUE(owner_id,name)
);
CREATE TABLE IF NOT EXISTS brokers (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
  name TEXT NOT NULL CHECK(length(trim(name))>0),
  dividend_fee_micro INTEGER NOT NULL DEFAULT 0 CHECK(dividend_fee_micro>=0),
  dividend_fee_currency TEXT NOT NULL DEFAULT 'HKD' CHECK(dividend_fee_currency IN ('HKD','USD')),
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','ARCHIVED')),
  UNIQUE(owner_id,name)
);
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE RESTRICT,
  broker_id TEXT NOT NULL REFERENCES brokers(id) ON DELETE RESTRICT,
  name TEXT NOT NULL CHECK(length(trim(name))>0),
  default_currency TEXT NOT NULL CHECK(default_currency IN ('HKD','USD')),
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','ARCHIVED')),
  opening_cash_micro INTEGER,
  cash_completeness TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK(cash_completeness IN ('UNKNOWN','PARTIAL','COMPLETE')),
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE(portfolio_id,name)
);
CREATE INDEX IF NOT EXISTS idx_accounts_broker ON accounts(broker_id);
CREATE TABLE IF NOT EXISTS exchanges (
  code TEXT PRIMARY KEY,
  market TEXT NOT NULL CHECK(market IN ('HK','US')),
  timezone TEXT NOT NULL,
  default_currency TEXT NOT NULL CHECK(default_currency IN ('HKD','USD')),
  display_name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS securities (
  id TEXT PRIMARY KEY,
  exchange_code TEXT NOT NULL REFERENCES exchanges(code) ON DELETE RESTRICT,
  display_ticker TEXT NOT NULL CHECK(length(trim(display_ticker))>0),
  name TEXT NOT NULL,
  security_type TEXT NOT NULL DEFAULT 'EQUITY' CHECK(security_type IN ('EQUITY','ETF','REIT','ADR','FUND','OTHER')),
  quote_currency TEXT NOT NULL CHECK(quote_currency IN ('HKD','USD')),
  isin TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','SUSPENDED','DELISTED','UNKNOWN')),
  verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
  created_at TEXT NOT NULL,
  UNIQUE(exchange_code,display_ticker)
);
CREATE TABLE IF NOT EXISTS security_identifiers (
  id TEXT PRIMARY KEY,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  exchange_code TEXT NOT NULL REFERENCES exchanges(code) ON DELETE RESTRICT,
  identifier_type TEXT NOT NULL CHECK(identifier_type IN ('TICKER','ISIN','MIC','OTHER')),
  identifier_value TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  CHECK(effective_to IS NULL OR effective_to>=effective_from),
  UNIQUE(exchange_code,identifier_type,identifier_value,effective_from)
);
CREATE INDEX IF NOT EXISTS idx_identifiers_security_dates ON security_identifiers(security_id,effective_from,effective_to);
CREATE TABLE IF NOT EXISTS provider_symbols (
  id TEXT PRIMARY KEY,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  provider_id TEXT NOT NULL,
  provider_symbol TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
  CHECK(effective_to IS NULL OR effective_to>=effective_from),
  UNIQUE(provider_id,security_id,effective_from)
);
CREATE INDEX IF NOT EXISTS idx_provider_symbol ON provider_symbols(provider_id,provider_symbol);

CREATE TABLE IF NOT EXISTS import_batches (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
  file_name TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  format TEXT NOT NULL CHECK(format IN ('XLSX','CSV','JSON')),
  schema_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('UPLOADED','PREVIEWED','COMMITTED','PARTIAL','FAILED','ROLLED_BACK')),
  mode TEXT CHECK(mode IN ('ALL_OR_NOTHING','PARTIAL_CONFIRMED')),
  created_at TEXT NOT NULL,
  committed_at TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(summary_json)),
  UNIQUE(owner_id,file_sha256,schema_version)
);
CREATE TABLE IF NOT EXISTS import_rows (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES import_batches(id) ON DELETE RESTRICT,
  sheet_name TEXT NOT NULL,
  row_number INTEGER NOT NULL CHECK(row_number>=1),
  normalized_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('VALID','INVALID','DUPLICATE','IMPORTED','SKIPPED','ROLLED_BACK')),
  entity_type TEXT,
  entity_id TEXT,
  errors_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(errors_json)),
  UNIQUE(batch_id,sheet_name,row_number)
);
CREATE INDEX IF NOT EXISTS idx_import_rows_hash ON import_rows(normalized_sha256);

CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY,
  portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE RESTRICT,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
  side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  trade_date TEXT NOT NULL,
  executed_at_utc TEXT,
  quantity_e8 INTEGER NOT NULL CHECK(quantity_e8>0),
  price_e8 INTEGER NOT NULL CHECK(price_e8>=0),
  fee_micro INTEGER NOT NULL DEFAULT 0 CHECK(fee_micro>=0),
  tax_micro INTEGER NOT NULL DEFAULT 0 CHECK(tax_micro>=0),
  currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
  fx_rate_hkd_e12 INTEGER CHECK(fx_rate_hkd_e12>0),
  fx_source TEXT,
  gross_micro INTEGER NOT NULL CHECK(gross_micro>=0),
  settlement_micro INTEGER NOT NULL,
  booked_hkd_micro INTEGER,
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('DRAFT_SCHEDULED','PENDING_FX','ACTIVE','SUPERSEDED','VOID')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  supersedes_id TEXT REFERENCES trades(id) ON DELETE RESTRICT,
  import_row_id TEXT REFERENCES import_rows(id) ON DELETE RESTRICT,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(status!='ACTIVE' OR (fx_rate_hkd_e12 IS NOT NULL AND booked_hkd_micro IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_trades_replay ON trades(account_id,security_id,trade_date,executed_at_utc,id);
CREATE INDEX IF NOT EXISTS idx_trades_portfolio_date ON trades(portfolio_id,trade_date,id);
CREATE INDEX IF NOT EXISTS idx_trades_supersedes ON trades(supersedes_id);
CREATE TABLE IF NOT EXISTS cash_ledger_entries (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  effective_date TEXT NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
  amount_micro INTEGER NOT NULL,
  fx_rate_hkd_e12 INTEGER CHECK(fx_rate_hkd_e12>0),
  kind TEXT NOT NULL CHECK(kind IN ('DEPOSIT','WITHDRAW','TRANSFER_IN','TRANSFER_OUT','TRADE_SETTLEMENT','DIVIDEND_RECEIPT','EXPENSE','ADJUSTMENT','REVERSAL','CORPORATE_CASH')),
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  entry_role TEXT NOT NULL DEFAULT 'PRIMARY' CHECK(entry_role IN ('PRIMARY','REVERSAL')),
  external_flow INTEGER NOT NULL DEFAULT 0 CHECK(external_flow IN (0,1)),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1),
  UNIQUE(account_id,source_type,source_id,entry_role)
);
CREATE INDEX IF NOT EXISTS idx_cash_ledger_account_date ON cash_ledger_entries(account_id,effective_date,id);
CREATE TABLE IF NOT EXISTS expenses (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  date TEXT NOT NULL,
  category TEXT NOT NULL,
  amount_micro INTEGER NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
  fx_rate_hkd_e12 INTEGER CHECK(fx_rate_hkd_e12>0),
  hkd_micro INTEGER,
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','SUPERSEDED','VOID')),
  supersedes_id TEXT REFERENCES expenses(id) ON DELETE RESTRICT,
  version INTEGER NOT NULL DEFAULT 1,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expenses_account_date ON expenses(account_id,date);
CREATE TABLE IF NOT EXISTS security_transfers (
  id TEXT PRIMARY KEY,
  portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE RESTRICT,
  source_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  target_account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
  transfer_date TEXT NOT NULL,
  quantity_e8 INTEGER NOT NULL CHECK(quantity_e8>0),
  transferred_cost_hkd_micro INTEGER NOT NULL CHECK(transferred_cost_hkd_micro>=0),
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','VOID')),
  created_at TEXT NOT NULL,
  CHECK(source_account_id<>target_account_id)
);
CREATE INDEX IF NOT EXISTS idx_security_transfer_date ON security_transfers(security_id,transfer_date);
CREATE TABLE IF NOT EXISTS corporate_actions (
  id TEXT PRIMARY KEY,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
  action_type TEXT NOT NULL CHECK(action_type IN ('SPLIT','REVERSE_SPLIT','SYMBOL_CHANGE','CASH_IN_LIEU','MERGER','SPIN_OFF')),
  effective_date TEXT NOT NULL,
  ratio_num INTEGER CHECK(ratio_num>0),
  ratio_den INTEGER CHECK(ratio_den>0),
  new_symbol TEXT,
  evidence_url TEXT,
  status TEXT NOT NULL CHECK(status IN ('CONFIRMED','NEEDS_REVIEW','VOID')),
  version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1),
  created_at TEXT NOT NULL,
  CHECK(action_type NOT IN ('SPLIT','REVERSE_SPLIT') OR (ratio_num IS NOT NULL AND ratio_den IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_actions_security_date ON corporate_actions(security_id,effective_date);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('QUEUED','LEASED','RUNNING','RETRYING','PARTIAL','SUCCEEDED','FAILED','CANCEL_REQUESTED','CANCELLED')),
  progress_done INTEGER NOT NULL DEFAULT 0 CHECK(progress_done>=0),
  progress_total INTEGER CHECK(progress_total>=0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
  max_attempts INTEGER NOT NULL DEFAULT 4 CHECK(max_attempts>=1),
  lease_owner TEXT,
  lease_expires_at TEXT,
  next_run_at TEXT,
  request_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(request_json)),
  result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
  last_error_code TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status,next_run_at,lease_expires_at);
CREATE TABLE IF NOT EXISTS provider_runs (
  id TEXT PRIMARY KEY,
  job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
  provider_id TEXT NOT NULL,
  origin_group TEXT NOT NULL,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('OK_WITH_DATA','NO_DATA','EMPTY_CONFIRMED','UNSUPPORTED','UNCONFIGURED','RATE_LIMITED','AUTH_ERROR','TRANSIENT_ERROR','PARSE_ERROR','STALE')),
  coverage_start TEXT,
  coverage_end TEXT,
  observations_count INTEGER NOT NULL DEFAULT 0 CHECK(observations_count>=0),
  http_status INTEGER,
  sanitized_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_provider_runs_stock ON provider_runs(security_id,provider_id,started_at);
CREATE TABLE IF NOT EXISTS provider_status (
  provider_id TEXT NOT NULL,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  provider_symbol TEXT,
  origin_group TEXT NOT NULL,
  status TEXT NOT NULL,
  last_success_at TEXT,
  coverage_start TEXT,
  coverage_end TEXT,
  last_error_code TEXT,
  PRIMARY KEY(provider_id,security_id)
);
CREATE TABLE IF NOT EXISTS dividend_events (
  id TEXT PRIMARY KEY,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
  event_identity_key TEXT NOT NULL,
  current_revision_id TEXT REFERENCES dividend_event_revisions(id) ON DELETE RESTRICT,
  workflow_status TEXT NOT NULL DEFAULT 'PROVISIONAL' CHECK(workflow_status IN ('PROVISIONAL','CORROBORATED','VERIFIED','CONFLICT','INCOMPLETE','WITHDRAWN')),
  version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1),
  created_at TEXT NOT NULL,
  UNIQUE(security_id,event_identity_key)
);
CREATE TABLE IF NOT EXISTS dividend_event_revisions (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES dividend_events(id) ON DELETE RESTRICT,
  revision_no INTEGER NOT NULL CHECK(revision_no>=1),
  event_type TEXT NOT NULL CHECK(event_type IN ('REGULAR','SPECIAL','INTERIM','FINAL','SUPPLEMENTAL','ETF_DISTRIBUTION','REIT_DISTRIBUTION','UNKNOWN')),
  ex_date TEXT NOT NULL,
  announce_date TEXT,
  record_date TEXT,
  pay_date TEXT,
  dps_e8 INTEGER NOT NULL CHECK(dps_e8>=0),
  currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
  adjustment_basis TEXT NOT NULL CHECK(adjustment_basis IN ('AS_DECLARED','SPLIT_ADJUSTED','UNKNOWN')),
  review_status TEXT NOT NULL CHECK(review_status IN ('PROVISIONAL','CORROBORATED','VERIFIED','CONFLICT','INCOMPLETE','WITHDRAWN')),
  manual_locked INTEGER NOT NULL DEFAULT 0 CHECK(manual_locked IN (0,1)),
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(event_id,revision_no)
);
CREATE INDEX IF NOT EXISTS idx_dividend_revisions_ex ON dividend_event_revisions(ex_date,event_id);
CREATE TABLE IF NOT EXISTS dividend_observations (
  id TEXT PRIMARY KEY,
  provider_run_id TEXT REFERENCES provider_runs(id) ON DELETE SET NULL,
  provider_id TEXT NOT NULL,
  origin_group TEXT NOT NULL,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE RESTRICT,
  provider_symbol TEXT NOT NULL,
  provider_event_id TEXT,
  normalized_sha256 TEXT NOT NULL,
  raw_sha256 TEXT,
  source_url TEXT,
  ex_date TEXT,
  announce_date TEXT,
  record_date TEXT,
  pay_date TEXT,
  event_type TEXT NOT NULL DEFAULT 'UNKNOWN',
  dps_e8 INTEGER CHECK(dps_e8>=0),
  currency TEXT CHECK(currency IN ('HKD','USD')),
  adjustment_basis TEXT NOT NULL CHECK(adjustment_basis IN ('AS_DECLARED','SPLIT_ADJUSTED','UNKNOWN')),
  raw_permitted INTEGER NOT NULL DEFAULT 0 CHECK(raw_permitted IN (0,1)),
  retrieved_at TEXT NOT NULL,
  source_metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(source_metadata_json)),
  UNIQUE(provider_id,security_id,normalized_sha256)
);
CREATE INDEX IF NOT EXISTS idx_dividend_obs_security ON dividend_observations(security_id,ex_date,provider_id);
CREATE TABLE IF NOT EXISTS dividend_evidence_links (
  revision_id TEXT NOT NULL REFERENCES dividend_event_revisions(id) ON DELETE RESTRICT,
  observation_id TEXT NOT NULL REFERENCES dividend_observations(id) ON DELETE RESTRICT,
  field_names_json TEXT NOT NULL CHECK(json_valid(field_names_json)),
  PRIMARY KEY(revision_id,observation_id)
);
CREATE TABLE IF NOT EXISTS dividend_entitlements (
  id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL REFERENCES dividend_event_revisions(id) ON DELETE RESTRICT,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  eligible_shares_e8 INTEGER NOT NULL CHECK(eligible_shares_e8>0),
  gross_micro INTEGER NOT NULL,
  tax_micro INTEGER NOT NULL DEFAULT 0,
  fee_micro INTEGER NOT NULL DEFAULT 0,
  expected_net_micro INTEGER NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
  fx_rate_hkd_e12 INTEGER CHECK(fx_rate_hkd_e12>0),
  expected_hkd_micro INTEGER,
  quality TEXT NOT NULL CHECK(quality IN ('ESTIMATED','ACCRUED','NEEDS_REVIEW','CONFIRMED','MISSING_FX')),
  tax_rate_snapshot_ppm INTEGER CHECK(tax_rate_snapshot_ppm BETWEEN 0 AND 1000000),
  projection_version INTEGER NOT NULL CHECK(projection_version>=0),
  created_at TEXT NOT NULL,
  UNIQUE(revision_id,account_id)
);
CREATE INDEX IF NOT EXISTS idx_entitlements_account ON dividend_entitlements(account_id,revision_id);
CREATE TABLE IF NOT EXISTS dividend_receipts (
  id TEXT PRIMARY KEY,
  entitlement_id TEXT NOT NULL REFERENCES dividend_entitlements(id) ON DELETE RESTRICT,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  received_date TEXT NOT NULL,
  received_micro INTEGER NOT NULL,
  currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
  tax_micro INTEGER NOT NULL DEFAULT 0,
  fee_micro INTEGER NOT NULL DEFAULT 0,
  fx_rate_hkd_e12 INTEGER CHECK(fx_rate_hkd_e12>0),
  received_hkd_micro INTEGER,
  note TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'MANUAL' CHECK(source IN ('MANUAL','IMPORT')),
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','VOID')),
  version INTEGER NOT NULL DEFAULT 1 CHECK(version>=1),
  voided_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_account_date ON dividend_receipts(account_id,received_date);
CREATE TABLE IF NOT EXISTS review_items (
  id TEXT PRIMARY KEY,
  portfolio_id TEXT REFERENCES portfolios(id) ON DELETE RESTRICT,
  event_id TEXT REFERENCES dividend_events(id) ON DELETE RESTRICT,
  account_id TEXT REFERENCES accounts(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN ('INFO','WARNING','CRITICAL')),
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','ACKNOWLEDGED','RESOLVED','DISMISSED')),
  reason_code TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(detail_json)),
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviews_open ON review_items(status,severity,created_at);

CREATE TABLE IF NOT EXISTS quotes (
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  provider_id TEXT NOT NULL,
  price_e8 INTEGER NOT NULL CHECK(price_e8>=0),
  currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
  prev_close_e8 INTEGER CHECK(prev_close_e8>=0),
  quote_as_of_utc TEXT NOT NULL,
  fetched_at_utc TEXT NOT NULL,
  quality TEXT NOT NULL CHECK(quality IN ('DELAYED','EOD','REALTIME','STALE','UNKNOWN')),
  PRIMARY KEY(security_id,provider_id)
);
CREATE TABLE IF NOT EXISTS historical_prices (
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  close_e8 INTEGER NOT NULL CHECK(close_e8>=0),
  currency TEXT NOT NULL CHECK(currency IN ('HKD','USD')),
  adjusted INTEGER NOT NULL DEFAULT 0 CHECK(adjusted IN (0,1)),
  quality TEXT NOT NULL CHECK(quality IN ('RAW','ADJUSTED','CARRIED_FORWARD','UNKNOWN')),
  fetched_at_utc TEXT NOT NULL,
  PRIMARY KEY(security_id,date,provider_id,adjusted)
);
CREATE TABLE IF NOT EXISTS fx_rates (
  base_currency TEXT NOT NULL CHECK(base_currency IN ('USD','HKD')),
  quote_currency TEXT NOT NULL CHECK(quote_currency IN ('USD','HKD')),
  as_of_date TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  rate_e12 INTEGER NOT NULL CHECK(rate_e12>0),
  quality TEXT NOT NULL CHECK(quality IN ('ACTUAL','HISTORICAL','ESTIMATED','CARRIED_FORWARD')),
  retrieved_at_utc TEXT NOT NULL,
  PRIMARY KEY(base_currency,quote_currency,as_of_date,provider_id)
);
CREATE TABLE IF NOT EXISTS positions (
  portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  security_id TEXT NOT NULL REFERENCES securities(id) ON DELETE CASCADE,
  projection_version INTEGER NOT NULL CHECK(projection_version>=0),
  shares_e8 INTEGER NOT NULL CHECK(shares_e8>=0),
  remaining_cost_hkd_micro INTEGER NOT NULL CHECK(remaining_cost_hkd_micro>=0),
  realized_hkd_micro INTEGER NOT NULL,
  calculated_at_utc TEXT NOT NULL,
  PRIMARY KEY(portfolio_id,account_id,security_id,projection_version)
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  projection_version INTEGER NOT NULL CHECK(projection_version>=0),
  market_value_hkd_micro INTEGER,
  cash_hkd_micro INTEGER,
  nav_hkd_micro INTEGER,
  realized_hkd_micro INTEGER NOT NULL,
  unrealized_hkd_micro INTEGER,
  recognized_dividend_hkd_micro INTEGER,
  operating_expenses_hkd_micro INTEGER NOT NULL,
  total_pnl_hkd_micro INTEGER,
  quality TEXT NOT NULL CHECK(quality IN ('COMPLETE','PARTIAL','MISSING_QUOTE','MISSING_FX','STALE')),
  reason_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(reason_json)),
  computed_at_utc TEXT NOT NULL,
  PRIMARY KEY(portfolio_id,date,projection_version)
);
CREATE TABLE IF NOT EXISTS projection_invalidations (
  id TEXT PRIMARY KEY,
  portfolio_id TEXT NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
  from_date TEXT NOT NULL,
  reason TEXT NOT NULL,
  source_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_invalidations_unresolved ON projection_invalidations(portfolio_id,resolved_at,from_date);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
  route TEXT NOT NULL,
  idem_key TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  response_status INTEGER,
  response_json TEXT CHECK(response_json IS NULL OR json_valid(response_json)),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  UNIQUE(owner_id,route,idem_key)
);
CREATE TABLE IF NOT EXISTS backup_records (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
  file_name TEXT NOT NULL UNIQUE,
  schema_revision TEXT NOT NULL,
  created_at TEXT NOT NULL,
  size_bytes INTEGER NOT NULL CHECK(size_bytes>0),
  sha256 TEXT NOT NULL,
  encrypted INTEGER NOT NULL CHECK(encrypted IN (0,1)),
  verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
  retention_bucket TEXT NOT NULL CHECK(retention_bucket IN ('DAILY','WEEKLY','MONTHLY','MANUAL')),
  manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json))
);
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE RESTRICT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  action TEXT NOT NULL,
  before_sha256 TEXT,
  after_sha256 TEXT,
  reason TEXT NOT NULL DEFAULT '',
  request_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type,entity_id,created_at);
CREATE INDEX IF NOT EXISTS idx_audit_owner_date ON audit_events(owner_id,created_at);

-- SEED only verified market defaults; no hardcoded owner/password or provider key.
INSERT OR IGNORE INTO exchanges(code,market,timezone,default_currency,display_name) VALUES
  ('HKG','HK','Asia/Hong_Kong','HKD','Hong Kong Stock Exchange'),
  ('NASDAQ','US','America/New_York','USD','Nasdaq'),
  ('NYSE','US','America/New_York','USD','New York Stock Exchange'),
  ('NYSEAMERICAN','US','America/New_York','USD','NYSE American'),
  ('NYSEARCA','US','America/New_York','USD','NYSE Arca');

-- Cross-entity relational invariants. Application validation is still required,
-- but a direct SQL/import path cannot connect a trade or transfer to a wrong portfolio.
CREATE TRIGGER IF NOT EXISTS trg_trades_account_portfolio_insert
BEFORE INSERT ON trades
WHEN (SELECT portfolio_id FROM accounts WHERE id=NEW.account_id) IS NOT NEW.portfolio_id
BEGIN SELECT RAISE(ABORT, 'TRADE_ACCOUNT_PORTFOLIO_MISMATCH'); END;
CREATE TRIGGER IF NOT EXISTS trg_trades_account_portfolio_update
BEFORE UPDATE OF account_id, portfolio_id ON trades
WHEN (SELECT portfolio_id FROM accounts WHERE id=NEW.account_id) IS NOT NEW.portfolio_id
BEGIN SELECT RAISE(ABORT, 'TRADE_ACCOUNT_PORTFOLIO_MISMATCH'); END;
CREATE TRIGGER IF NOT EXISTS trg_transfer_accounts_insert
BEFORE INSERT ON security_transfers
WHEN (SELECT portfolio_id FROM accounts WHERE id=NEW.source_account_id) IS NOT NEW.portfolio_id
  OR (SELECT portfolio_id FROM accounts WHERE id=NEW.target_account_id) IS NOT NEW.portfolio_id
BEGIN SELECT RAISE(ABORT, 'TRANSFER_ACCOUNT_PORTFOLIO_MISMATCH'); END;
CREATE TRIGGER IF NOT EXISTS trg_transfer_accounts_update
BEFORE UPDATE OF portfolio_id, source_account_id, target_account_id ON security_transfers
WHEN (SELECT portfolio_id FROM accounts WHERE id=NEW.source_account_id) IS NOT NEW.portfolio_id
  OR (SELECT portfolio_id FROM accounts WHERE id=NEW.target_account_id) IS NOT NEW.portfolio_id
BEGIN SELECT RAISE(ABORT, 'TRANSFER_ACCOUNT_PORTFOLIO_MISMATCH'); END;
CREATE TRIGGER IF NOT EXISTS trg_positions_account_portfolio_insert
BEFORE INSERT ON positions
WHEN (SELECT portfolio_id FROM accounts WHERE id=NEW.account_id) IS NOT NEW.portfolio_id
BEGIN SELECT RAISE(ABORT, 'POSITION_ACCOUNT_PORTFOLIO_MISMATCH'); END;
CREATE TRIGGER IF NOT EXISTS trg_positions_account_portfolio_update
BEFORE UPDATE OF account_id, portfolio_id ON positions
WHEN (SELECT portfolio_id FROM accounts WHERE id=NEW.account_id) IS NOT NEW.portfolio_id
BEGIN SELECT RAISE(ABORT, 'POSITION_ACCOUNT_PORTFOLIO_MISMATCH'); END;
CREATE TRIGGER IF NOT EXISTS trg_dividend_receipt_account_insert
BEFORE INSERT ON dividend_receipts
WHEN (SELECT account_id FROM dividend_entitlements WHERE id=NEW.entitlement_id) IS NOT NEW.account_id
BEGIN SELECT RAISE(ABORT, 'RECEIPT_ACCOUNT_ENTITLEMENT_MISMATCH'); END;
CREATE TRIGGER IF NOT EXISTS trg_dividend_receipt_account_update
BEFORE UPDATE OF account_id, entitlement_id ON dividend_receipts
WHEN (SELECT account_id FROM dividend_entitlements WHERE id=NEW.entitlement_id) IS NOT NEW.account_id
BEGIN SELECT RAISE(ABORT, 'RECEIPT_ACCOUNT_ENTITLEMENT_MISMATCH'); END;
CREATE TRIGGER IF NOT EXISTS trg_dividend_current_revision_insert
BEFORE INSERT ON dividend_events
WHEN NEW.current_revision_id IS NOT NULL
  AND (SELECT event_id FROM dividend_event_revisions WHERE id=NEW.current_revision_id) IS NOT NEW.id
BEGIN SELECT RAISE(ABORT, 'DIVIDEND_REVISION_EVENT_MISMATCH'); END;
CREATE TRIGGER IF NOT EXISTS trg_dividend_current_revision_update
BEFORE UPDATE OF current_revision_id ON dividend_events
WHEN NEW.current_revision_id IS NOT NULL
  AND (SELECT event_id FROM dividend_event_revisions WHERE id=NEW.current_revision_id) IS NOT NEW.id
BEGIN SELECT RAISE(ABORT, 'DIVIDEND_REVISION_EVENT_MISMATCH'); END;

-- Evidence and accepted event revisions are immutable; corrections append new rows.
CREATE TRIGGER IF NOT EXISTS trg_dividend_observation_no_update
BEFORE UPDATE ON dividend_observations
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DIVIDEND_OBSERVATION'); END;
CREATE TRIGGER IF NOT EXISTS trg_dividend_observation_no_delete
BEFORE DELETE ON dividend_observations
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DIVIDEND_OBSERVATION'); END;
CREATE TRIGGER IF NOT EXISTS trg_dividend_event_revision_no_update
BEFORE UPDATE ON dividend_event_revisions
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DIVIDEND_EVENT_REVISION'); END;
CREATE TRIGGER IF NOT EXISTS trg_dividend_event_revision_no_delete
BEFORE DELETE ON dividend_event_revisions
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DIVIDEND_EVENT_REVISION'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_event_no_update
BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_AUDIT_EVENT'); END;
CREATE TRIGGER IF NOT EXISTS trg_audit_event_no_delete
BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_AUDIT_EVENT'); END;
