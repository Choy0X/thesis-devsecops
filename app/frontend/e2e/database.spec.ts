import { expect, test } from "@playwright/test";
import { fixtureFor } from "./fixtures";

for (const width of [1360, 390]) {
  test(`embedded database is a local file with no daemon controls at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const mutations: string[] = [];
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      if (request.method() !== "GET") mutations.push(path);
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixtureFor(request.method(), path) ?? {}) });
    });
    await page.goto("/settings?tab=developer");
    await expect(page.getByRole("heading", { name: "Local database" })).toBeVisible();
    await expect(page.getByText("data/interim/database/aivf.sqlite3", { exact: true })).toBeVisible();
    await expect(page.getByText("SQLite 3.53.1", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Start the database" })).toHaveCount(0);
    expect(mutations).toEqual([]);
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.getByRole("heading", { name: "Local database" }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath(`database-${width}.png`), fullPage: true });
  });
}

test("saving the records folder sends only the setting and waits for a deliberate restart", async ({ page }) => {
  const writes: { path: string; body: unknown }[] = [];
  const original = fixtureFor("GET", "/api/settings") as Record<string, unknown>;
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let reply = fixtureFor(request.method(), path) ?? {};
    if (request.method() !== "GET") {
      const body = request.postDataJSON();
      writes.push({ path, body });
      if (path === "/api/settings") reply = { ...original, ...body };
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(reply) });
  });
  await page.goto("/settings?tab=storage");
  const panel = page.getByRole("region", { name: "Database records folder", exact: true });
  await panel.getByLabel("Database records folder", { exact: true }).fill("D:/records");
  await panel.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Database records folder saved. Restart the app to use it." })).toBeVisible();
  expect(writes).toEqual([{ path: "/api/settings", body: { database_dir: "D:/records" } }]);
});
