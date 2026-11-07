import { defineConfig } from "@playwright/test";
import config from "../../../playwright.config";

const outputDir = process.env.SPM_E2E_PRIVACY_OUTPUT;
if (!outputDir) throw new Error("An isolated privacy output directory is required");

export default defineConfig(config, {
  testDir: import.meta.dirname,
  testMatch: "failure.fixture.ts",
  outputDir,
  reporter: process.env.SPM_E2E_PRIVACY_UNSAFE_CONTROL === "1" || process.env.SPM_E2E_PRIVACY_UNSAFE_REPORTER === "1" ? "list" : config.reporter,
  use: { ...config.use, trace: process.env.SPM_E2E_PRIVACY_UNSAFE_TRACE === "1" ? "on" : "off" },
  // Retain the deliberately failed synthetic cases to verify contents, not deletion as protection.
  preserveOutput: "always",
});
