import { defineConfig } from "@playwright/test";
import config from "../credentials/playwright.config";

export default defineConfig(config, {
  testDir: import.meta.dirname,
  testMatch: "withdrawal.fixture.ts",
  timeout: 120_000,
});
