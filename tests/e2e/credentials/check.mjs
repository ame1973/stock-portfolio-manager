import { spawn } from "node:child_process";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const tradeOrigin = process.argv[2] === "--trade-origin";
const tradeReview = process.argv[2] === "--trade-review";
const dividendEvents = process.argv[2] === "--dividend-events";
const dividendWithdrawal = process.argv[2] === "--dividend-withdrawal";
const dividendOverride = process.argv[2] === "--dividend-override";
const dividendCandidates = process.argv[2] === "--dividend-candidates";
const candidateWrite = process.argv[2] === "--candidate-write";
const candidateEventCreate = process.argv[2] === "--candidate-event-create";
const candidateEventMatch = process.argv[2] === "--candidate-event-match";
const candidateSourcePicker = process.argv[2] === "--candidate-source-picker";
const candidateBasisPicker = process.argv[2] === "--candidate-basis-picker";
const dividendEstimates = process.argv[2] === "--dividend-estimates";
const estimateWrites = process.argv[2] === "--estimate-writes";
const projectionHistory = process.argv[2] === "--projection-history";
const projectionWrite = process.argv[2] === "--projection-write";
if (process.argv.length > 3 || (process.argv.length === 3 && !tradeOrigin && !tradeReview && !dividendEvents && !dividendWithdrawal && !dividendOverride && !dividendCandidates && !candidateWrite && !candidateEventCreate && !candidateEventMatch && !candidateSourcePicker && !candidateBasisPicker && !dividendEstimates && !estimateWrites && !projectionHistory && !projectionWrite)) {
  console.error("FAIL isolated browser fixture (unknown mode)");
  process.exit(1);
}
if (process.env.PWDEBUG || process.env.DEBUG || process.env.NODE_DEBUG) {
  console.error("FAIL isolated browser fixture (debug logging must be unset)");
  process.exit(1);
}
const root = await mkdtemp(join(tmpdir(), "spm-credential-browser-"));
try {
  const socket = createServer();
  await new Promise((done, reject) => { socket.once("error", reject); socket.listen(0, "127.0.0.1", done); });
  const address = socket.address();
  if (!address || typeof address === "string") throw new Error("FIXTURE_PORT_UNAVAILABLE");
  await new Promise(done => socket.close(done));
  const cli = join(dirname(createRequire(import.meta.url).resolve("@playwright/test/package.json")), "cli.js");
  const result = await new Promise((done, reject) => {
    const config = tradeOrigin ? "tests/e2e/trade-origin/playwright.config.ts" : tradeReview ? "tests/e2e/trade-review/playwright.config.ts" : dividendEvents ? "tests/e2e/dividend-events/playwright.config.ts" : dividendWithdrawal ? "tests/e2e/dividend-events/withdrawal.playwright.config.ts" : dividendOverride ? "tests/e2e/dividend-events/override.playwright.config.ts" : dividendCandidates ? "tests/e2e/dividend-events/candidates.playwright.config.ts" : candidateWrite ? "tests/e2e/dividend-events/candidate-write.playwright.config.ts" : candidateEventCreate ? "tests/e2e/dividend-events/candidate-event-create.playwright.config.ts" : candidateEventMatch ? "tests/e2e/dividend-events/candidate-event-match.playwright.config.ts" : candidateSourcePicker ? "tests/e2e/dividend-events/candidate-source-picker.playwright.config.ts" : candidateBasisPicker ? "tests/e2e/dividend-events/candidate-basis-picker.playwright.config.ts" : dividendEstimates ? "tests/e2e/dividend-events/estimates.playwright.config.ts" : estimateWrites ? "tests/e2e/dividend-events/estimate-writes.playwright.config.ts" : projectionHistory ? "tests/e2e/dividend-events/projection-history.playwright.config.ts" : projectionWrite ? "tests/e2e/dividend-events/projection-write.playwright.config.ts" : "tests/e2e/credentials/playwright.config.ts";
    const child = spawn(process.execPath, [cli, "test", "--config", resolve(config)], {
      env: { ...process.env, APP_ENV: "test", PUBLIC_ORIGIN: `http://127.0.0.1:${address.port}`, SPM_E2E_BASE_URL: `http://127.0.0.1:${address.port}`,
        SPM_E2E_COMPOSE_PROJECT: "spm-e2e-credential-browser-fixture", SPM_E2E_CREDENTIAL_ROOT: root,
        DATA_DIR: join(root, "data"), BACKUP_DIR: join(root, "backups"), PROVIDER_SECRET_DIR: join(root, "operator"), MANAGED_PROVIDER_SECRET_DIR: join(root, "managed") },
      stdio: ["ignore", "pipe", "pipe"], timeout: 180_000,
    });
    const chunks = []; let length = 0;
    const capture = chunk => { length += chunk.length; if (length > 1_048_576) { child.kill(); reject(new Error("FIXTURE_OUTPUT_LIMIT")); } else chunks.push(chunk); };
    child.stdout.on("data", capture); child.stderr.on("data", capture); child.once("error", reject);
    child.once("close", (code, signal) => done({ code, signal, output: Buffer.concat(chunks).toString() }));
  });
  if (result.code !== 0 || result.signal || !result.output.includes("1 passed, 0 failed")) throw new Error("PRIVATE_BROWSER_FAILED");
  console.log(tradeOrigin
    ? "PASS isolated trade-origin browser fixture: actual recovery, exact wire integers, reauthentication and replay"
    : tradeReview ? "PASS isolated trade-review browser fixture: review, scope refresh, correction/void forms, reauthentication and replay"
      : dividendEvents ? "PASS isolated dividend-event browser fixture: histories, protected decisions, rejection races and unchanged retries"
        : dividendWithdrawal ? "PASS isolated dividend-withdrawal browser fixture: explicit resolution, historical replay and rejection race"
          : dividendOverride ? "PASS isolated dividend-override browser fixture: explicit confirmations, historical replay and rejection race"
            : dividendCandidates ? "PASS isolated dividend-candidate browser fixture: read-only versions, exact values and unknowns"
              : candidateWrite ? "PASS isolated candidate-write browser fixture: explicit sources, replay and stale revision"
                : candidateEventCreate ? "PASS isolated candidate-event browser fixture: distinct choice, replay and competing decisions"
                  : candidateEventMatch ? "PASS isolated candidate-event match browser fixture: explicit association, pinned rejection and competing decisions"
                    : candidateSourcePicker ? "PASS isolated candidate-source picker browser fixture: exact source selection, replay and security fence"
                      : candidateBasisPicker ? "PASS isolated candidate-basis picker browser fixture: explicit basis selection, replay and stale action fence"
                        : dividendEstimates ? "PASS isolated dividend-estimate browser fixture: exact scopes, unknowns and independent rejection"
                          : estimateWrites ? "PASS isolated estimate-write browser fixture: protected settings, historical replay and rejection race"
                            : projectionHistory ? "PASS isolated projection-history browser fixture: exact recorded values, missing inputs and no writes"
                              : projectionWrite ? "PASS isolated projection-write browser fixture: explicit pins, fence, historical replay and input races"
                              : "PASS isolated credential browser fixture: actual onboarding, reauthentication, import, rotation, clear, replay, exact private files and zero provider work");
} catch {
  // Only fixed stage labels are allowed; subprocess diagnostics and payloads remain private.
  const stages = new Set(["SETUP", "MASTER", "SEED", "NAVIGATE", "OPEN", "RESIZE", "RESIZE_MOBILE", "RESIZE_TABLET", "RESIZE_DESKTOP", "RESIZE_OPEN", "RESIZE_DETAIL", "RESIZE_SCROLL", "FILL", "PASSWORD", "PASSWORD_HELD", "COMMIT", "COMMIT_HELD", "LOST", "REAUTH", "REPLAY", "COMPARE", "SECOND", "REVIEW_ACK", "REVIEW_WITHDRAW", "CHANGED_EVIDENCE", "FINAL", "CORRECTION_REACK", "CORRECTION_REVISE", "CORRECTION_HELD", "CORRECTION_LOST", "CORRECTION_WITHDRAW", "CORRECTION_VOID", "ACCEPT_LOST", "ACCEPT_RATE_LIMITED", "ACCEPT_REAUTH"]);
  const stage = tradeOrigin || tradeReview || dividendEvents || dividendWithdrawal || dividendOverride || dividendCandidates || candidateWrite || candidateEventCreate || candidateEventMatch || candidateSourcePicker || candidateBasisPicker || dividendEstimates || estimateWrites || projectionHistory || projectionWrite ? await readFile(join(root, "stage"), "utf8").catch(() => "") : "";
  console.error(`FAIL isolated browser fixture${stages.has(stage) ? ` at ${stage}` : ""} (private diagnostics suppressed)`); process.exitCode = 1;
} finally {
  // Inspect only this newly generated temporary tree before removing it; no deployment volume is used.
  await readdir(root);
  await rm(root, { recursive: true, force: true });
}
