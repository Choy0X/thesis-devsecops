import { expect, test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// The layout of every page at the two widths the app is designed for (the narrow rail at 1024 px and the wide one at 1400 px), with the fixtures of e2e/fixtures.ts answering. A page
// passes when nothing makes it scroll sideways, nothing sticks out of the page area on the right unless it sits in a box that scrolls on its own (a table), no word is cut off by a box
// that clips, and no uncaught error is raised. It does not judge looks: E2E_SHOTS=<folder> also takes a screenshot of each page and width for looking at.

const PAGES: [string, string][] = [
  ["/", "Overview"], ["/models", "Model Hub"], ["/models?tab=serving", "Model Hub"], ["/collections", "CVE Collections"], ["/campaigns", "Campaigns"], ["/records", "Source records"],
  ["/pairs", "Collected data"], ["/build", "Build dataset"], ["/train", "Train"], ["/sandbox", "Docker sandbox"], ["/projects", "Projects"],
  ["/settings", "Settings"], ["/settings?tab=sources", "Settings"], ["/settings?tab=notify", "Settings"], ["/settings?tab=storage", "Settings"], ["/settings?tab=app", "Settings"],
  ["/settings?tab=developer", "Settings"], ["/settings?tab=skills", "Settings"], ["/kit", "Component kit"],
];
const SETTINGS_GROUPS = ["", "?tab=sources", "?tab=notify", "?tab=storage", "?tab=app", "?tab=developer", "?tab=skills"];
const WIDTHS = [1024, 1400];

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixtureFor(route.request().method(), url.pathname) ?? {}) });
  });
}

/** Elements that end to the right of the page area (or below nothing, they are scrolled vertically) and are not inside a box that scrolls or clips on its own. */
async function stickingOut(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const main = document.getElementById("content")!;
    const edge = main.getBoundingClientRect().right;
    const clips = (el: Element) => {
      for (let p: Element | null = el.parentElement; p && p !== main; p = p.parentElement) {
        const o = getComputedStyle(p).overflowX;
        if (o === "auto" || o === "scroll" || o === "hidden" || o === "clip") return true;
      }
      return false;
    };
    const out: string[] = [];
    for (const el of main.querySelectorAll("*")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const style = getComputedStyle(el);
      if (style.position === "fixed" || style.visibility === "hidden") continue;
      if (r.right > edge + 1 && !clips(el)) out.push(`${el.tagName.toLowerCase()}.${String(el.getAttribute("class") ?? "").split(" ").slice(0, 3).join(".")} right=${Math.round(r.right)} edge=${Math.round(edge)} "${(el.textContent ?? "").trim().slice(0, 40)}"`);
    }
    return out.slice(0, 8);
  });
}

/** Table headers that print over one another or over the edge of their cell (a table whose columns need more room than it has). */
async function crowdedHeaders(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    for (const row of document.querySelectorAll('[role="row"]')) {
      const heads = [...row.querySelectorAll(':scope > [role="columnheader"]')] as HTMLElement[];
      heads.forEach((el, i) => {
        const text = (el.textContent ?? "").trim();
        if (el.scrollWidth > el.clientWidth + 1) out.push(`header "${text}" is wider than its cell`);
        if (i > 0 && el.getBoundingClientRect().left < heads[i - 1].getBoundingClientRect().right - 1) out.push(`header "${text}" overlaps the one before it`);
      });
    }
    return out;
  });
}

/** Table cells whose content is taller than the row that holds them. A DataTable row has one fixed height (the rows are windowed), so a cell that wraps onto a second line is drawn over the
 *  rows above and below it instead of making its own row taller. */
async function cellsTallerThanRows(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    for (const row of document.querySelectorAll('[role="row"]')) {
      const rowBox = row.getBoundingClientRect();
      if (rowBox.height === 0) continue;
      for (const cell of row.querySelectorAll(':scope > [role="cell"]')) {
        const h = cell.getBoundingClientRect().height;
        if (h > rowBox.height + 1) out.push(`a cell of ${Math.round(h)} px in a row of ${Math.round(rowBox.height)} px: "${(cell.textContent ?? "").trim().slice(0, 40)}"`);
      }
    }
    return out.slice(0, 8);
  });
}

for (const width of WIDTHS) {
  // The models the owner had when the list was first seen to break: two safetensors models that are not ready to run (the cell holds the words and a button) and one that has no weights.
  test(`the Installed models list at ${width} px keeps the words and the button of a model that is not ready inside its row, with every column readable`, async ({ page }) => {
    await page.setViewportSize({ width, height: 860 });
    await mockApi(page);
    const GB = 1024 ** 3;
    const row = (id: string, bytes: number, safetensors: boolean, gguf = 0) => ({ id, source: "hub_cache", path: `D:/m/hub/models--${id.replace("/", "--")}`, bytes, revisions: [], main_revision: null, safetensors, gguf: Array.from({ length: gguf }, (_, i) => `m-${i}.gguf`), weights: safetensors || gguf > 0, config: true });
    await page.route("**/api/models/local", (r) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      hub_cache: "D:/m/hub", hub_cache_exists: true, models_dir: "D:/m", models_dir_exists: true, total_bytes: 15.2 * GB,
      models: [row("Qwen/Qwen2.5-Coder-7B-Instruct", 14.2 * GB, true), row("jinaai/jina-code-embeddings-0.5b", 0.9 * GB, true), row("Xenova/all-MiniLM-L6-v2", 0.3 * GB, false), row("org/both-formats", 31.7 * GB, true, 12)] }) }));
    await page.route("**/api/serving/models", (r) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ files: [], not_ready: [
      { id: "Qwen/Qwen2.5-Coder-7B-Instruct", name: "Qwen2.5-Coder-7B-Instruct", source: "hub_cache", bytes: 14.2 * GB, role: "chat" },
      { id: "jinaai/jina-code-embeddings-0.5b", name: "jina-code-embeddings-0.5b", source: "hub_cache", bytes: 0.9 * GB, role: "embedding" }] }) }));
    await page.goto("/models");
    const table = page.getByRole("table", { name: "Installed models" });
    await expect(table.getByText("Not ready")).toHaveCount(2);
    await expect(table.getByRole("button", { name: /Finish the install of Qwen/ })).toBeVisible();
    expect(await cellsTallerThanRows(page), "a cell is taller than its row").toEqual([]);
    // A button in a cell is whole and inside the cell's padding (the cell clips at its padding box, so a button that runs into the padding is not caught by scrollWidth).
    const intoPadding = await table.evaluate((el) => [...el.querySelectorAll('[role="cell"]')].flatMap((cell) => {
      const button = cell.querySelector("button");
      if (!button) return [];
      const edge = cell.getBoundingClientRect().right - parseFloat(getComputedStyle(cell).paddingRight);
      const over = button.getBoundingClientRect().right - edge;
      return over > 0.5 ? [`"${(button.textContent ?? "").trim()}" runs ${over.toFixed(1)} px into the padding`] : [];
    }));
    expect(intoPadding, "a button runs into the padding of its cell").toEqual([]);
    expect(await table.evaluate((el) => el.scrollWidth <= el.clientWidth), "the table fits its frame").toBe(true);
    // No cell of the row is cut short: the place a model was found in and the formats are read in full, and the button is whole.
    const clipped = await table.evaluate((el) => [...el.querySelectorAll('[role="cell"]')].filter((c) => c.scrollWidth > c.clientWidth + 1 && !c.querySelector(".font-mono")).map((c) => (c.textContent ?? "").trim()));
    expect(clipped, "text of a cell is cut off with an ellipsis").toEqual([]);
  });

  for (const [path, title] of PAGES) {
    test(`${path} at ${width} px does not scroll sideways or spill`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(`${e.name}: ${e.message}`));
      page.on("console", (m) => { if (m.type() === "error" && !/WebSocket|favicon|Failed to load resource/.test(m.text())) errors.push(m.text().slice(0, 300)); });
      await page.setViewportSize({ width, height: 860 });
      await mockApi(page);
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
      await page.waitForTimeout(900);
      if (process.env.E2E_SHOTS) await page.screenshot({ path: `${process.env.E2E_SHOTS}/layout-${width}-${path.replace(/\W+/g, "_")}.png` });
      const sideways = await page.evaluate(() => {
        const main = document.getElementById("content")!;
        return { page: document.documentElement.scrollWidth - window.innerWidth, area: main.scrollWidth - main.clientWidth };
      });
      expect(sideways, "the page scrolls sideways").toEqual({ page: 0, area: 0 });
      expect(await stickingOut(page), "elements stick out of the page area").toEqual([]);
      expect(await crowdedHeaders(page), "table headers crowd one another").toEqual([]);
      expect(await cellsTallerThanRows(page), "table cells are taller than their rows").toEqual([]);
      expect(errors).toEqual([]);
    });
  }
}

// The Settings page is judged on alignment, which is what makes it look professional or not: every card of a group is as wide as the column, every row has the same two columns so that the
// controls (and the values of the facts) of all the cards start at one vertical line, a card's action does not sit on its description, and exactly one entry of the menu is the current one.
async function settingsAlignment(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const problems: string[] = [];
    const main = document.getElementById("content")!;
    const box = (el: Element) => el.getBoundingClientRect();
    const visible = (el: Element) => { const r = box(el); return r.width > 0 && r.height > 0; };
    const cards = [...main.querySelectorAll("section.hud-frame")].filter(visible);
    if (cards.length === 0) problems.push("no card on the page");
    const edges = (pick: (r: DOMRect) => number) => [...new Set(cards.map((c) => Math.round(pick(box(c)))))];
    if (edges((r) => r.left).length > 1) problems.push(`the cards start at different places: ${edges((r) => r.left).join(", ")}`);
    if (edges((r) => r.right).length > 1) problems.push(`the cards end at different places: ${edges((r) => r.right).join(", ")}`);

    const rows = [...main.querySelectorAll('div[class*="minmax(0,5fr)"]')].filter(visible) as HTMLElement[];
    const columnLefts = new Set<number>(), nameLefts = new Set<number>();
    for (const row of rows) {
      const [name, control] = Array.from(row.children) as HTMLElement[];
      if (!name || !control) { problems.push("a row has fewer than two columns"); continue; }
      columnLefts.add(Math.round(box(control).left));
      nameLefts.add(Math.round(box(name).left));
      const first = control.querySelector('input:not([type="checkbox"]):not([type="radio"]), [role="combobox"], [role="radiogroup"], [role="switch"], button') as HTMLElement | null;
      if (first && Math.abs(box(first).left - box(control).left) > 1) problems.push(`a control does not start at its column: "${(name.textContent ?? "").trim().slice(0, 30)}" ${Math.round(box(first).left)} against ${Math.round(box(control).left)}`);
      if (Math.abs(box(name).right - box(control).left) < 8) problems.push(`the name and the control touch: "${(name.textContent ?? "").trim().slice(0, 30)}"`);
    }
    for (const dd of main.querySelectorAll('dl[class*="minmax(0,5fr)"] > dd')) if (visible(dd)) columnLefts.add(Math.round(box(dd).left));
    if (columnLefts.size > 1 && Math.max(...columnLefts) - Math.min(...columnLefts) > 1) problems.push(`the controls do not share a left edge: ${[...columnLefts].join(", ")}`);
    if (nameLefts.size > 1 && Math.max(...nameLefts) - Math.min(...nameLefts) > 1) problems.push(`the names do not share a left edge: ${[...nameLefts].join(", ")}`);

    for (const card of cards) {
      const header = card.querySelector(":scope > header");
      const parts = header ? (Array.from(header.children) as HTMLElement[]) : [];
      const description = header?.querySelector("p");
      const action = parts.length > 1 ? parts[parts.length - 1] : null;
      if (description && action && visible(description) && visible(action)) {
        const a = box(description), b = box(action);
        if (a.right > b.left && a.left < b.right && a.bottom > b.top && a.top < b.bottom) problems.push(`the action of "${(header!.textContent ?? "").trim().slice(0, 30)}" is on its description`);
      }
    }
    const current = document.querySelectorAll('nav[aria-label="Settings groups"] a[aria-current="page"]').length;
    if (current !== 1) problems.push(`${current} entries of the menu are the current one`);
    return problems;
  });
}

for (const width of WIDTHS) {
  for (const group of SETTINGS_GROUPS) {
    test(`the Settings group ${group || "?tab=engines"} at ${width} px: cards of one width, controls on one line`, async ({ page }) => {
      await page.setViewportSize({ width, height: 860 });
      await mockApi(page);
      await page.goto(`/settings${group}`);
      await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
      await expect(page.locator("section.hud-frame").first()).toBeVisible();
      await page.waitForTimeout(900);
      expect(await settingsAlignment(page), "the group is not aligned").toEqual([]);
    });
  }
}

// A check that cannot fail checks nothing: with one row pushed sideways and one card made narrower the same measurement reports both.
test("the alignment check of Settings sees a row that is out of line and a card that is narrower", async ({ page }) => {
  await page.setViewportSize({ width: 1400, height: 860 });
  await mockApi(page);
  await page.goto("/settings?tab=storage");
  await expect(page.locator("section.hud-frame").first()).toBeVisible();
  await page.waitForTimeout(900);
  expect(await settingsAlignment(page)).toEqual([]);
  await page.addStyleTag({ content: 'div[class*="minmax(0,5fr)"]:nth-of-type(1) > :last-child { margin-left: 13px; } section.hud-frame:nth-of-type(2) { width: 90%; }' });
  const problems = await settingsAlignment(page);
  expect(problems.some((p) => /share a left edge/.test(p)), problems.join(" | ")).toBe(true);
  expect(problems.some((p) => /end at different places/.test(p)), problems.join(" | ")).toBe(true);
});
