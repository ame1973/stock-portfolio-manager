import { defineConfig } from "@playwright/test";
import config from "../credentials/playwright.config";

export default defineConfig(config, {
  testDir: import.meta.dirname,
  testMatch: "estimate-writes.fixture.ts",
  timeout: 160_000,
});
