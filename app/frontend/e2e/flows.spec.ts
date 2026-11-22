import { expect, test, type Page, type Route } from "@playwright/test";
import { fixtureFor, servingFixture } from "./fixtures";

// Whole flows of a few sections on the built app, with the API answered by fixtures and the answers a flow depends on written here. They go through the page the way a person does
// (roles and names, no test ids), and they check what is sent to the backend, which is where a broken form or a missing confirmation shows.

type Handler = (route: Route, body: any) => Promise<boolean> | boolean;

async function mockApi(page: Page, handlers: Record<string, Handler>, sent: { method: string; path: string; body: any }[] = []) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    let body: any = null;
    try { body = request.postData() ? JSON.parse(request.postData()!) : null; } catch { body = request.postData(); }
    if (request.method() !== "GET") sent.push({ method: request.method(), path: url.pathname, body });
    const handler = handlers[`${request.method()} ${url.pathname}`];
    if (handler && (await handler(route, body))) return;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixtureFor(request.method(), url.pathname) ?? {}) });
  });
  return sent;
}

const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) }).then(() => true);

const STATEMENT = "I own this code, or I am authorised to test it. The app may read it and copy it into a sandbox to analyse it.";
const project = (over: Record<string, unknown> = {}) => ({
  id: "new-aaaaaa", name: "New one", folders: [{ path: "C:/code/new", languages: { Python: 4 }, ecosystems: ["PyPI"], manifests: { PyPI: ["requirements.txt"] }, files_seen: 4, truncated: false }],
  languages: { Python: 4 }, ecosystems: ["PyPI"], created_at: "2026-10-09T10:00:00Z", updated_at: "2026-10-09T10:00:00Z",
  authorization: { acknowledged: true, at: "2026-10-09T10:00:05Z", statement: STATEMENT }, last_scan: null, ...over,
});

test("a project is added only after the folder is looked at and the confirmation is ticked, and its page offers chat, scans and updates", async ({ page }) => {
  let created = false;
  const sent = await mockApi(page, {
    "GET /api/projects": (r) => json(r, { projects: created ? [project()] : [], statement: STATEMENT }),
    "GET /api/projects/look": (r) => json(r, { ok: true, path: "C:/code/new", languages: { Python: 4 }, ecosystems: ["PyPI"], manifests: { PyPI: ["requirements.txt"] }, files_seen: 4, truncated: false }),
    "POST /api/projects": (r) => { created = true; return json(r, project()); },
    "GET /api/projects/new-aaaaaa": (r) => json(r, project()),
  });
  await page.goto("/projects");
  await expect(page.getByText("No project yet")).toBeVisible();
  await page.getByRole("button", { name: /Add a project/ }).click();
  const dialog = page.getByRole("dialog", { name: "Add a project" });
  const add = dialog.getByRole("button", { name: "Add the project" });
  await expect(add).toBeDisabled();
  await dialog.getByLabel("Project name").fill("New one");
  await dialog.getByLabel("Source folder").fill("C:/code/new");
  await expect(dialog.getByText(/Python \(4\); packages: PyPI\./)).toBeVisible();
  await expect(add).toBeDisabled();                                                              // looked at, but not confirmed
  await dialog.getByLabel("I own this code or am authorised to test it").check();
  await expect(add).toBeEnabled();
  await add.click();
  await expect(page.getByText(/that you own the code or may test it\./)).toBeVisible();
  const post = sent.find((s) => s.method === "POST" && s.path === "/api/projects");
  expect(post?.body).toEqual({ name: "New one", folders: ["C:/code/new"], authorized: true });
  for (const title of ["Chat", "Scan dependencies", "Scan codebase", "Audit log"]) await expect(page.getByText(title, { exact: true }).first()).toBeVisible();      // the three actions, and the log
  await expect(page.getByRole("heading", { name: "Scan dependencies" })).toBeVisible();                                                                            // dependencies is the default view
  await page.getByText("Scan dependencies", { exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Scan dependencies" })).toBeVisible();
  await expect(page.getByText("Dependencies and code check", { exact: true })).toBeVisible();
  await page.getByText("Dependencies and code check", { exact: true }).click();
  await expect(page.getByRole("heading", { name: "Update dependencies" })).toBeVisible();
  await page.getByText("Scan codebase", { exact: true }).first().click();
  await expect(page.getByRole("heading", { name: "Scan code" })).toBeVisible();
  await page.getByText("Full: in the sandbox", { exact: true }).click();
  await expect(page.getByRole("heading", { name: "Scan codebase in the sandbox" })).toBeVisible();
  await expect(page.getByLabel("Start command")).toBeVisible();
});

test("a project's page shows its search index as Stale with the files that changed, and offers Update", async ({ page }) => {
  await mockApi(page, {
    "GET /api/projects": (r) => json(r, { projects: [project()], statement: STATEMENT }),
    "GET /api/projects/new-aaaaaa": (r) => json(r, project()),
    "GET /api/projects/new-aaaaaa/index": (r) => json(r, { enabled: true, built: true, tracked: true, files: 4, chunks: 12, indexed_at: 1_790_000_000, stale: false, embedder: { model: "jina-code-embeddings-0.5b-Q8_0.gguf", key: "k" } }),
    "GET /api/projects/new-aaaaaa/index/changes": (r) => json(r, { built: true, tracked: true, checked: 5, read: 2, touched: 0, new: 1, changed: 1, removed: 0, total: 2, examples: { new: ["src/more.py"], changed: ["src/app.py"], removed: [] } }),
  });
  await page.goto("/projects");
  await page.getByRole("button", { name: /New one/ }).click();
  const card = page.getByLabel("Search index of the project");
  await expect(card.getByRole("status")).toHaveText("Stale: 2 files changed");
  await expect(card.getByRole("list", { name: "Files that changed" })).toContainText("src/app.py");
  await expect(card.getByRole("button", { name: "Update" })).toBeEnabled();
  await expect(card.getByRole("button", { name: "Delete" })).toBeVisible();
});

test("a project takes one folder, is named from it without typing, and a typed name is kept", async ({ page }) => {
  const sent = await mockApi(page, {
    "GET /api/projects": (r) => json(r, { projects: [], statement: STATEMENT }),
    "GET /api/projects/look": (r) => json(r, { ok: true, path: "C:/code/acme-shop", languages: { TypeScript: 9 }, ecosystems: ["npm"], manifests: { npm: ["package.json"] }, files_seen: 9, truncated: false,
      suggested_name: "Acme Shop", name_source: "package.json" }),
    "POST /api/projects": (r, body) => json(r, project({ name: body.name || "Acme Shop" })),
    "GET /api/projects/new-aaaaaa": (r) => json(r, project({ name: "Acme Shop" })),
  });
  await page.goto("/projects");
  await page.getByRole("button", { name: /Add a project/ }).click();
  const dialog = page.getByRole("dialog", { name: "Add a project" });
  await expect(dialog.getByRole("button", { name: "Add another folder" })).toHaveCount(0);
  await expect(dialog.getByLabel("Source folder")).toHaveCount(1);
  await dialog.getByLabel("Source folder").fill("C:/code/acme-shop");
  await expect(dialog.getByLabel("Project name")).toHaveValue("Acme Shop");
  await expect(dialog.getByText("Suggested from package.json.")).toBeVisible();
  await dialog.getByLabel("Project name").fill("My own name");
  await dialog.getByLabel("Source folder").fill("C:/code/acme-shop2");                       // the folder changes; the typed name stays
  await expect(dialog.getByLabel("Project name")).toHaveValue("My own name");
  await dialog.getByLabel("I own this code or am authorised to test it").check();
  await dialog.getByRole("button", { name: "Add the project" }).click();
  await expect.poll(() => sent.find((s) => s.method === "POST" && s.path === "/api/projects")?.body).toEqual({ name: "My own name", folders: ["C:/code/acme-shop2"], authorized: true });
});

test("a project a folder names itself: the name field may stay empty", async ({ page }) => {
  const sent = await mockApi(page, {
    "GET /api/projects": (r) => json(r, { projects: [], statement: STATEMENT }),
    "GET /api/projects/look": (r) => json(r, { ok: true, path: "C:/code/acme-shop", languages: { TypeScript: 9 }, ecosystems: ["npm"], manifests: {}, files_seen: 9, truncated: false, suggested_name: "Acme Shop", name_source: "package.json" }),
    "POST /api/projects": (r) => json(r, project({ name: "Acme Shop" })),
    "GET /api/projects/new-aaaaaa": (r) => json(r, project({ name: "Acme Shop" })),
  });
  await page.goto("/projects");
  await page.getByRole("button", { name: /Add a project/ }).click();
  const dialog = page.getByRole("dialog", { name: "Add a project" });
  await dialog.getByLabel("Source folder").fill("C:/code/acme-shop");
  await expect(dialog.getByLabel("Project name")).toHaveValue("Acme Shop");
  await dialog.getByLabel("Project name").fill("");
  await dialog.getByLabel("I own this code or am authorised to test it").check();
  await expect(dialog.getByRole("button", { name: "Add the project" })).toBeEnabled();
  await dialog.getByRole("button", { name: "Add the project" }).click();
  await expect.poll(() => sent.find((s) => s.method === "POST" && s.path === "/api/projects")?.body).toEqual({ name: "", folders: ["C:/code/acme-shop"], authorized: true });
});

test("a project cannot be added with a folder the server refuses, and the reason is shown", async ({ page }) => {
  await mockApi(page, {
    "GET /api/projects": (r) => json(r, { projects: [], statement: STATEMENT }),
    "GET /api/projects/look": (r) => json(r, { ok: false, reason: "C:/nope does not exist, or cannot be read." }),
  });
  await page.goto("/projects");
  await page.getByRole("button", { name: /Add a project/ }).click();
  const dialog = page.getByRole("dialog", { name: "Add a project" });
  await dialog.getByLabel("Source folder").fill("C:/nope");
  await expect(dialog.getByText("C:/nope does not exist, or cannot be read.")).toBeVisible();
});

test("a setting is saved by its own button and the page says so", async ({ page }) => {
  const sent = await mockApi(page, {});
  await page.goto("/settings");
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  await page.getByRole("navigation", { name: "Settings groups" }).getByRole("link", { name: "Storage and compute" }).click();          // the menu leads to the group
  await expect(page).toHaveURL(/tab=storage/);
  const jobs = page.getByRole("combobox", { name: "Jobs at once" });
  await jobs.scrollIntoViewIfNeeded();
  await jobs.click();
  await page.getByRole("option", { name: "2", exact: true }).click();
  const panel = jobs.locator("xpath=ancestor::*[.//button[normalize-space(.)='Save']][1]");               // the nearest block around the field that has a Save button of its own
  await panel.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => sent.find((s) => s.method === "PUT" && s.path === "/api/settings")?.body).toMatchObject({ max_jobs: "2" });
});

test("the eye of a saved key shows the key, hides it again, and a save sends nothing for a key that was only looked at", async ({ page }) => {
  let settings: Record<string, unknown> = { ...(fixtureFor("GET", "/api/settings") as object), nvd_api_key_set: true, nvd_api_key_hint: "a1b2" };
  const sent = await mockApi(page, {
    "GET /api/settings": (r) => json(r, settings),
    "PUT /api/settings": (r, body) => { settings = { ...settings, ...body }; return json(r, settings); },
    "POST /api/settings/secret": (r, body) => json(r, body?.name === "nvd_api_key" ? { value: "nvd-0123456789a1b2" } : { value: "" }),
  });
  await page.goto("/settings?tab=sources");
  const panel = page.locator("section").filter({ has: page.getByRole("heading", { name: "NVD" }) }).last();
  const field = panel.getByLabel("API key", { exact: true });
  await expect(field).toHaveValue("");
  await expect(panel.getByText("saved...a1b2")).toBeVisible();
  expect(sent.some((s) => s.path === "/api/settings/secret")).toBe(false);                      // nothing is read until the eye is pressed
  await panel.getByRole("button", { name: "Show API key" }).click();
  await expect(field).toHaveValue("nvd-0123456789a1b2");
  await expect(field).toHaveAttribute("type", "text");
  expect(sent.filter((s) => s.path === "/api/settings/secret").map((s) => s.body)).toEqual([{ name: "nvd_api_key" }]);
  await panel.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => sent.find((s) => s.method === "PUT" && s.path === "/api/settings")?.body).toEqual({});
  await panel.getByRole("button", { name: "Hide API key" }).click();
  await expect(field).toHaveValue("");
  await expect(field).toHaveAttribute("type", "password");
});

test("Projects and tools opens with legacy embedding settings and no embedding service", async ({ page }) => {
  const embeddingRequests: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (/^\/api\/(?:rag\/|embed(?:dings)?$)|^\/v1\/embeddings$/.test(path)) embeddingRequests.push(path);
  });
  await mockApi(page, {
    "GET /api/settings": (r) => json(r, { ...(fixtureFor("GET", "/api/settings") as object), rag_enabled: "off", embed_ollama_model: "old-embedder:latest" }),
  });
  await page.goto("/settings?tab=engines");
  await expect(page.getByRole("heading", { name: "Command tools" })).toBeVisible();
  await expect(page.getByText(/embedding model|projects agent retrieval|old-embedder/i)).toHaveCount(0);
  expect(embeddingRequests).toEqual([]);
});

test("the Language model panel chooses a model of this computer, and the Notifications group saves its switch", async ({ page }) => {
  let settings: Record<string, unknown> = { ...(fixtureFor("GET", "/api/settings") as object), llm_engine: "local", llm_base_url: "" };
  const sent = await mockApi(page, {
    "GET /api/settings": (r) => json(r, settings),
    "PUT /api/settings": (r, body) => { settings = { ...settings, ...body }; return json(r, settings); },
  });
  await page.goto("/settings");
  const panel = page.locator("section").filter({ has: page.getByRole("heading", { name: "Language model" }) }).last();          // the card, not the page around it
  await expect(panel.getByRole("radio", { name: "Local" })).toBeChecked();
  await panel.getByRole("combobox", { name: "Model" }).click();
  await page.getByRole("option", { name: /model-q4\.gguf/ }).click();
  await expect.poll(() => sent.find((s) => s.method === "PUT" && s.body?.llm_local_model)?.body).toMatchObject({ llm_local_model: "D:/m/model-q4.gguf" });
  await page.getByRole("navigation", { name: "Settings groups" }).getByRole("link", { name: "Notifications" }).click();
  const notify = page.getByRole("switch", { name: "Tell me when a job finishes or stops" });
  await expect(notify).toBeChecked();
  await notify.click();
  await expect.poll(() => sent.find((s) => s.method === "PUT" && s.body?.desktop_notify !== undefined)?.body).toMatchObject({ desktop_notify: "off" });
  await expect(page.getByText("Set up the email server above to be told by email when a job ends.")).toBeVisible();
});

test("the Hard reset dialog lists the sizes, keeps the models and the keys unticked, and needs the word before it can delete", async ({ page }) => {
  const cat = (id: string, label: string, bytes: number, over: Record<string, unknown> = {}) => ({ id, label, note: `Note of ${label}.`, default: true, warning: null, present: true, bytes, files: 4, paths: [], blocked: null, databases: [], ...over });
  const sent = await mockApi(page, {
    "GET /api/reset/inventory": (r) => json(r, { running: false, done: true, token: "tok-1", taken: 1, progress: { category: "", files: 0, bytes: 0 }, error: null, root: "D:/p", confirm_word: "RESET",
      docker_note: "The databases live in a volume of the Docker container. The volume file does not get smaller.",
      categories: [cat("raw", "Downloaded sources", 2_500_000_000), cat("models", "Models", 5_100_000_000, { default: false, warning: "Not ticked: every model goes." }), cat("settings", "Settings and keys", 150, { default: false, warning: "Not ticked: every key goes." })] }),
    "POST /api/reset/start": (r) => json(r, { running: true, done: false, started: 1, steps: [], ok: null, error: null, pending: 0, categories: ["raw"] }),
    "GET /api/reset/status": (r) => json(r, { running: false, done: true, started: 1, steps: [{ id: "raw", label: "Downloaded sources", ok: true, detail: "5 items removed" }], ok: true, error: null, pending: 0, categories: ["raw"] }),
  });
  await page.goto("/settings?tab=app");
  const open = page.getByRole("button", { name: "Hard reset..." });
  await open.click();
  const dialog = page.getByRole("dialog", { name: "Hard reset" });
  await expect(dialog.getByLabel("Downloaded sources: size")).toHaveText(/2\.3 GB/);
  await expect(dialog.getByRole("checkbox", { name: "Downloaded sources" })).toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Models" })).not.toBeChecked();
  await expect(dialog.getByRole("checkbox", { name: "Settings and keys" })).not.toBeChecked();
  const del = dialog.getByRole("button", { name: "Delete what is chosen" });
  await expect(del).toBeDisabled();
  await dialog.getByLabel(/Type RESET to continue/).fill("RESET");
  await expect(del).toBeEnabled();
  await del.click();
  await expect(dialog.getByText(/Done\. The app is empty now\./)).toBeVisible();
  await expect.poll(() => sent.find((s) => s.path === "/api/reset/start")?.body).toEqual({ token: "tok-1", categories: ["raw"], confirm: "RESET", backup: false });
});

test("Settings refreshes Ollama status and lists a newly downloaded model", async ({ page }) => {
  let running = false, finished = false, haveModel = false;
  const status = () => ({ ...servingFixture(), ollama: { running, version: running ? "0.35.0" : null, error: null } });
  const file = { id: "org/m", name: "fresh-model-q4_k_m.gguf", path: "D:/m/fresh-model-q4_k_m.gguf", bytes: 400_000_000, source: "folder", info: { architecture: "qwen2", name: "x", quantisation: "Q4_K_M", context_length: 32768, layers: 24, parameters: 494_000_000 } };
  const downloadJob = () => ({ id: "install000001", stage: "hf_download", status: finished ? "done" : "running", params: {}, started: "2026-10-12T10:00:00Z", finished: finished ? "2026-10-12T10:01:00Z" : "", error: "", log_count: 0, progress: null });
  await mockApi(page, {
    "GET /api/settings": (r) => json(r, { ...(fixtureFor("GET", "/api/settings") as object), llm_engine: "local", llm_base_url: "" }),
    "GET /api/serving/status": (r) => json(r, status()),
    "GET /api/serving/models": (r) => json(r, { files: haveModel ? [file] : [] }),
    "GET /api/serving/adapters": (r) => json(r, { adapters: [] }),
    "GET /api/jobs": (r) => json(r, [downloadJob()]),
  });
  await page.goto("/settings");
  await expect(page.getByText(/Ollama is not running/).first()).toBeVisible();
  running = true; haveModel = true; finished = true;
  await expect(page.getByText(/Ollama is not running/)).toHaveCount(0, { timeout: 20_000 });
  const panel = page.locator("section").filter({ has: page.getByRole("heading", { name: "Language model" }) }).last();          // the card, not the page around it
  await panel.getByRole("combobox", { name: "Model" }).click();
  await expect(page.getByRole("option", { name: /fresh-model-q4_k_m/ })).toBeVisible();
});

test("the Model Hub Serving tab shows and unloads a running Ollama model", async ({ page }) => {
  const runner = { name: "aivf/qwen:latest", size_vram: 2 * 1024 ** 3, context_length: 4096 };
  let running = [runner];
  const sent = await mockApi(page, {
    "GET /api/serving/status": (r) => json(r, servingFixture(running)),
    "POST /api/serving/unload": (r) => {
      running = [];
      return json(r, { unloaded: [runner.name], status: servingFixture() });
    },
  });
  await page.goto("/models?tab=serving");
  await expect(page.getByRole("heading", { name: "Ollama 0.35.0" })).toBeVisible();
  await page.getByRole("button", { name: "Unload aivf/qwen:latest" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Unload", exact: true }).click();
  await expect.poll(() => sent.find((item) => item.path === "/api/serving/unload")?.body).toEqual({ model: "aivf/qwen:latest" });
  await expect(page.getByText("No models are running")).toBeVisible();
});

test("the Installed models list deletes a model only after asking with its size, and asks about a running Ollama before stopping it", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 800 });
  const row = (id: string, source: string, path: string, bytes: number) => ({ id, source, path, bytes, revisions: [], main_revision: null, safetensors: false, gguf: ["a.gguf"], weights: false, config: false });
  let listed = [row("org/small", "hub_cache", "D:/m/hub/models--org--small", 3_221_225_472), row("qwen:7b", "ollama", "C:/o/blobs/sha256-a", 1_073_741_824)];
  const plan = (r: Route) => {
    const id = new URL(r.request().url()).searchParams.get("id");
    return json(r, id === "qwen:7b"
      ? { source: "ollama", id, path: "C:/o/blobs/sha256-a", kind: "ollama", removable: true, refusal: null, bytes: 1_073_741_824, files: 3, revisions: 0, kept_shared_files: 1, in_use: null, settings_use: [], ollama_running: true, ollama_processes: ["ollama app.exe", "ollama.exe"] }
      : { source: "hub_cache", id, path: "D:/m/hub/models--org--small", kind: "cache", removable: true, refusal: null, bytes: 3_221_225_472, files: 12, revisions: 1, kept_shared_files: 0, in_use: null, settings_use: [], ollama_running: false, ollama_processes: [] });
  };
  const deletes: URLSearchParams[] = [];
  await mockApi(page, {
    "GET /api/models/local": (r) => json(r, { hub_cache: "D:/m/hub", hub_cache_exists: true, models_dir: "D:/m", models_dir_exists: true, models: listed, total_bytes: 4_294_967_296 }),
    "GET /api/models/installed/remove-plan": plan,
    "DELETE /api/models/installed": (r) => {
      const q = new URL(r.request().url()).searchParams;
      deletes.push(q);
      listed = listed.filter((m) => m.id !== q.get("id"));
      return json(r, { source: q.get("source"), id: q.get("id"), freed_bytes: q.get("id") === "qwen:7b" ? 1_073_741_824 : 3_221_225_472, files: 3, stopped: q.get("stop_ollama") === "true" ? ["ollama app.exe", "ollama.exe"] : [], left_behind: [] });
    },
  });
  await page.goto("/models");
  const table = page.getByRole("table", { name: "Installed models" });
  await expect(table.getByText("org/small")).toBeVisible();
  expect(await table.evaluate((el) => el.scrollWidth <= el.clientWidth), "the table with its delete column fits its frame at 1024 px").toBe(true);
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await table.getByRole("button", { name: "Delete org/small" }).click();
  const ask = page.getByRole("alertdialog");
  await expect(ask).toContainText("Delete org/small?");
  await expect(ask).toContainText("Deletes 3.0 GB (12 files, 1 version) from the Hugging Face cache");
  await expect(page.getByRole("dialog")).toHaveCount(0);                       // the fit check did not open behind it
  await ask.getByRole("button", { name: "Cancel" }).click();
  await expect(ask).toBeHidden();
  expect(deletes).toEqual([]);

  await table.getByRole("button", { name: "Delete org/small" }).click();
  await ask.getByRole("button", { name: "Delete 3.0 GB" }).click();
  await expect(page.getByText("org/small deleted; 3.0 GB freed.")).toBeVisible();
  await expect(table.getByText("org/small")).toHaveCount(0);
  expect(deletes.map((q) => [q.get("source"), q.get("id"), q.get("path"), q.get("stop_ollama")])).toEqual([["hub_cache", "org/small", "D:/m/hub/models--org--small", "false"]]);

  await table.getByRole("button", { name: "Delete qwen:7b" }).click();
  await expect(ask).toContainText("Ollama is running (ollama app.exe and ollama.exe)");
  await ask.getByRole("button", { name: "Cancel" }).click();
  await expect(ask).toBeHidden();
  expect(deletes.length).toBe(1);
  await table.getByRole("button", { name: "Delete qwen:7b" }).click();
  await ask.getByRole("button", { name: "Stop Ollama and delete" }).click();
  await expect(page.getByText(/qwen:7b deleted; 1\.0 GB freed\. Ollama was stopped/)).toBeVisible();
  expect(deletes.map((q) => q.get("stop_ollama"))).toEqual(["false", "true"]);
});

test("closing the dialog of a destructive action keeps everything", async ({ page }) => {
  await mockApi(page, {
    "GET /api/projects": (r) => json(r, { projects: [project()], statement: STATEMENT }),
  });
  const sent: { method: string }[] = [];
  page.on("request", (rq) => { if (rq.method() === "DELETE") sent.push({ method: "DELETE" }); });
  await page.goto("/projects");
  await page.getByRole("button", { name: /New one/ }).click();
  await page.getByRole("button", { name: "Remove", exact: true }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("Its source folders are not touched");
  await dialog.getByRole("button", { name: "Keep it" }).click();
  await expect(dialog).toBeHidden();
  expect(sent).toEqual([]);
  await expect(page.getByRole("heading", { name: "New one" })).toBeVisible();
});

test("a sandbox container's page shows its limits and files, and creating it again asks first and says what is lost", async ({ page }) => {
  const container = { id: "c1", name: "aivdf-demo", status: "running", image: "python:3.11-slim", created: "2026-10-10T00:00:00Z", network: "none" };
  const policy = { cpus: 1, memory_mb: 512, swap_mb: 512, pids: 256, network: "none", read_only: false, editable: true, reason: null,
    bounds: { max_cpus: 8, max_memory_mb: 8192, min_cpus: 0.25, min_memory_mb: 64, min_pids: 16, max_pids: 4096 }, warnings: ["The root filesystem is writable: a program can change the container's system files (not your computer's)."] };
  const sent = await mockApi(page, {
    "GET /api/sandbox/status": (r) => json(r, { docker_available: true, version: "29.0", hardening: [] }),
    "GET /api/sandbox/containers": (r) => json(r, [container]),
    "GET /api/sandbox/containers/c1/policy": (r) => json(r, policy),
    "GET /api/sandbox/containers/c1/files": (r) => json(r, { path: "/work", parent: "/", truncated: false, entries: [{ name: "a.txt", kind: "file", size: 5, mode: "-rw-r--r--", modified: 1760000200, link: null }] }),
    "POST /api/sandbox/containers/c1/recreate": (r) => json(r, { ...policy, network: "bridge", id: "c2", name: "aivdf-demo", status: "running", warnings: ["This container can now reach the network."] }),
  });
  await page.goto("/sandbox");
  await page.getByRole("button", { name: /demo/ }).click();
  await expect(page.getByText("The root filesystem is writable", { exact: false })).toBeVisible();
  await expect(page.getByRole("link", { name: "Download" })).toHaveAttribute("href", /\/api\/sandbox\/containers\/c1\/files\/download\?path=%2Fwork%2Fa\.txt/);
  await page.getByLabel("Network on").check();
  await page.getByRole("button", { name: "Create it again with these settings" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("Programs that are running and the open terminal are lost");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  expect(sent.filter((s) => s.path.endsWith("/recreate"))).toEqual([]);
  await page.getByRole("button", { name: "Create it again with these settings" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Create it again", exact: true }).click();
  await expect(page.getByRole("status", { name: "After the container was created again" })).toContainText("This container can now reach the network.");
  expect(sent.find((s) => s.path.endsWith("/recreate"))?.body).toEqual({ network: true });
});

test("a model download shows the transfer panel and the terminal in the dialog, within the window, and follows the job to its end", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  const GB = 1024 ** 3;
  let phase: "running" | "done" = "running";
  const logs = ["Model     org/big", "Revision  dddddddddd (pinned)", "Plan      4 files, 14.0 GB; 120.0 GB free on the disk that holds the cache", "  model-00001-of-00002.safetensors   9.0 GB", "  model-00002-of-00002.safetensors   5.0 GB"];
  const job = () => ({ id: "dl0000000001", stage: "hf_download", status: phase, params: { repo_id: "org/big", quant: "" }, started: "2026-10-13T10:00:00Z", finished: phase === "done" ? "2026-10-13T10:02:00Z" : "", error: "", log_count: logs.length,
    logs: phase === "done" ? [...logs, "Finished  14.0 GB in 2:00 (119.4 MB/s), saved to D:/m/hub/models--org--big"] : logs, log_start: 0,
    progress: phase === "done" ? { state: "done", label: "org/big is downloaded", unit: "bytes", processed: 14 * GB, total: 14 * GB, files: 4, files_total: 4, seconds: 120 }
      : { state: "downloading", label: "Downloading org/big", unit: "bytes", processed: 2 * GB, total: 14 * GB, rate: 100 * 1024 ** 2, eta: 125, files: 1, files_total: 4, current: "model-00001-of-00002.safetensors" } });
  const started: unknown[] = [];
  await mockApi(page, {
    "GET /api/models/search": (r) => json(r, { query: "", task: "", models: [{ id: "org/big", downloads: 5, likes: 1, task: "text-generation", library: null, gated: false, parameters: 7_000_000_000, updated: null }] }),
    "GET /api/models/download-plan": (r) => json(r, { repo_id: "org/big", revision: "d".repeat(40), gated: false, token_set: false, quants: [], needs_quant: false, files: 4, bytes: 14 * GB, free_bytes: 120 * GB, enough: true, partial_bytes: 0 }),
    "POST /api/models/fit": (r) => json(r, { mode: "inference", parts: { weights: 3 * GB, kv_cache: 0.2 * GB, overhead: 0.75 * GB, total: 3.95 * GB }, total: 3.95 * GB, notes: [],
      check: { gpu: "fits", ram: "fits", disk: "unknown", runs_on: "gpu", gpu_free_bytes: 6 * GB, ram_free_bytes: 12 * GB } }),
    "GET /api/jobs": (r) => json(r, []),
    "POST /api/jobs": (r, body) => { started.push(body); return json(r, job()); },
    "GET /api/jobs/dl0000000001": (r) => json(r, job()),
  });
  await page.goto("/models?tab=catalog");
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByRole("table", { name: "Models on the Hub" }).getByText("org/big").click();
  const dialog = page.getByRole("dialog", { name: "Does big fit?" });
  await expect(dialog.getByText("Estimated total")).toBeVisible();
  await dialog.getByRole("button", { name: "Download", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Download", exact: true }).click();
  expect(started).toEqual([{ stage: "hf_download", params: { repo_id: "org/big", quant: "" } }]);

  const panel = dialog.getByLabel("Download of org/big");
  await expect(panel).toBeVisible();
  await expect(dialog.getByText("Estimated total"), "the estimate is folded into one line").toHaveCount(0);
  await expect(dialog.getByText(/Fits on the graphics card: 4\.0 GB estimated/)).toBeVisible();
  await expect(panel.getByText("Downloading", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByText("1 of 4 files")).toBeVisible();
  await expect(panel.getByText("model-00001-of-00002.safetensors")).toBeVisible();
  const log = dialog.getByRole("log", { name: "Output of Download a model" });
  await expect(log).toContainText("Plan      4 files, 14.0 GB");

  const view = page.viewportSize()!;
  const frame = await dialog.boundingBox();
  const box = await panel.boundingBox();
  expect(frame!.y + frame!.height, "the dialog is within the window").toBeLessThanOrEqual(view.height);
  expect(box!.y + box!.height, "the panel of the download is in view without scrolling").toBeLessThanOrEqual(frame!.y + frame!.height);
  expect(box!.y).toBeGreaterThanOrEqual(frame!.y);
  await log.focus();
  await expect(log, "the terminal can be reached with the keyboard").toBeFocused();

  phase = "done";
  await expect(panel.getByText("Finished", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByText("14.0 GB in 4 files, took 2:00 (119.5 MB/s).")).toBeVisible();
  await expect(log).toContainText("Finished  14.0 GB in 2:00");
  await dialog.getByRole("button", { name: "Show estimate" }).click();
  await expect(dialog.getByText("Estimated total")).toBeVisible();
});

// A model that comes as safetensors, installed ready to run: the plan said in words before anything starts, the question, the steps in the panel (scripts, download, conversion), and what the
// rest of the app does when it ends. The job is answered by a fixture that moves from step to step.
const installPlan = (over: Record<string, unknown> = {}) => {
  const GB = 1024 ** 3;
  return { kind: "convert", repo_id: "org/small", revision: "d".repeat(40), role: "chat", role_detected: true, outtype: "f16", quant: "Q4_K_M", format: "Q4_K_M", format_reason: "Q4_K_M: 0.4 GB, the usual balance.", format_fit: "split",
    parameters: 494_000_000, parameters_estimated: false, architecture: "Qwen2ForCausalLM", supported: null,
    download: { files: 3, bytes: 1 * GB, partial_bytes: 0, free_bytes: 120 * GB, enough: true, gated: false, token_set: false },
    convert: { intermediate_bytes: 1 * GB, final_bytes: 0.4 * GB, final_bytes_max: 0.4 * GB, peak_bytes: 1.4 * GB, fallback: false, seconds: 53, seconds_note: "An estimate: a model of 0.5 billion parameters took 53 s here." },
    scripts: { tag: "b11528", installed: false, installed_tag: null, will_fetch: true, blockers: [], note: "about 115 small files, under 2 MB, from github.com (counted for llama.cpp b11528)" },
    disk_needed_bytes: 2.5 * GB, problems: [], ready: true, notes: [], ...over };
};
type Step = "scripts" | "download" | "convert" | "done";
const installProgress = (step: Step) => ({
  scripts: { state: "scripts", label: "Fetching the converter scripts", unit: "files", processed: 40, total: 115, step: 1, steps: 3, phase: "scripts" },
  download: { state: "downloading", label: "Downloading org/small", unit: "bytes", processed: 400 * 1024 ** 2, total: 1024 ** 3, rate: 50 * 1024 ** 2, eta: 12, files: 1, files_total: 3, current: "model.safetensors", step: 2, steps: 3, phase: "download" },
  convert: { state: "converting", label: "Converting org/small to Q4_K_M", unit: "percent", processed: 37, total: 100, step: 3, steps: 3, phase: "convert" },
  done: { state: "done", label: "org/small is ready to run", unit: "percent", processed: 100, total: 100, step: 3, steps: 3, phase: "convert", seconds: 70 },
})[step];

test("installing a model that comes as safetensors shows the plan, asks, goes through the steps, and Settings lists it by what it is without a reload", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const GB = 1024 ** 3;
  let step: Step | null = null;
  const job = () => ({ id: "inst00000001", stage: "install_model", status: step === "done" ? "done" : "running", params: { repo_id: "org/small", quant: "Q4_K_M", role: "chat" }, started: "2026-10-13T10:00:00Z",
    finished: step === "done" ? "2026-10-13T10:02:00Z" : "", error: "", log_count: 2, logs: ["Model     org/small", "Plan      3 files, 1.0 GB"], log_start: 0, progress: installProgress(step ?? "scripts") });
  const converted = { id: "org/small", name: "org-small-d1d1d1d1-Q4_K_M.gguf", label: "org/small, Q4_K_M", path: "D:/m/converted/org-small-d1d1d1d1-Q4_K_M.gguf", bytes: 0.4 * GB, source: "converted",
    info: { architecture: "qwen2", name: "x", quantisation: "Q4_K_M", context_length: 32768, layers: 24, parameters: 494_000_000 } };
  const started: unknown[] = [];
  await mockApi(page, {
    "GET /api/settings": (r) => json(r, { ...(fixtureFor("GET", "/api/settings") as object), llm_engine: "local", llm_base_url: "" }),
    "GET /api/models/search": (r) => json(r, { query: "", task: "", models: [{ id: "org/small", downloads: 5, likes: 1, task: "text-generation", library: null, gated: false, parameters: 494_000_000, updated: null }] }),
    "GET /api/models/download-plan": (r) => json(r, { repo_id: "org/small", revision: "d".repeat(40), gated: false, token_set: false, quants: [], needs_quant: false, files: 3, bytes: GB, free_bytes: 120 * GB, enough: true, partial_bytes: 0, patterns: ["*.safetensors", "*.json"] }),
    "GET /api/models/install-plan": (r) => json(r, installPlan()),
    "POST /api/models/fit": (r) => json(r, { mode: "inference", parts: { weights: 0.4 * GB, kv_cache: 0.1 * GB, overhead: 0.5 * GB, total: GB }, total: GB, notes: [],
      check: { gpu: "fits", ram: "fits", disk: "unknown", runs_on: "gpu", gpu_free_bytes: 6 * GB, ram_free_bytes: 12 * GB } }),
    "GET /api/serving/models": (r) => json(r, { files: step === "done" ? [converted] : [] }),
    "GET /api/jobs": (r) => json(r, step ? [job()] : []),
    "POST /api/jobs": (r, body) => { started.push(body); step = "scripts"; return json(r, job()); },
    "GET /api/jobs/inst00000001": (r) => json(r, job()),
  });
  await page.goto("/models?tab=catalog");
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByRole("table", { name: "Models on the Hub" }).getByText("org/small").click();
  const dialog = page.getByRole("dialog", { name: "Does small fit?" });

  await expect(dialog.getByRole("radio", { name: /Install it ready to run/ })).toBeChecked();
  await expect(dialog.getByText("Q4_K_M: 0.4 GB, the usual balance.")).toBeVisible();
  await expect(dialog.getByText(/converter scripts are fetched first from github\.com/)).toBeVisible();
  await expect(dialog.getByText(/about 50 seconds on the processor/)).toBeVisible();
  await dialog.getByRole("button", { name: "Install", exact: true }).click();
  const ask = page.getByRole("alertdialog");
  await expect(ask).toContainText("fetches llama.cpp b11528's converter scripts from github.com");
  await expect(ask).toContainText("converts the model on this computer's processor to Q4_K_M GGUF");
  await expect(ask).toContainText("The downloaded files are kept.");
  await ask.getByRole("button", { name: "Cancel" }).click();
  expect(started, "nothing starts when the person says no").toEqual([]);
  await dialog.getByRole("button", { name: "Install", exact: true }).click();
  await ask.getByRole("button", { name: "Install", exact: true }).click();
  expect(started).toEqual([{ stage: "install_model", params: { repo_id: "org/small", quant: "Q4_K_M", role: "chat" } }]);

  const panel = dialog.getByLabel("Install of org/small");
  await expect(panel).toBeVisible();
  await expect(panel.getByText("Fetching scripts", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByText("Step 1 of 3")).toBeVisible();
  step = "download";
  await expect(panel.getByText("Downloading", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByText("Step 2 of 3")).toBeVisible();
  await expect(panel.getByText("1 of 3 files")).toBeVisible();
  step = "convert";
  await expect(panel.getByText("Converting", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByText("37%")).toBeVisible();
  await expect(panel.getByText("Step 3 of 3")).toBeVisible();
  await expect(dialog.getByRole("log", { name: "Output of Install a model" })).toContainText("Plan      3 files, 1.0 GB");
  const view = page.viewportSize()!;
  const frame = await dialog.boundingBox();
  expect(frame!.y + frame!.height, "the dialog is within the window").toBeLessThanOrEqual(view.height);

  step = "done";
  await expect(panel.getByText("Ready to run", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByText(/org\/small is converted and ready to run as a language model/)).toBeVisible();

  await page.keyboard.press("Escape");                                         // the dialog goes; the app stays where it is, nothing is reloaded
  await page.getByRole("link", { name: "Settings" }).first().click();
  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: "Language model" }) }).last();
  await card.getByRole("combobox", { name: "Model" }).click();
  await expect(page.getByRole("option", { name: /org\/small, Q4_K_M/ })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("option", { name: /d1d1d1d1/ }), "the file name made of the id and the revision is not what is shown").toHaveCount(0);
});

test("Finish install on a model that is already here converts it, and the row says Ready when it ends", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const GB = 1024 ** 3;
  let step: Step | null = null;
  const job = () => ({ id: "fin00000001", stage: "install_model", status: step === "done" ? "done" : "running", params: { repo_id: "org/small", quant: "Q4_K_M", source: "hub_cache", skip_download: true, role: "chat" },
    started: "2026-10-13T10:00:00Z", finished: step === "done" ? "2026-10-13T10:01:00Z" : "", error: "", log_count: 1, logs: ["Model     org/small (already here)"], log_start: 0, progress: installProgress(step ?? "convert") });
  const listed = [{ id: "org/small", source: "hub_cache", path: "D:/m/hub/models--org--small", bytes: GB, revisions: [], main_revision: null, safetensors: true, gguf: [], weights: true, config: true }];
  const started: unknown[] = [];
  await mockApi(page, {
    "GET /api/models/local": (r) => json(r, { hub_cache: "D:/m/hub", hub_cache_exists: true, models_dir: "D:/m", models_dir_exists: true, models: listed, total_bytes: GB }),
    "GET /api/serving/models": (r) => json(r, { files: [], not_ready: step === "done" ? [] : [{ id: "org/small", name: "small", source: "hub_cache", bytes: GB, role: "chat" }] }),
    "GET /api/models/finish-plan": (r) => json(r, installPlan({ kind: "finish", download: null, scripts: { tag: "b11528", installed: true, installed_tag: "b11528", will_fetch: false, blockers: [], note: null }, disk_needed_bytes: 1.4 * GB })),
    "GET /api/jobs": (r) => json(r, step ? [job()] : []),
    "POST /api/jobs": (r, body) => { started.push(body); step = "convert"; return json(r, job()); },
    "GET /api/jobs/fin00000001": (r) => json(r, job()),
  });
  await page.goto("/models");
  const table = page.getByRole("table", { name: "Installed models" });
  await expect(table.getByText("Not ready")).toBeVisible();
  await table.getByRole("button", { name: "Finish the install of org/small" }).click();
  const dialog = page.getByRole("dialog", { name: "Finish installing small" });
  await expect(dialog.getByText(/converter scripts are already installed/)).toBeVisible();
  await expect(dialog.getByText("Download", { exact: true }), "nothing is downloaded for a model that is here").toHaveCount(0);
  await dialog.getByRole("button", { name: "Convert it" }).click();
  const ask = page.getByRole("alertdialog");
  await expect(ask).toContainText("This converts the model on this computer's processor to Q4_K_M GGUF");
  await ask.getByRole("button", { name: "Convert it" }).click();
  expect(started).toEqual([{ stage: "install_model", params: { repo_id: "org/small", quant: "Q4_K_M", source: "hub_cache", skip_download: true, role: "chat" } }]);
  const panel = dialog.getByLabel("Install of org/small");
  await expect(panel.getByText("Converting", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByText("37%")).toBeVisible();
  step = "done";
  await expect(panel.getByText("Ready to run", { exact: true })).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press("Escape");
  await expect(table.getByText("Ready", { exact: true })).toBeVisible({ timeout: 20_000 });
  await expect(table.getByRole("button", { name: "Finish the install of org/small" })).toHaveCount(0);
});

test("the Serving tab shows the running model and can pin converted models and set a budget", async ({ page }) => {
  const GB = 1024 ** 3;
  let budget = 0;
  let pinned = false;
  const entry = (name: string, over: object = {}) => ({ name, path: `D:/m/converted/${name}`, bytes: 4 * GB, files: 2, last_used: Date.now() / 1000 - 2 * 86400, used: true, pinned: false, label: `org/${name}, Q4_K_M`, in_use: null, settings_roles: [], adapters: 0, ...over });
  const sent: { method: string; path: string; body: any }[] = [];
  await mockApi(page, {
    "GET /api/serving/status": (r) => json(r, servingFixture([{ name: "aivf/qwen:latest", size_vram: 3 * GB, context_length: 4096 }])),
    "GET /api/serving/derived": (r) => json(r, { path: "D:/m/converted", budget_gb: budget, used_bytes: 8 * GB, entries: [entry("a", { pinned }), entry("b", { used: false, adapters: 1 })] }),
    "PUT /api/serving/derived/pin": (r) => { pinned = r.request().postDataJSON().pinned; return json(r, { name: "a", pinned }); },
    "PUT /api/settings": (r) => { budget = Number(r.request().postDataJSON().derived_cache_gb || 0); return json(r, { ...(fixtureFor("GET", "/api/settings") as object), derived_cache_gb: String(budget) }); },
  }, sent);
  await page.goto("/models?tab=serving");
  await expect(page.getByRole("button", { name: "Unload aivf/qwen:latest" })).toBeVisible();
  await expect(page.getByText(/4,096 context tokens/)).toBeVisible();

  const cache = page.getByRole("table", { name: "Converted models" });
  await expect(cache.getByText("org/a, Q4_K_M")).toBeVisible();
  await expect(cache.getByText("2 days ago").first()).toBeVisible();
  await cache.getByRole("button", { name: "Pin org/a, Q4_K_M" }).click();
  await expect(cache.getByRole("button", { name: "Unpin org/a, Q4_K_M" })).toBeVisible();
  await expect.poll(() => sent.find((s) => s.path === "/api/serving/derived/pin")?.body).toEqual({ name: "a", pinned: true });
  await page.getByLabel("Most the converted models may take, in GB").fill("6");
  await page.getByRole("button", { name: "Save" }).click();
  await expect.poll(() => sent.find((s) => s.method === "PUT" && s.path === "/api/settings")?.body).toEqual({ derived_cache_gb: "6" });
  await expect(page.getByText(/The limit is 6 GB\./)).toBeVisible();
  await expect(page.getByRole("button", { name: "Free space now" })).toBeEnabled();                       // 8 GB held against a limit of 6
});
