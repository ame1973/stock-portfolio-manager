import { execFileSync } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { expect, test } from "@playwright/test";

const PASSWORD = "synthetic-browser-passphrase-2026";

test("owner onboarding, responsive views, logout revocation, and login", async ({ page, context }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "建立私人帳戶" })).toBeVisible();

  // Capture directly into test memory; never print or persist a live bootstrap credential.
  const output = execFileSync("docker", [
    "compose", "-p", process.env.SPM_E2E_COMPOSE_PROJECT!, "exec", "-T", "api",
    "spm-manage", "setup-token",
  ], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  const bootstrap = output.trim().split(": ").at(-1)!;
  await page.getByLabel(/初始化 token/).fill(bootstrap);
  await page.getByLabel(/設定密碼/).fill(PASSWORD);
  await page.getByLabel("再次輸入密碼").fill(PASSWORD);
  await page.getByRole("button", { name: "建立帳戶" }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();

  const session = (await context.cookies()).find(cookie => cookie.name === "spm_session")!;
  expect(session.httpOnly).toBe(true);
  expect(session.sameSite).toBe("Strict");
  expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0);

  await mkdir(".local/screenshots", { recursive: true });
  for (const width of [360, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByRole("button", { name: "登出" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const bounds = await page.getByRole("button", { name: "重新檢查" }).boundingBox();
    expect(bounds!.height).toBeGreaterThanOrEqual(44);
    await page.screenshot({ path: `.local/screenshots/authenticated-${width}.png`, fullPage: true });
  }
  await page.emulateMedia({ colorScheme: "dark" });
  await page.screenshot({ path: ".local/screenshots/authenticated-dark.png", fullPage: true });
  await page.getByRole("button", { name: "登出" }).click();
  await expect(page.getByRole("heading", { name: "登入持倉簿" })).toBeVisible();
  const replay = await page.request.get("/api/v1/auth/me", { headers: { Cookie: `spm_session=${session.value}` } });
  expect(replay.status()).toBe(401);

  await page.getByLabel("密碼", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登入", exact: true }).click();
  await expect(page.getByText("已連線，資料庫已就緒")).toBeVisible();
  expect(errors).toEqual([]);
  await page.getByRole("button", { name: "登出" }).click();
  await expect(page.getByRole("heading", { name: "登入持倉簿" })).toBeVisible();
});
