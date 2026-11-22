import { expect, test, type Page } from "@playwright/test";
import { fixtureFor, servingFixture } from "./fixtures";

// A structural accessibility check of every section and every tab, on the built app with the API answered by fixtures. It does not replace a person with a screen reader: it
// finds what a machine can find. Every control has an accessible name, no id is used twice, no tabindex is above 0, the headings go down one level at a time from a single h1,
// there is one main landmark and the document has a language, images have an alt attribute and tables are named. Contrast is measured separately (tools/contrast_table.py).

const PAGES: [string, string][] = [
  ["/", "Overview"], ["/models", "Model Hub"], ["/collections", "CVE Collections"], ["/campaigns", "Campaigns"], ["/records", "Dataset records"], ["/pairs", "Code pairs"],
  ["/build", "Build dataset"], ["/train", "Train"], ["/sandbox", "Docker sandbox"], ["/projects", "Projects"], ["/settings", "Settings"], ["/kit", "Component kit"],
];
const TABS: [string, string[]][] = [
  ["/pairs", ["pairs", "mining", "enrichment", "maintenance"]], ["/build", ["wizard", "versions", "other"]], ["/train", ["runs", "settings", "results", "evaluate"]],
  ["/sandbox", ["containers", "images", "history", "verification"]], ["/models", ["installed", "catalog", "serving"]], ["/settings", ["engines", "sources", "notify", "storage", "app", "developer"]], ["/projects", ["projects", "patch", "response"]],
];

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const hit = fixtureFor(route.request().method(), url.pathname);
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(hit ?? {}) });
  });
}

/** Runs in the page: the list of problems found, each as a sentence naming the element. */
function audit(): string[] {
  const out: string[] = [];
  const visible = (el: Element) => {
    for (let e: Element | null = el; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.display === "none" || s.visibility === "hidden" || e.getAttribute("aria-hidden") === "true" || (e as HTMLElement).hidden) return false;
    }
    return true;
  };
  const text = (el: Element): string => {
    let t = "";
    el.childNodes.forEach((n) => {
      if (n.nodeType === 3) t += n.textContent;
      else if (n.nodeType === 1 && (n as Element).getAttribute("aria-hidden") !== "true") {
        const c = n as Element;
        t += c.tagName === "IMG" ? c.getAttribute("alt") ?? "" : c.getAttribute("aria-label") ?? text(c);
      }
    });
    return t.replace(/\s+/g, " ").trim();
  };
  const name = (el: Element): string => {
    const label = el.getAttribute("aria-label");
    if (label?.trim()) return label.trim();
    const by = el.getAttribute("aria-labelledby");
    if (by) { const t = by.split(/\s+/).map((id) => document.getElementById(id)).filter(Boolean).map((n) => text(n!)).join(" ").trim(); if (t) return t; }
    const labels = (el as HTMLInputElement).labels;
    if (labels && labels.length) { const t = Array.from(labels).map(text).join(" ").trim(); if (t) return t; }
    if (el.tagName === "INPUT" && ["button", "submit", "reset"].includes((el as HTMLInputElement).type)) return (el as HTMLInputElement).value;
    const own = ["BUTTON", "A", "SUMMARY"].includes(el.tagName) || /^(button|link|tab|switch|menuitem|option|treeitem)$/.test(el.getAttribute("role") ?? "") ? text(el) : "";
    return own || el.getAttribute("title")?.trim() || "";
  };
  const describe = (el: Element) => `<${el.tagName.toLowerCase()}${el.getAttribute("role") ? ` role=${el.getAttribute("role")}` : ""}${(el as HTMLInputElement).type ? ` type=${(el as HTMLInputElement).type}` : ""}> "${(el.textContent ?? "").trim().slice(0, 30)}" in ${(el.closest("[aria-label],section,main,nav,header,aside") as HTMLElement | null)?.getAttribute("aria-label") ?? el.closest("section,main,nav,header,aside")?.tagName.toLowerCase() ?? "page"}`;

  const controls = document.querySelectorAll('button, a[href], input:not([type="hidden"]), select, textarea, summary, [role="button"], [role="link"], [role="tab"], [role="switch"], [role="checkbox"], [role="menuitem"], [role="combobox"]');
  controls.forEach((el) => { if (visible(el) && !name(el)) out.push(`no accessible name: ${describe(el)}`); });
  document.querySelectorAll("img").forEach((img) => { if (visible(img) && !img.hasAttribute("alt")) out.push(`image without alt: ${describe(img)}`); });
  const ids = new Map<string, number>();
  document.querySelectorAll("[id]").forEach((el) => ids.set(el.id, (ids.get(el.id) ?? 0) + 1));
  ids.forEach((n, id) => { if (n > 1) out.push(`id used ${n} times: ${id}`); });
  document.querySelectorAll("[tabindex]").forEach((el) => { if (Number(el.getAttribute("tabindex")) > 0) out.push(`tabindex above 0: ${describe(el)}`); });
  const heads = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6")).filter(visible).map((h) => Number(h.tagName[1]));
  if (heads.filter((l) => l === 1).length !== 1) out.push(`${heads.filter((l) => l === 1).length} h1 headings (one is expected)`);
  heads.forEach((l, i) => { if (i > 0 && l > heads[i - 1] + 1) out.push(`heading level jumps from h${heads[i - 1]} to h${l}`); });
  if (document.querySelectorAll("main").length !== 1) out.push(`${document.querySelectorAll("main").length} main landmarks (one is expected)`);
  const navs = Array.from(document.querySelectorAll("nav")).filter(visible);
  if (navs.length > 1) navs.forEach((n) => { if (!name(n) && !n.getAttribute("aria-label")) out.push("a nav landmark without a name while there are several"); });
  if (!document.documentElement.lang) out.push("the document has no language");
  document.querySelectorAll("table").forEach((t) => { if (visible(t) && !t.getAttribute("aria-label") && !t.getAttribute("aria-labelledby") && !t.querySelector("caption")) out.push(`table without a name: ${describe(t)}`); });
  return out;
}

async function check(page: Page, path: string) {
  await mockApi(page);
  await page.goto(path);
  await page.waitForTimeout(700);
  return page.evaluate(audit);
}

for (const [path, title] of PAGES) {
  test(`${title} has no structural accessibility problem a machine can find`, async ({ page }) => {
    expect(await check(page, path), `problems on ${path}`).toEqual([]);
  });
}

for (const [path, tabs] of TABS) {
  for (const tab of tabs) {
    test(`${path} tab ${tab} has no structural accessibility problem a machine can find`, async ({ page }) => {
      expect(await check(page, `${path}?tab=${tab}`), `problems on ${path}?tab=${tab}`).toEqual([]);
    });
  }
}

test("the first Tab reaches the way past the rail, and it moves focus to the page", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await page.waitForTimeout(500);
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to the page" })).toBeFocused();
  await expect(page.getByRole("link", { name: "Skip to the page" })).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.locator("#content")).toBeFocused();
});

test("every control of a page can be reached with the keyboard and shows where the focus is", async ({ page }) => {
  await mockApi(page);
  await page.goto("/projects");
  await page.waitForTimeout(700);
  const seen = new Set<string>();
  let invisible = 0;
  for (let i = 0; i < 40; i++) {
    await page.keyboard.press("Tab");
    const info = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      if (!el || el === document.body) return null;
      const s = getComputedStyle(el);
      const ring = s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0;
      return { key: `${el.tagName}:${el.getAttribute("aria-label") ?? el.textContent?.trim().slice(0, 20)}`, ring, tag: el.tagName };
    });
    if (!info) break;
    seen.add(info.key);
    if (!info.ring && !["INPUT", "SELECT", "TEXTAREA"].includes(info.tag)) invisible++;                 // a field shows its focus by its border
  }
  expect(seen.size).toBeGreaterThan(10);
  expect(invisible).toBe(0);
});

test("a project's page has no structural accessibility problem a machine can find", async ({ page }) => {
  await mockApi(page);
  await page.goto("/projects");
  await page.getByRole("button", { name: /The shop/ }).click();
  await expect(page.getByRole("button", { name: "All projects" })).toBeVisible();
  await page.waitForTimeout(700);
  expect(await page.evaluate(audit), "problems on a project's page").toEqual([]);
});

test("the Installed models list with models in it, its delete buttons and the question before deleting have no structural accessibility problem", async ({ page }) => {
  await mockApi(page);
  const row = (id: string, source: string, path: string, bytes: number) => ({ id, source, path, bytes, revisions: [], main_revision: null, safetensors: false, gguf: ["a.gguf"], weights: false, config: false });
  await page.route("**/api/models/local", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ hub_cache: "D:/m/hub", hub_cache_exists: true, models_dir: "D:/m", models_dir_exists: true,
    models: [row("org/small", "hub_cache", "D:/m/hub/models--org--small", 3_221_225_472), row("qwen:7b", "ollama", "C:/o/blobs/sha256-a", 1_073_741_824)], total_bytes: 4_294_967_296 }) }));
  await page.route("**/api/models/installed/remove-plan**", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ source: "ollama", id: "qwen:7b", path: "C:/o/blobs/sha256-a", kind: "ollama",
    removable: true, refusal: null, bytes: 1_073_741_824, files: 3, revisions: 0, kept_shared_files: 1, in_use: null, settings_use: ["the language model"], ollama_running: true, ollama_processes: ["ollama.exe"] }) }));
  await page.goto("/models");
  const button = page.getByRole("button", { name: "Delete qwen:7b" });
  await expect(button).toBeVisible();
  await page.waitForTimeout(700);
  expect(await page.evaluate(audit), "problems on the list").toEqual([]);
  await page.getByRole("button", { name: "Delete org/small" }).focus();
  const ring = await page.evaluate(() => { const s = getComputedStyle(document.activeElement as HTMLElement); return s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0; });
  expect(ring, "the delete button shows where the focus is").toBe(true);
  await button.click();
  const question = page.getByRole("alertdialog", { name: "Ollama is running" });
  await expect(question).toBeVisible();
  await expect(question, "the question is described, so a screen reader reads what will happen").toHaveAccessibleDescription(/keeps the files of its models open/);
  await expect(question.getByRole("button", { name: "Cancel" }), "the focus starts on the safe button, so Enter cannot delete").toBeFocused();
  await expect(question.getByRole("button", { name: "Stop Ollama and delete" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  await expect(button, "focus goes back to where it was").toBeFocused();
});

test("the download view of a model's dialog, with its panel, terminal and controls, has no structural accessibility problem", async ({ page }) => {
  await mockApi(page);
  const GB = 1024 ** 3;
  const fulfil = (route: import("@playwright/test").Route, body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  const job = { id: "dl0000000002", stage: "hf_download", status: "running", params: { repo_id: "org/big", quant: "" }, started: "2026-10-13T10:00:00Z", finished: "", error: "", log_count: 3, log_start: 0,
    logs: ["Model     org/big", "\u001b[32mPlan\u001b[0m      4 files, 14.0 GB", "bar 40%\r"],
    progress: { state: "downloading", label: "Downloading org/big", unit: "bytes", processed: 2 * GB, total: 14 * GB, rate: 100 * 1024 ** 2, eta: 125, files: 1, files_total: 4, current: "model.safetensors" } };
  await page.route("**/api/models/search**", (r) => fulfil(r, { query: "", task: "", models: [{ id: "org/big", downloads: 5, likes: 1, task: "text-generation", library: null, gated: false, parameters: 7_000_000_000, updated: null }] }));
  await page.route("**/api/models/download-plan**", (r) => fulfil(r, { repo_id: "org/big", revision: "d".repeat(40), gated: false, token_set: false, quants: [], needs_quant: false, files: 4, bytes: 14 * GB, free_bytes: 120 * GB, enough: true, partial_bytes: 0 }));
  await page.route("**/api/models/fit", (r) => fulfil(r, { mode: "inference", parts: { weights: 3 * GB, total: 3.95 * GB }, total: 3.95 * GB, notes: [], check: { gpu: "fits", ram: "fits", disk: "unknown", runs_on: "gpu", gpu_free_bytes: 6 * GB, ram_free_bytes: 12 * GB } }));
  await page.route("**/api/jobs/dl0000000002**", (r) => fulfil(r, job));
  await page.route("**/api/jobs", (r) => fulfil(r, r.request().method() === "POST" ? job : []));
  await page.goto("/models?tab=catalog");
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByRole("table", { name: "Models on the Hub" }).getByText("org/big").click();
  const dialog = page.getByRole("dialog", { name: "Does big fit?" });
  await dialog.getByRole("button", { name: "Download", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Download", exact: true }).click();
  await expect(dialog.getByRole("log", { name: "Output of Download a model" })).toContainText("Plan      4 files", { timeout: 10_000 });
  await page.waitForTimeout(700);
  const problems = (await page.evaluate(audit)).filter((p) => !p.startsWith("0 h1 headings"));      // while a dialog is open the page behind it is hidden from assistive technology, so its one h1 is not counted
  expect(problems, "problems in the download view").toEqual([]);
  await expect(dialog.getByRole("button", { name: "Copy the output of Download a model" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Follow" })).toHaveAttribute("aria-pressed", "true");
  await expect(dialog.getByRole("progressbar")).toHaveAttribute("aria-valuetext", /percent/);
});

test("the install view of a model's dialog (the choice, the plan, the steps) and the Finish install dialog have no structural accessibility problem", async ({ page }) => {
  await mockApi(page);
  const GB = 1024 ** 3;
  const fulfil = (route: import("@playwright/test").Route, body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  const plan = (kind: string) => ({ kind, repo_id: "org/small", revision: "d".repeat(40), role: "chat", role_detected: true, outtype: "f16", quant: "Q4_K_M", format: "Q4_K_M", format_reason: "Q4_K_M: 0.4 GB, the usual balance.", format_fit: "split",
    parameters: 494_000_000, parameters_estimated: false, architecture: "Qwen2ForCausalLM", supported: null,
    download: kind === "finish" ? null : { files: 3, bytes: GB, partial_bytes: 0, free_bytes: 120 * GB, enough: true, gated: false, token_set: false },
    convert: { intermediate_bytes: GB, final_bytes: 0.4 * GB, final_bytes_max: 0.4 * GB, peak_bytes: 1.4 * GB, fallback: false, seconds: 53, seconds_note: "An estimate: a model of 0.5 billion parameters took 53 s here." },
    scripts: { tag: "b11528", installed: false, installed_tag: null, will_fetch: true, blockers: [], note: "about 115 small files, under 2 MB, from github.com" },
    disk_needed_bytes: 2.5 * GB, problems: [], ready: true, notes: ["This repository also holds GGUF files (Q4_K_M)."] });
  const job = { id: "inst00000002", stage: "install_model", status: "running", params: { repo_id: "org/small", quant: "Q4_K_M", role: "chat" }, started: "2026-10-13T10:00:00Z", finished: "", error: "", log_count: 2, log_start: 0,
    logs: ["Model     org/small", "\u001b[32mPlan\u001b[0m      3 files, 1.0 GB"], progress: { state: "converting", label: "Converting org/small to Q4_K_M", unit: "percent", processed: 37, total: 100, step: 3, steps: 3, phase: "convert" } };
  await page.route("**/api/models/search**", (r) => fulfil(r, { query: "", task: "", models: [{ id: "org/small", downloads: 5, likes: 1, task: "text-generation", library: null, gated: false, parameters: 494_000_000, updated: null }] }));
  await page.route("**/api/models/download-plan**", (r) => fulfil(r, { repo_id: "org/small", revision: "d".repeat(40), gated: false, token_set: false, quants: [], needs_quant: false, files: 3, bytes: GB, free_bytes: 120 * GB, enough: true, partial_bytes: 0, patterns: ["*.safetensors"] }));
  await page.route("**/api/models/install-plan**", (r) => fulfil(r, plan("convert")));
  await page.route("**/api/models/finish-plan**", (r) => fulfil(r, plan("finish")));
  await page.route("**/api/models/fit", (r) => fulfil(r, { mode: "inference", parts: { weights: 0.4 * GB, total: GB }, total: GB, notes: [], check: { gpu: "fits", ram: "fits", disk: "unknown", runs_on: "gpu", gpu_free_bytes: 6 * GB, ram_free_bytes: 12 * GB } }));
  await page.route("**/api/models/local", (r) => fulfil(r, { hub_cache: "D:/m/hub", hub_cache_exists: true, models_dir: "D:/m", models_dir_exists: true,
    models: [{ id: "org/small", source: "hub_cache", path: "D:/m/hub/models--org--small", bytes: GB, revisions: [], main_revision: null, safetensors: true, gguf: [], weights: true, config: true }], total_bytes: GB }));
  await page.route("**/api/serving/models", (r) => fulfil(r, { files: [], not_ready: [{ id: "org/small", name: "small", source: "hub_cache", bytes: GB, role: "chat" }] }));
  await page.route("**/api/jobs/inst00000002**", (r) => fulfil(r, job));
  await page.route("**/api/jobs", (r) => fulfil(r, r.request().method() === "POST" ? job : []));
  const audited = async (what: string) => {
    await page.waitForTimeout(700);
    const problems = (await page.evaluate(audit)).filter((p) => !p.startsWith("0 h1 headings"));          // a dialog hides the page behind it, so its one h1 is not counted
    expect(problems, `problems in ${what}`).toEqual([]);
  };

  await page.goto("/models?tab=catalog");
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByRole("table", { name: "Models on the Hub" }).getByText("org/small").click();
  const dialog = page.getByRole("dialog", { name: "Does small fit?" });
  await expect(dialog.getByRole("radio", { name: /Install it ready to run/ })).toBeChecked();
  await expect(dialog.getByText("Will be set up as a language model.")).toBeVisible();                  // told, not asked
  await expect(dialog.getByRole("radiogroup", { name: "Use it as" })).toHaveCount(0);
  await audited("the install choice and plan");
  await dialog.getByRole("button", { name: "Change what it is used as" }).click();
  await expect(dialog.getByRole("radiogroup", { name: "Use it as" })).toBeVisible();
  await audited("the install plan with the role choice opened");
  await dialog.getByRole("button", { name: "Install", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Install", exact: true }).click();
  await expect(dialog.getByRole("log", { name: "Output of Install a model" })).toContainText("Plan      3 files", { timeout: 10_000 });
  await expect(dialog.getByRole("progressbar").first()).toHaveAttribute("aria-valuetext", /percent/);
  await audited("the steps of the install");
  await page.keyboard.press("Escape");

  await page.goto("/models");
  const table = page.getByRole("table", { name: "Installed models" });
  await expect(table.getByText("Not ready")).toBeVisible();
  await audited("the Installed models list with a model that is not ready");
  await table.getByRole("button", { name: "Finish the install of org/small" }).focus();
  const ring = await page.evaluate(() => { const s = getComputedStyle(document.activeElement as HTMLElement); return s.outlineStyle !== "none" && parseFloat(s.outlineWidth) > 0; });
  expect(ring, "the Finish install button shows where the focus is").toBe(true);
  await page.keyboard.press("Enter");
  const finish = page.getByRole("dialog", { name: "Finish installing small" });
  await expect(finish.getByRole("button", { name: "Convert it" })).toBeVisible();
  await audited("the Finish install dialog");
});

test("the chat of a project (its thread, code blocks, thinking, conversations and settings) has no structural accessibility problem", async ({ page }) => {
  await mockApi(page);
  const fulfil = (route: import("@playwright/test").Route, body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  const project = { id: "shop-aaaaaa", name: "Shop", folders: [{ path: "C:/code/shop", languages: { Python: 4 }, ecosystems: ["PyPI"], manifests: {}, files_seen: 4, truncated: false }], languages: { Python: 4 }, ecosystems: ["PyPI"],
    created_at: "2026-10-09T10:00:00Z", updated_at: "2026-10-09T10:00:00Z", authorization: { acknowledged: true, at: "2026-10-09T10:00:05Z", statement: "I own this code." }, last_scan: null };
  const reply = "The login is in **auth.py**.\n\n```python\ndef login(user):\n    return check(user)\n```\n\n| file | line |\n|---|---|\n| auth.py | 12 |\n\n- first\n- second\n\n# A heading the model wrote\n\nSee [the docs](https://example.org/docs).";
  const chat = { schema: "chat/1", id: "c00000000001", title: "Where is the login?", created_at: "2026-10-13T09:00:00.000Z", updated_at: "2026-10-13T10:00:00.000Z", params: { system: "", temperature: null, top_p: null, max_tokens: null },
    messages: [{ id: "u1", role: "user", content: "Where is the login?", created_at: "2026-10-13T09:00:00.000Z" },
      { id: "a1", role: "assistant", content: reply, created_at: "2026-10-13T09:00:01.000Z", model: "qwen2.5-coder-7b", thinking: "I should read auth.py first.", tools: [{ ok: true, summary: "Read auth.py lines 1 to 40" }, { ok: false, summary: "A file that is kept from the assistant" }],
        sources: [{ tool: "read_file", summary: "Read auth.py lines 1 to 40" }], notices: ["The oldest 4 messages were left out of what the model is sent."], usage: { prompt_tokens: 812, completion_tokens: 60, seconds: 1.6, tokens_per_second: 37.5 } }] };
  await page.route("**/api/projects", (r) => fulfil(r, { projects: [project], statement: "I own this code." }));
  await page.route("**/api/projects/shop-aaaaaa/chat/endpoint", (r) => fulfil(r, { configured: true, model: "qwen2.5-coder-7b", host: "127.0.0.1", local: true, provider: "managed" }));
  await page.route("**/api/projects/shop-aaaaaa/chats", (r) => fulfil(r, { chats: [{ id: chat.id, title: chat.title, created_at: chat.created_at, updated_at: chat.updated_at, message_count: 2, preview: "The login is in auth.py." }], damaged: 1 }));
  await page.route("**/api/projects/shop-aaaaaa/chats/c00000000001", (r) => fulfil(r, chat));
  const settled = async (what: string) => {
    await page.waitForTimeout(700);
    const problems = (await page.evaluate(audit)).filter((p) => !p.startsWith("0 h1 headings"));
    expect(problems, `problems in ${what}`).toEqual([]);
  };

  await page.goto("/projects?project=shop-aaaaaa&focus=chat");
  const thread = page.getByRole("list", { name: "The conversation" });
  await expect(page.getByRole("heading", { level: 1, name: "Chat about Shop" })).toBeVisible();
  await expect(thread.locator(".hljs-keyword").first()).toBeVisible();
  await expect(thread.getByRole("table")).toBeVisible();
  await expect(thread.locator("h1, h2, h3, h4, h5, h6")).toHaveCount(0);                      // a heading in an answer is a bold line, not a heading of the page
  await expect(page.getByRole("navigation", { name: "Conversations" })).toBeVisible();
  await expect(page.getByRole("region", { name: "This conversation's settings" })).toBeVisible();
  await expect(page.getByText("1 file of a conversation could not be read and is not listed.")).toBeVisible();
  await settled("the chat on its own page, the conversations and the settings of a conversation");
  await thread.getByText("Thinking", { exact: true }).click();
  await settled("thinking unfolded");
  await page.getByRole("button", { name: "Rename Where is the login?" }).click();
  await expect(page.getByRole("textbox", { name: "New name of Where is the login?" })).toBeFocused();
  await settled("a conversation being renamed");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Edit and send again" }).click();
  await expect(page.getByRole("textbox", { name: "Your question, edited" })).toBeFocused();
  await settled("a question being edited");
  await page.keyboard.press("Escape");
  const problems = await page.evaluate(audit);
  expect(problems, "problems in the chat with the whole window").toEqual([]);
});

test("the Serving tab with running Ollama and converted models has no structural accessibility problem", async ({ page }) => {
  await mockApi(page);
  const GB = 1024 ** 3;
  const fulfil = (route: import("@playwright/test").Route, body: unknown) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/serving/status", (r) => fulfil(r, servingFixture([{ name: "aivf/qwen:latest", context_length: 4096, size_vram: 3 * GB }])));
  await page.route("**/api/serving/derived", (r) => fulfil(r, { path: "D:/m/converted", budget_gb: 6, used_bytes: 8 * GB, entries: [
    { name: "a", path: "D:/m/converted/a", bytes: 4 * GB, files: 2, last_used: Date.now() / 1000 - 86400, used: true, pinned: true, label: "org/a, Q4_K_M", in_use: null, settings_roles: [], adapters: 0 },
    { name: "b", path: "D:/m/converted/b", bytes: 4 * GB, files: 2, last_used: 0, used: false, pinned: false, label: null, in_use: "the model gateway has it loaded", settings_roles: ["the language model"], adapters: 2 }] }));
  await page.goto("/models?tab=serving");
  await expect(page.getByRole("button", { name: "Unload aivf/qwen:latest" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Converted models" })).toBeVisible();
  await page.waitForTimeout(700);
  const problems = await page.evaluate(audit);
  expect(problems, "problems in the Serving tab").toEqual([]);
});
