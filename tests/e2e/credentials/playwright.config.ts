import { fileURLToPath } from "node:url";
import { join } from "node:path";
import { defineConfig } from "@playwright/test";
import config from "../../../playwright.config";

const root = process.env.SPM_E2E_CREDENTIAL_ROOT;
if (!root || process.env.SPM_E2E_COMPOSE_PROJECT !== "spm-e2e-credential-browser-fixture") throw new Error("An isolated credential fixture root is required");
export default defineConfig(config, {
  testDir: import.meta.dirname,
  testMatch: "workflow.fixture.ts",
  outputDir: join(root, "browser-output"),
  timeout: 90_000,
  webServer: {
    command: "uv run python -I tests/e2e/credentials/server.py",
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    url: `${process.env.SPM_E2E_BASE_URL}/api/v1/health/ready`,
    reuseExistingServer: false,
    stdout: "ignore", stderr: "ignore",
    gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
  },
});
