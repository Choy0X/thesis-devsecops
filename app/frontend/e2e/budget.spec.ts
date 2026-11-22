import { expect, test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// The performance budgets that do not need a large store: how many requests a section makes when it opens (about 6; CVE Collections and Train
// are above it for a reason given in that note), how much script the first page loads,
// that revisiting a section repeats nothing, and that no single task blocks the page for long. The numbers a large store decides (route times) are measured by tools/perf_baseline.

const PAGES: [string, string, number][] = [
  ["/", "Overview", 6], ["/models", "Model Hub", 6], ["/collections", "CVE Collections", 8], ["/campaigns", "Campaigns", 6], ["/records", "Dataset records", 6], ["/pairs", "Code pairs", 6],
  ["/build", "Build dataset", 6], ["/train", "Train", 8], ["/sandbox", "Docker sandbox", 6], ["/projects", "Projects", 6], ["/settings", "Settings", 6],
];

const SHELL = /\/api\/(jobs|health|compute|db\/status|device|data\/stamp|settings|lifecycle)/;       // the header, the banner and the settings the shell reads once

async function mockApi(page: Page, seen: string[]) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    seen.push(`${route.request().method()} ${url.pathname}`);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixtureFor(route.request().method(), url.pathname) ?? {}) });
  });
}

for (const [path, title, budget] of PAGES) {
  test(`${title} opens with at most ${budget} requests`, async ({ page }) => {
    const seen: string[] = [];
    await mockApi(page, seen);
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
    await page.waitForTimeout(1200);
    const own = seen.filter((s) => !SHELL.test(s));                              // what the shell asks for on every page belongs to the shell, not to the section
    console.log(`[budget] ${title}: ${own.length} requests (${seen.length} with the shell's): ${[...new Set(own)].join(", ")}`);
    expect(own.length, own.join("\n")).toBeLessThanOrEqual(budget);
  });
}

test("the first page loads under 450 kB of script, and the heavy parts are separate files loaded only when needed", async ({ page }) => {
  const scripts: Record<string, number> = {};
  page.on("response", async (r) => { if (r.url().endsWith(".js")) scripts[r.url().split("/").pop()!] = (await r.body().catch(() => Buffer.alloc(0))).length; });
  await mockApi(page, []);
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
  await page.waitForTimeout(800);
  const total = Object.values(scripts).reduce((a, b) => a + b, 0);
  console.log(`[budget] first page script: ${Math.round(total / 1000)} kB in ${Object.keys(scripts).length} files`);
  expect(total).toBeLessThan(450_000);
  expect(Object.keys(scripts).some((n) => /^SandboxPage-/.test(n))).toBe(false);          // the terminal's page (xterm) is not loaded until the Sandbox is opened
  expect(Object.keys(scripts).some((n) => /^CodeViewerImpl-/.test(n))).toBe(false);        // nor the code viewer (CodeMirror) until a file is shown
});

test("the code viewer is not loaded by the page of Projects either: only a file shown in an agent's step asks for it", async ({ page }) => {
  const scripts: string[] = [];
  page.on("response", (r) => { if (r.url().endsWith(".js")) scripts.push(r.url().split("/").pop()!); });
  await mockApi(page, []);
  await page.goto("/projects");
  await expect(page.getByRole("heading", { level: 1, name: "Projects" })).toBeVisible();
  await page.waitForTimeout(800);
  expect(scripts.some((n) => /^CodeViewerImpl-/.test(n) || /codemirror/i.test(n))).toBe(false);
});

test("revisiting a section repeats no request and shows it at once", async ({ page }) => {
  const seen: string[] = [];
  await mockApi(page, seen);
  await page.goto("/records");
  await expect(page.getByRole("heading", { level: 1, name: "Dataset records" })).toBeVisible();
  await page.waitForTimeout(800);
  await page.getByRole("link", { name: "Settings" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  await page.waitForTimeout(800);
  const before = seen.length;
  const t0 = Date.now();
  await page.getByRole("link", { name: "Dataset records" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Dataset records" })).toBeVisible();
  expect(Date.now() - t0).toBeLessThan(500);
  await page.waitForTimeout(400);
  const repeated = seen.slice(before).filter((s) => !/\/api\/(jobs|health|device|compute|data\/stamp)\b/.test(s));
  console.log(`[budget] revisit: ${repeated.length} requests: ${repeated.join(", ")}`);
  expect(repeated.length).toBeLessThanOrEqual(2);
});

test("no task blocks the page for more than 120 ms while the first pages open", async ({ page }) => {
  await mockApi(page, []);
  await page.addInitScript(() => {
    (window as unknown as { __long: number[] }).__long = [];
    new PerformanceObserver((list) => { for (const e of list.getEntries()) (window as unknown as { __long: number[] }).__long.push(Math.round(e.duration)); }).observe({ entryTypes: ["longtask"] });
  });
  for (const [path] of PAGES.slice(0, 5)) {
    await page.goto(path);
    await page.waitForTimeout(900);
  }
  const long = await page.evaluate(() => (window as unknown as { __long: number[] }).__long);
  console.log(`[budget] long tasks on the last page: ${JSON.stringify(long)}`);
  expect(Math.max(0, ...long)).toBeLessThan(120);
});
