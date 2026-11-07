import { spawn } from "node:child_process";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { createServer } from "node:net";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

if (process.env.PWDEBUG || process.env.DEBUG || process.env.NODE_DEBUG) {
  console.error("FAIL credential browser fixture (debug logging must be unset)");
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
    const child = spawn(process.execPath, [cli, "test", "--config", resolve("tests/e2e/credentials/playwright.config.ts")], {
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
  if (result.code !== 0 || result.signal || !result.output.includes("1 passed, 0 failed")) throw new Error("CREDENTIAL_BROWSER_FAILED");
  console.log("PASS isolated credential browser fixture: actual onboarding, reauthentication, import, rotation, clear, replay, exact private files and zero provider work");
} catch {
  // Never forward captured subprocess diagnostics or error payloads from a credential-bearing flow.
  console.error("FAIL credential browser fixture (private diagnostics suppressed)"); process.exitCode = 1;
} finally {
  // Inspect only this newly generated temporary tree before removing it; no deployment volume is used.
  await readdir(root);
  await rm(root, { recursive: true, force: true });
}
