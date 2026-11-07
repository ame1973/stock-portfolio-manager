import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";

const version = (createRequire(import.meta.url)("@playwright/test/package.json") as { version: string }).version;
const reporter = fileURLToPath(new URL("./private-reporter.ts", import.meta.url));

export const test = base.extend<{ privateDiagnostics: void }>({
  privateDiagnostics: [async ({ page }, runFixture, info) => {
    // 1.64.1 checks NO_COPY_PROMPT before failure DOM capture; reverify on every upgrade.
    const options = info.project.use;
    if (version !== "1.64.1" || process.env.PLAYWRIGHT_NO_COPY_PROMPT !== "1"
      || options.trace !== "off" || options.video !== "off" || options.screenshot !== "off"
      || info.config.reporter.length !== 1 || info.config.reporter[0]?.[0] !== reporter
      || process.env.PWDEBUG || process.env.DEBUG || process.env.NODE_DEBUG || page.isClosed()) {
      throw new Error("PRIVATE_E2E_CONFIGURATION_REQUIRED");
    }
    try {
      await runFixture();
    } finally {
      // This automatic fixture depends on page: sanitize before page/context artifact teardown.
      for (const error of info.errors) {
        Object.assign(error, { message: "PRIVATE_E2E_FAILURE_DETAILS_SUPPRESSED", stack: undefined,
          snippet: undefined, value: undefined, errorContext: undefined, cause: undefined });
      }
    }
  }, { auto: true }],
});
