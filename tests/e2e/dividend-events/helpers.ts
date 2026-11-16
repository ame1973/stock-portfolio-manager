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
    assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0026_dividend_projection_fee',)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'alembic_version' ORDER BY name")]
    assert len(tables) == 63
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
