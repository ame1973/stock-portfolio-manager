import { defineConfig } from "@playwright/test";
import config from "../credentials/playwright.config";

export default defineConfig(config, {
  testDir: import.meta.dirname,
  testMatch: "estimates.fixture.ts",
  timeout: 140_000,
});
