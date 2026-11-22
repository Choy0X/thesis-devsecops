import { expect, test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// The Dataset records page stays light: the table holds only the short fields the server sends for a list row, the drawer opens at once from the row with a skeleton, and the
// whole record fills it in when it arrives. The record route is slowed here so that the skeleton is really on screen.

const SLOW_MS = 900;

async function mockApi(page: Page, seen: string[]) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    seen.push(`${route.request().method()} ${url.pathname}${url.search}`);
    if (/^\/api\/dataset\/records\/\d+$/.test(url.pathname)) await new Promise((r) => setTimeout(r, SLOW_MS));
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixtureFor(route.request().method(), url.pathname) ?? {}) });
  });
}

test("a record opens at once from its row, with a skeleton until the whole record arrives", async ({ page }) => {
  const seen: string[] = [];
  await mockApi(page, seen);
  await page.goto("/records");
  await page.getByText("CVE-2024-1001").first().click();
  const drawer = page.getByRole("dialog", { name: "CVE-2024-1001" });
  await expect(drawer).toBeVisible({ timeout: SLOW_MS - 300 });                      // before the slow answer: the drawer does not wait for it
  await expect(drawer.getByText("Prototype pollution")).toBeVisible();                 // the summary the row already had
  await expect(drawer.getByRole("status", { name: "Loading the whole record" })).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Edit" })).toBeDisabled();
  await expect(drawer.getByRole("status", { name: "Loading the whole record" })).toHaveCount(0, { timeout: SLOW_MS + 3000 });
  await expect(drawer.getByRole("button", { name: "Edit" })).toBeEnabled();
});

test("the list asks for the tiny view, sorts on the server and reads counts for the filters", async ({ page }) => {
  const seen: string[] = [];
  await mockApi(page, seen);
  await page.goto("/records");
  await expect(page.getByRole("table", { name: "Dataset records" })).toBeVisible();
  await page.getByRole("columnheader", { name: "Published" }).getByRole("button").click();
  await expect.poll(() => seen.some((s) => s.includes("/api/dataset/records?") && s.includes("view=list") && s.includes("sort=published") && s.includes("dir=asc"))).toBe(true);
  expect(seen.some((s) => s.includes("/api/dataset/facets"))).toBe(true);
  expect(seen.some((s) => s.includes("/api/dataset/summary"))).toBe(false);          // the filters are counted by the facets, not by the whole-store summary
  await page.getByRole("combobox", { name: "Severity" }).click();
  await expect(page.getByRole("option", { name: "High (1)" })).toBeVisible();
});
