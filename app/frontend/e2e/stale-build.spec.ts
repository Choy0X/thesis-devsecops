import { expect, test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// A window that was opened before the app was rebuilt asks for page files that are gone. It must reload itself once and draw the page, never loop, and say what happened when a reload does
// not help. The missing file is simulated by failing the request for a page's file.

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixtureFor(route.request().method(), url.pathname) ?? {}) });
  });
}

test("a page file that is gone once makes the window reload itself once and then draw the page", async ({ page }) => {
  await mockApi(page);
  let seen = 0;
  await page.route(/\/assets\/RecordsPage-.*\.js$/, (route) => { seen += 1; return seen === 1 ? route.abort() : route.continue(); });
  await page.goto("/records");
  await expect(page.getByText("CVE-2024-1001").first()).toBeVisible({ timeout: 15_000 });      // the page itself; the shell draws its heading without the page file
  await expect(page.getByText("The app was updated while this window was open.")).toHaveCount(0);
  expect(seen).toBeGreaterThanOrEqual(2);
});

test("a page file that stays gone does not make the window reload forever; it says the app was updated", async ({ page }) => {
  await mockApi(page);
  let seen = 0;
  await page.route(/\/assets\/RecordsPage-.*\.js$/, (route) => { seen += 1; return route.abort(); });
  await page.goto("/records");
  await expect(page.getByText("The app was updated while this window was open.")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: "Reload the app" })).toBeVisible();
  const before = seen;
  await page.waitForTimeout(1500);
  expect(seen).toBe(before);                                                    // no further reload while it waited
  expect(seen).toBeLessThanOrEqual(3);                                          // the first try, the one reload, and at most a retry by the browser
});
