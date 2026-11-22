import { expect, test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// Every page of the built app is opened with the API answered by fixtures. A page passes when it shows its heading and raises no uncaught error. This does not
// judge layout; it proves that each page loads, renders its first state and survives a realistic set of answers.

const PAGES: [string, string][] = [
  ["/", "Overview"], ["/models", "Model Hub"], ["/collections", "CVE Collections"], ["/campaigns", "Campaigns"], ["/records", "Source records"], ["/pairs", "Collected data"],
  ["/build", "Build dataset"], ["/train", "Train"], ["/sandbox", "Docker sandbox"], ["/projects", "Projects"], ["/settings", "Settings"], ["/kit", "Component kit"],
];

async function mockApi(page: Page, unknown: string[]) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const hit = fixtureFor(route.request().method(), url.pathname);
    if (!hit) unknown.push(`${route.request().method()} ${url.pathname}`);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(hit ?? {}) });
  });
}

for (const [path, title] of PAGES) {
  test(`${title} loads and raises no error`, async ({ page }) => {
    const errors: string[] = [];
    const unknown: string[] = [];
    page.on("pageerror", (e) => errors.push(`${e.name}: ${e.message}`));
    page.on("console", (m) => { if (m.type() === "error" && !/WebSocket|favicon|Failed to load resource/.test(m.text())) errors.push(m.text().slice(0, 300)); });
    await mockApi(page, unknown);
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
    await page.waitForTimeout(600);                                   // let the first answers arrive and render
    if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/${title.replace(/\W+/g, "-")}.png`, fullPage: true });
    expect(errors, `errors on ${path}`).toEqual([]);
    if (unknown.length) console.log(`[${title}] requests with no fixture: ${[...new Set(unknown)].join(", ")}`);
  });
}

test("an address of the earlier layout leads to the section that holds the page", async ({ page }) => {
  const unknown: string[] = [];
  await mockApi(page, unknown);
  await page.goto("/legacy/review");
  await expect(page).toHaveURL(/\/pairs\?tab=context$/);
  await expect(page.getByRole("heading", { level: 1, name: "Collected data" })).toBeVisible();
});

test("an address nobody owns says so and offers the way home", async ({ page }) => {
  await mockApi(page, []);
  await page.goto("/nowhere");
  await expect(page.getByText("There is no page at this address")).toBeVisible();
  await page.getByRole("link", { name: "Go to the Overview" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
});

test("the rail leads from one section to the next and back keeps the page", async ({ page }) => {
  await mockApi(page, []);
  await page.goto("/");
  await page.getByRole("link", { name: "Collected data" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Collected data" })).toBeVisible();
  await page.getByRole("link", { name: "Build dataset" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Build dataset" })).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("heading", { level: 1, name: "Collected data" })).toBeVisible();
});

// The tabs of the pages are loaded only when opened, so each one is opened by its address.
const TABS: [string, string, string[]][] = [
  ["/pairs", "Collected data", ["pairs", "context", "maintenance"]],
  ["/build", "Build dataset", ["wizard", "versions"]],
  ["/sandbox", "Docker sandbox", ["containers", "images", "history", "verification"]],
  ["/models", "Model Hub", ["installed", "catalog", "serving"]],
  ["/settings", "Settings", ["settings", "developer"]],
];

for (const [path, title, tabs] of TABS) {
  for (const tab of tabs) {
    test(`${title}, tab ${tab} loads and raises no error`, async ({ page }) => {
      const errors: string[] = [];
      const unknown: string[] = [];
      page.on("pageerror", (e) => errors.push(`${e.name}: ${e.message}`));
      page.on("console", (m) => { if (m.type() === "error" && !/WebSocket|favicon|Failed to load resource/.test(m.text())) errors.push(m.text().slice(0, 300)); });
      await mockApi(page, unknown);
      await page.goto(`${path}?tab=${tab}`);
      await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
      await page.waitForTimeout(500);
      if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/${title.replace(/\W+/g, "-")}-${tab}.png`, fullPage: true });
      expect(errors, `errors on ${path}?tab=${tab}`).toEqual([]);
      if (unknown.length) console.log(`[${title} ${tab}] requests with no fixture: ${[...new Set(unknown)].join(", ")}`);
    });
  }
}

test("the Train wizard walks through its four steps without an error", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`${e.name}: ${e.message}`));
  await mockApi(page, []);
  await page.goto("/train");
  await expect(page.getByRole("heading", { name: "Prepare a run" })).toBeVisible();
  const shot = async (name: string) => { if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/Train-wizard-${name}.png`, fullPage: true }); };
  await shot("1-model");
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByRole("combobox", { name: "Dataset" })).toBeVisible();
  await shot("2-dataset");
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByRole("radio", { name: /As in the contract/ })).toBeVisible();
  await page.getByLabel("Adapter rank").fill("16");
  await page.waitForTimeout(500);
  await shot("3-settings");
  await page.getByRole("button", { name: "Next" }).click();
  await expect(page.getByRole("button", { name: "Prepare run" })).toBeVisible();
  await page.waitForTimeout(500);
  await shot("4-review");
  expect(errors).toEqual([]);
});

test("a prepared run offers training on this computer, with the memory verdict and the way to continue", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(`${e.name}: ${e.message}`));
  await mockApi(page, []);
  await page.goto("/train");
  await page.getByRole("button", { name: "Details" }).click();
  await expect(page.getByText("Cannot fit.")).toBeVisible();
  await expect(page.getByText(/Stopped at step 3 of 8 on its session budget/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Continue training" })).toBeDisabled();
  await page.getByRole("combobox", { name: "Model", exact: true }).click();
  await page.getByRole("option", { name: /software check/ }).click();
  await page.waitForTimeout(500);
  if (process.env.E2E_SHOTS) await page.locator("text=Train on this computer").first().scrollIntoViewIfNeeded();
  if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/Train-local.png`, fullPage: true });
  expect(errors).toEqual([]);
});
