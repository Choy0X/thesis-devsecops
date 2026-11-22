import { expect, test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// The window shell in a real Chrome, with a stand-in for the Qt bridge so that the page draws its own title bar (as it does in the frameless desktop window): the page area is the only thing
// that scrolls, so the scrollbar never runs up beside the title bar, and the Close button turns solid critical red under the pointer.

async function openFrameless(page: Page, path: string) {
  await page.addInitScript(() => {
    const done = (cb: unknown, value?: unknown) => { if (typeof cb === "function") (cb as (v?: unknown) => void)(value); };
    (window as unknown as { __PYLOID__: unknown }).__PYLOID__ = {
      getFrame: (cb: unknown) => done(cb, false),
      isMaximized: (cb: unknown) => done(cb, false),
      minimize: (cb: unknown) => done(cb),
      toggleMaximize: (cb: unknown) => done(cb),
      close: (cb: unknown) => { (window as unknown as { __closed: boolean }).__closed = true; done(cb); },
    };
  });
  await page.route("**/api/**", async (route) => {
    const hit = fixtureFor(route.request().method(), new URL(route.request().url()).pathname);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(hit ?? {}) });
  });
  await page.goto(path);
  await expect(page.getByRole("banner", { name: "Window" })).toBeVisible();
}

const colourOf = (page: Page, css: string) =>
  page.evaluate((value) => { const probe = document.createElement("i"); probe.style.background = value; document.body.appendChild(probe); const c = getComputedStyle(probe).backgroundColor; probe.remove(); return c; }, css);

test("only the page area scrolls, below the title bar, and the scrollbar sits at the window's edge", async ({ page }) => {
  await openFrameless(page, "/settings");
  await page.waitForTimeout(500);
  const m = await page.evaluate(() => {
    const main = document.getElementById("content")!;
    const bar = document.querySelector('[role="banner"][aria-label="Window"]')!.getBoundingClientRect();
    const box = main.getBoundingClientRect();
    return {
      documentScrolls: document.scrollingElement!.scrollHeight > window.innerHeight + 1,
      mainScrolls: main.scrollHeight > main.clientHeight + 1,
      barBottom: Math.round(bar.bottom), mainTop: Math.round(box.top), mainRight: Math.round(box.right), width: window.innerWidth,
    };
  });
  expect(m.documentScrolls).toBe(false);          // the window itself does not scroll
  expect(m.mainScrolls).toBe(true);               // the page area does (Settings is long)
  expect(m.mainTop).toBeGreaterThanOrEqual(m.barBottom);
  expect(m.mainRight).toBe(m.width);
});

test("each page keeps its own scroll position", async ({ page }) => {
  await openFrameless(page, "/settings");
  await page.waitForTimeout(400);
  await page.evaluate(() => { document.getElementById("content")!.scrollTop = 300; });
  await page.getByRole("link", { name: "Overview" }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
  expect(await page.evaluate(() => document.getElementById("content")!.scrollTop)).toBe(0);
  await page.getByRole("link", { name: "Settings" }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.getElementById("content")!.scrollTop)).toBeGreaterThan(200);
});

test("the Close button is solid critical red on hover and while pressed, the others are not", async ({ page }) => {
  await openFrameless(page, "/");
  const crit = await colourOf(page, "var(--crit)");
  const close = page.getByRole("button", { name: "Close" });
  const minimise = page.getByRole("button", { name: "Minimise" });
  expect(await close.evaluate((e) => getComputedStyle(e).backgroundColor)).not.toBe(crit);
  await close.hover();
  await expect.poll(() => close.evaluate((e) => getComputedStyle(e).backgroundColor)).toBe(crit);
  await minimise.hover();
  expect(await minimise.evaluate((e) => getComputedStyle(e).backgroundColor)).not.toBe(crit);
  await close.hover();
  await page.mouse.down();
  await expect.poll(() => close.evaluate((e) => getComputedStyle(e).backgroundColor)).toBe(crit);       // the colour fades in over 120 ms
  await page.mouse.up();
});

test("the Close button reaches the shell", async ({ page }) => {
  await openFrameless(page, "/");
  await page.getByRole("button", { name: "Close" }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { __closed?: boolean }).__closed === true)).toBe(true);
});
