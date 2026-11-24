import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, type Page } from "@playwright/test";
import type { components } from "../../../packages/contracts/src/schema";

export type Event = components["schemas"]["DividendEventRecord"];
type Tables = Record<string, string[]>;
const origin = process.env.SPM_E2E_BASE_URL!;
export function stage(value: string) { writeFileSync(join(process.env.SPM_E2E_CREDENTIAL_ROOT!, "stage"), value); }
export function database(): Tables {
  // Read only the exclusive synthetic fixture database; rows remain in process memory.
  return JSON.parse(execFileSync("uv", ["run", "python", "-I", "-c", `
import json, os, sqlite3
from pathlib import Path
root = Path(os.environ['SPM_E2E_CREDENTIAL_ROOT']).resolve(strict=True)
assert Path(os.environ['DATA_DIR']) == root / 'data'
with sqlite3.connect(f"file:{root / 'data' / 'portfolio.sqlite3'}?mode=ro", uri=True) as db:
    db.execute('PRAGMA query_only=ON')
    assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0030_direct_receipts',)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version' ORDER BY name")]
    assert len(tables) == 66
    print(json.dumps({name: [json.dumps(row, separators=(',', ':')) for row in db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid')] for name in tables}))
`], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] })) as Tables;
}
export async function headers(page: Page, version?: number) {
  const csrf = await (await page.request.get("/api/v1/auth/csrf")).json() as { csrf_token: string };
  return { Origin: origin, "X-CSRF-Token": csrf.csrf_token, "Idempotency-Key": randomUUID(), ...(version ? { "If-Match": `"${version}"` } : {}) };
}
export async function readEvent(page: Page, eventId: string) {
  const response = await page.request.get(`/api/v1/dividends/events/${eventId}`);
  expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
  return { data: await response.json() as Event, etag: response.headers().etag };
}
export async function seedCandidateSource(page: Page, securityId: string) {
  const reason = "Synthetic non-network source citation for candidate history.";
  const symbol = await page.request.post(`/api/v1/securities/${securityId}/provider-symbols`, { headers: await headers(page), data: {
    provider_id: "manual", provider_symbol: "CANDE2E.CITED", effective_from: "2020-01-01", reason,
  } });
  expect(symbol.status()).toBe(201);
  const policy = { provider_id: "manual", enabled: false, terms_acknowledged: true, terms_reference: "Synthetic local citation permission",
    terms_reason: reason, requests_per_minute: null, requests_per_day: null, raw_payload_storage: false };
  const profile = await page.request.post("/api/v1/provider-profiles", { headers: await headers(page), data: { policy, reason } });
  expect(profile.status()).toBe(201);
  const enable = await page.request.patch("/api/v1/provider-profiles/manual", { headers: await headers(page, 1), data: {
    policy: { ...policy, enabled: true }, clear_credential: false, reason,
  } });
  expect(enable.status()).toBe(200);
  const cited = await page.request.post("/api/v1/providers/cited-observations", { headers: await headers(page), data: {
    operation: "DIVIDENDS", provider_id: "manual", security_id: securityId, provider_symbol: "CANDE2E.CITED",
    ex_date: "2026-11-01", event_type: "SPECIAL", dps: "0", currency: "USD", adjustment_basis: "AS_DECLARED",
    citation: { citation: "Synthetic source reference", reason, url: "https://example.test/citation?private=synthetic-secret#page" },
  } });
  expect(cited.status()).toBe(201);
  const created = await cited.json() as components["schemas"]["CitedSourceCreated"];
  const response = await page.request.get(`/api/v1/dividends/observations?security_id=${securityId}`);
  expect(response.status()).toBe(200); expect(response.headers()["cache-control"]).toBe("no-store");
  const rows = await response.json() as { items: components["schemas"]["DividendObservationRecord"][] };
  const source = rows.items.find(row => row.id === created.observation_id)!;
  expect(source.normalized_sha256).toMatch(/^[a-f0-9]{64}$/);
  return { source, reason };
}
