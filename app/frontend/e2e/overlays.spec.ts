import { expect, test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// The page behind a dialog or a drawer is dimmed. A class for it once produced no CSS, because the theme colours are variables, so nothing was dimmed; and a translucent layer over the window
// made the real window flicker on the graphics card, so the dimming is now a filter on the page root (components/hud/Scrim). This opens the overlays the app has and checks, from the painted
// pixels of the rail, that the dimming is really there; with E2E_SHOTS set it also saves a picture of each.

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixtureFor(route.request().method(), url.pathname) ?? {}) });
  });
  const hit = { osv_id: "GHSA-aaaa", cve_id: "CVE-2021-23337", ecosystem: "npm", packages: ["lodash"], more_packages: 0, summary: "Command injection in lodash", published: "2021-02-15T00:00:00Z", severity: "HIGH", aliases: [], state: "in_store" };
  await page.route("**/api/collections/cves?**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ query: "", ecosystem: "", state: "", total: 1, offset: 0, limit: 50, ecosystems_searched: ["npm"], downloaded_ecosystems: 1, unread: [], results: [hit] }) }));
  await page.route("**/api/collections/cves/npm/GHSA-aaaa", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
    ...hit, details: "Lodash versions before 4.17.21 are vulnerable to command injection.", modified: "2021-03-01T00:00:00Z", withdrawn: null, severity_label: "HIGH", cvss_v3: "CVSS:3.1/AV:N/AC:L/PR:H/UI:N/S:U/C:H/I:H/A:H", cvss_v4: null, cwe: ["CWE-77"],
    github_reviewed: true, nvd_published_at: null, affected: [{ package: "lodash", ecosystem: "npm", purl: null, ranges: [{ type: "SEMVER", repo: null, events: [{ introduced: "0" }, { fixed: "4.17.21" }] }], versions: ["4.17.20"], versions_more: 0 }],
    fix_commits: [], references: [{ type: "ADVISORY", url: "https://example.org/a" }], source_record_hash: "f".repeat(64), source: "dump", raw: { id: "GHSA-aaaa" } }) }));
}

/** The mean brightness (0 to 255) of the rail as painted, read from a screenshot: the rail is never under a dialog, so what changes it is the dimming and nothing else. */
async function railBrightness(page: Page): Promise<number> {
  const png = await page.screenshot({ clip: { x: 0, y: 60, width: 220, height: 420 } });
  return page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = img.width; canvas.height = img.height;
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(img, 0, 0);
    const px = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let sum = 0;
    for (let i = 0; i < px.length; i += 4) sum += (px[i] + px[i + 1] + px[i + 2]) / 3;
    return sum / (px.length / 4);
  }, png.toString("base64"));
}

/** How much of the brightness of the page is gone, 0 to 1, with a dialog open compared with `before`. */
async function dimming(page: Page, name: string, before: number): Promise<number> {
  if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/overlay-${name}.png`, fullPage: false });
  expect(await page.evaluate(() => document.documentElement.dataset.scrim), "a dialog marks the document").toBe("on");
  return 1 - (await railBrightness(page)) / before;
}

test("the Dataset records drawer dims the page behind it", async ({ page }) => {
  await mockApi(page);
  await page.goto("/records");
  await expect(page.getByText("CVE-2024-1001").first()).toBeVisible();
  const before = await railBrightness(page);
  await page.getByText("CVE-2024-1001").first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(await dimming(page, "records-drawer", before)).toBeGreaterThan(0.5);
});

test("the delete confirmation dims the page behind it", async ({ page }) => {
  await mockApi(page);
  await page.goto("/records");
  await expect(page.getByText("CVE-2024-1001").first()).toBeVisible();
  const before = await railBrightness(page);
  await page.getByText("CVE-2024-1001").first().click();
  await page.getByRole("button", { name: /^Delete/ }).first().click();
  await expect(page.getByRole("alertdialog")).toBeVisible();
  expect(await dimming(page, "delete-confirm", before)).toBeGreaterThan(0.5);
});

test("the advisory window and the dump dialog dim the page behind them", async ({ page }) => {
  await mockApi(page);
  await page.goto("/collections");
  await expect(page.getByRole("button", { name: "Open CVE-2021-23337 in npm" })).toBeVisible();
  const before = await railBrightness(page);
  await page.getByRole("button", { name: "Open CVE-2021-23337 in npm" }).click();
  await expect(page.getByRole("dialog", { name: "CVE-2021-23337" })).toBeVisible();
  await expect(page.getByText("Affected packages (1)")).toBeVisible();
  expect(await dimming(page, "advisory-window", before)).toBeGreaterThan(0.5);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => document.documentElement.dataset.scrim), "the mark goes when the last dialog closes").toBeUndefined();
  await page.getByRole("button", { name: /^(Check|Update) the npm dump$/ }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(await dimming(page, "dump-dialog", before)).toBeGreaterThan(0.5);
});

test("with the window drawn without the graphics card the translucent layer dims the page and no filter is used", async ({ page }) => {
  await mockApi(page);
  await page.goto("/records");
  await expect(page.getByText("CVE-2024-1001").first()).toBeVisible();
  await page.evaluate(() => { document.documentElement.dataset.motion = "reduced"; });          // what the app sets when the setting "graphics card for the window" is off
  const before = await railBrightness(page);
  await page.getByText("CVE-2024-1001").first().click();
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(await page.evaluate(() => getComputedStyle(document.getElementById("root")!).filter)).toBe("none");
  const amount = await dimming(page, "software-drawn", before);
  expect(amount).toBeGreaterThan(0.4);                    // the translucent layer blends towards the page colour, so it cannot go below about half the brightness of a dark page
});

test("a tall dialog stays inside the screen and scrolls its own content", async ({ page }) => {
  await mockApi(page);
  await page.goto("/collections");
  await page.getByRole("button", { name: "Open CVE-2021-23337 in npm" }).click();
  const dialog = page.getByRole("dialog", { name: "CVE-2021-23337" });
  await expect(page.getByText("Affected packages (1)")).toBeVisible();
  const box = (await dialog.boundingBox())!;
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(860);                                    // inside the window, not pushed past its edge
  const raw = dialog.getByText("Raw record (JSON)");
  await raw.scrollIntoViewIfNeeded();
  const after = (await dialog.boundingBox())!;
  const inner = (await raw.boundingBox())!;
  expect(inner.y + inner.height).toBeLessThanOrEqual(after.y + after.height + 1);        // the last line is inside the dialog's border once scrolled to
  if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/overlay-advisory-window-scrolled.png` });
});
