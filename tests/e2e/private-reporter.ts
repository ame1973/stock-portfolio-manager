import { relative } from "node:path";
import type { FullConfig, FullResult, Reporter, Suite, TestCase, TestResult } from "@playwright/test/reporter";

// Never serialize worker output, titles, steps, errors, sources or attachments.
export default class PrivateReporter implements Reporter {
  private passed = 0;
  private failed = 0;
  private skipped = 0;

  printsToStdio() { return true; }

  onBegin(_config: FullConfig, suite: Suite) {
    console.log(`Running ${suite.allTests().length} tests with private diagnostics`);
  }

  onTestEnd(test: TestCase, result: TestResult) {
    const status = result.status === "skipped" ? "SKIP" : result.status === test.expectedStatus ? "PASS" : "FAIL";
    if (status === "SKIP") this.skipped++;
    else if (status === "PASS") this.passed++;
    else this.failed++;
    console.log(`[${status}] ${relative(process.cwd(), test.location.file)}:${test.location.line}:${test.location.column}`);
  }

  onStdOut() { /* Worker output can contain request values; do not forward it. */ }
  onStdErr() { /* Worker output can contain request values; do not forward it. */ }
  onError() { this.failed++; console.log("[RUNNER_ERROR] Private diagnostics suppressed"); }

  onEnd(result: FullResult) {
    console.log(`${this.passed} passed, ${this.failed} failed, ${this.skipped} skipped (${result.status}; ${Math.ceil(result.duration / 1000)}s)`);
  }
}
