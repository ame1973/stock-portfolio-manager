import { defineConfig } from "@playwright/test";
import config from "../credentials/playwright.config";

export default defineConfig(config, {
  testDir: import.meta.dirname,
  testMatch: "projection-history.fixture.ts",
  timeout: 140_000,
});
