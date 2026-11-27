import { defineConfig } from "@playwright/test";
import config from "../credentials/playwright.config";

export default defineConfig(config, {
  testDir: import.meta.dirname,
  testMatch: "evidence-filters.fixture.ts",
  timeout: 100_000,
});
