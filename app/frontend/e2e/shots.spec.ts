import { test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// Screenshots of the sections for the papers. They run only when E2E_SHOTS names a folder (`E2E_SHOTS=../../figures/ui_rework npx playwright test e2e/shots.spec.ts`), and they show the fixtures
// of e2e/fixtures.ts, not results: the database is empty until the data is collected again, and a figure drawn from invented numbers must say so in its caption.

const OUT = process.env.E2E_SHOTS;
test.skip(!OUT, "set E2E_SHOTS to a folder to take the screenshots");
test.use({ viewport: { width: 1400, height: 900 } });

/** The window is a fixed-height column and only the page area scrolls, so a "full page" shot is taken by making the viewport as tall as the page area's content needs. */
async function shoot(page: Page, file: string) {
  const height = await page.evaluate(() => {
    const main = document.getElementById("content")!;
    return Math.ceil(window.innerHeight - main.clientHeight + main.scrollHeight);
  });
  await page.setViewportSize({ width: 1400, height: Math.min(Math.max(height, 900), 5000) });
  await page.waitForTimeout(200);
  await page.screenshot({ path: `${OUT}/${file}.png` });
}

const SHOTS: [string, string, string][] = [
  ["00-component-kit", "/kit", "Component kit"], ["01-overview", "/", "Overview"], ["02-model-hub-installed", "/models", "Model Hub"], ["03-model-hub-serving", "/models?tab=serving", "Model Hub"], ["04-cve-collections", "/collections", "CVE Collections"], ["04b-campaigns", "/campaigns", "Campaigns"],
  ["05-dataset-records", "/records", "Dataset records"], ["06-code-pairs", "/pairs", "Code pairs"], ["07-build-dataset", "/build", "Build dataset"], ["08-train-runs", "/train", "Train"],
  ["09-train-settings", "/train?tab=settings", "Train"], ["10-sandbox", "/sandbox", "Docker sandbox"], ["11-projects", "/projects", "Projects"], ["12-settings", "/settings", "Settings"],
];

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixtureFor(route.request().method(), url.pathname) ?? {}) });
  });
}

for (const [file, path, title] of SHOTS) {
  test(`screenshot ${file}`, async ({ page }) => {
    await mockApi(page);
    await page.goto(path);
    await page.getByRole("heading", { level: 1, name: title }).waitFor();
    await page.waitForTimeout(900);
    await shoot(page, file);
  });
}

test("screenshot 13-project-page", async ({ page }) => {
  await mockApi(page);
  await page.goto("/projects");
  await page.getByRole("button", { name: /The shop/ }).click();
  await page.getByRole("button", { name: "All projects" }).waitFor();
  await page.waitForTimeout(900);
  await shoot(page, "13-project-page");
});

test("screenshot 00b-kit-overlays", async ({ page }) => {
  await mockApi(page);
  await page.goto("/kit");
  await page.getByRole("heading", { level: 1, name: "Component kit" }).waitFor();
  await page.getByRole("combobox", { name: "Model (grouped)" }).scrollIntoViewIfNeeded();
  await page.getByRole("combobox", { name: "Model (grouped)" }).click();
  await page.getByRole("listbox").waitFor();
  await page.screenshot({ path: `${OUT}/00b-select-open.png` });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Ask (critical)" }).click();
  await page.getByRole("alertdialog").waitFor();
  await page.screenshot({ path: `${OUT}/00c-alert-critical.png` });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Open a dialog" }).click();
  await page.getByRole("dialog").waitFor();
  await page.screenshot({ path: `${OUT}/00d-dialog.png` });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Error toast" }).click();
  await page.getByRole("status").first().waitFor();
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}/00e-toast.png` });
});
