import { defineConfig } from "@playwright/test";

const project = process.env.SPM_E2E_COMPOSE_PROJECT ?? "";
const baseURL = process.env.SPM_E2E_BASE_URL ?? "http://localhost:18081";
const url = new URL(baseURL);
if (!/^spm-(auth-smoke|e2e-[a-z0-9-]+)$/.test(project)) {
  throw new Error("Set SPM_E2E_COMPOSE_PROJECT to an isolated spm-e2e-* Compose project");
}
if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname)) {
  throw new Error("Authentication E2E tests may run against loopback HTTP test instances only");
}

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  reporter: "list",
  use: {
    baseURL,
    browserName: "chromium",
    // Bootstrap and session secrets must never be persisted in browser traces or videos.
    trace: "off",
    screenshot: "off",
    video: "off",
  },
});
