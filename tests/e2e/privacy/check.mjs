import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { createServer } from "node:http";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

if (process.env.PWDEBUG || process.env.DEBUG || process.env.NODE_DEBUG) {
  console.error("FAIL E2E_PRIVACY_GATE (debug logging must be unset)");
  process.exit(1);
}

const values = ["bootstrap", "password", "session", "provider"].map(kind => `synthetic-${kind}-${randomUUID()}`);
const reached = new Set();
let delivered = 0;
const root = await mkdtemp(join(tmpdir(), "spm-private-e2e-"));
const labels = ["Bootstrap credential", "Password", "Session credential", "Provider credential"];
const server = createServer((request, response) => {
  response.setHeader("Cache-Control", "no-store");
  if (request.url === "/") {
    delivered++;
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    response.setHeader("Set-Cookie", `spm_session=${values[2]}; HttpOnly; SameSite=Strict; Path=/`);
    response.end(`<html><body>${values.map((value, index) => `<input data-private aria-label="${labels[index]}" type="${index === 1 || index === 3 ? "password" : "text"}" value="${value}">`).join("")}<input disabled aria-label="Blocked input"></body></html>`);
  } else if (["/reached/assertion", "/reached/action"].includes(request.url)) {
    reached.add(request.url);
    response.end("ok");
  } else {
    response.statusCode = 404;
    response.end();
  }
});

async function artifacts(directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const file = join(directory, entry.name);
    if (entry.isDirectory()) result.push(...await artifacts(file));
    else if (entry.isFile()) result.push({ file, content: await readFile(file) });
    else throw new Error("UNEXPECTED_PRIVATE_ARTIFACT_TYPE");
  }
  return result;
}

try {
  await new Promise((done, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", done); });
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("PRIVATE_CHECK_SERVER_UNAVAILABLE");
  const cli = join(dirname(createRequire(import.meta.url).resolve("@playwright/test/package.json")), "cli.js");
  for (const mode of ["control", "protected", "reporter", "trace"]) {
    const unsafe = mode === "control";
    const blocked = mode === "reporter" || mode === "trace";
    reached.clear();
    delivered = 0;
    const output = join(root, mode);
    const result = await new Promise((done, reject) => {
      const child = spawn(process.execPath, [cli, "test", "--config", resolve("tests/e2e/privacy/playwright.config.ts")], {
        env: { ...process.env, SPM_E2E_COMPOSE_PROJECT: "spm-e2e-private-artifacts", SPM_E2E_BASE_URL: `http://127.0.0.1:${address.port}`, SPM_E2E_PRIVACY_OUTPUT: output,
          SPM_E2E_PRIVACY_UNSAFE_CONTROL: unsafe ? "1" : "0", SPM_E2E_PRIVACY_UNSAFE_REPORTER: mode === "reporter" ? "1" : "0",
          SPM_E2E_PRIVACY_UNSAFE_TRACE: mode === "trace" ? "1" : "0" },
        stdio: ["ignore", "pipe", "pipe"],
        timeout: 60_000,
      });
      const chunks = [];
      let length = 0;
      const capture = chunk => {
        length += chunk.length;
        if (length > 1_048_576) { child.kill(); reject(new Error("PRIVATE_CHECK_OUTPUT_LIMIT")); }
        else chunks.push(chunk);
      };
      child.stdout.on("data", capture);
      child.stderr.on("data", capture);
      child.once("error", reject);
      child.once("close", (code, signal) => done({ code, signal, output: Buffer.concat(chunks) }));
    });
    if (result.code !== 1 || result.signal || reached.size !== (blocked ? 0 : 2)
      || (blocked ? delivered !== 0 : delivered < 2) || !result.output.includes("2 failed")) {
      throw new Error("EXPECTED_PRIVATE_FAILURE_CASES_DID_NOT_RUN");
    }
    const files = await artifacts(output);
    const contexts = files.filter(file => file.file.endsWith("error-context.md"));
    if (!blocked && (contexts.length !== 2 || files.some(file => !file.file.endsWith("error-context.md") && !file.file.endsWith(".last-run.json")))) {
      throw new Error("UNEXPECTED_PRIVATE_ARTIFACT_SET");
    }
    const exposed = values.filter(value => result.output.includes(value) || files.some(file => file.content.includes(value)));
    if (unsafe ? exposed.length !== values.length : exposed.length !== 0) {
      throw new Error("PRIVATE_DIAGNOSTIC_CONTROL_OR_REDACTION_FAILED");
    }
    if (mode === "protected" && contexts.some(file => !file.content.includes("PRIVATE_E2E_FAILURE_DETAILS_SUPPRESSED")
      || file.content.includes("# Page snapshot") || file.content.includes("# Test source"))) {
      throw new Error("PRIVATE_CONTEXT_NOT_REDACTED_BEFORE_WRITE");
    }
  }
  console.log("PASS privacy gate: unsafe control detected; two protected failures are secret-free; reporter/trace overrides blocked before private data loads");
} catch {
  // Never print subprocess messages, stacks or captured output: a failing guard may contain secrets.
  console.error("FAIL E2E_PRIVACY_GATE (private diagnostics suppressed)");
  process.exitCode = 1;
} finally {
  await new Promise(done => server.close(done));
  // Only this process's inspected, generated temporary tree is removed.
  await rm(root, { recursive: true, force: true });
}
