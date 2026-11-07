import { expect, test as unsafeControl } from "@playwright/test";
import { test as privateTest } from "../private-test";

// Only the isolated fake-server privacy control uses the unprotected fixture.
const test = process.env.SPM_E2E_PRIVACY_UNSAFE_CONTROL === "1" ? unsafeControl : privateTest;

test("synthetic assertion failure must not publish private values", async ({ page }) => {
  await page.goto("/");
  const values = await page.locator("input[data-private]").evaluateAll(elements =>
    elements.map(element => (element as HTMLInputElement).value));
  await page.request.post("/reached/assertion");
  console.log(values.join(" "));
  console.error(values.join(" "));
  expect(values).toEqual(["Deliberate public sentinel mismatch"]);
});

test("synthetic failed fill must not publish its call argument", async ({ page }) => {
  await page.goto("/");
  const value = await page.getByLabel("Provider credential").inputValue();
  await page.request.post("/reached/action");
  await page.getByLabel("Blocked input").fill(value, { timeout: 150 });
});
