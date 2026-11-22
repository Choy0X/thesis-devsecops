import { expect, test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

async function runCommand(page: Page, name: RegExp) {
  await expect(page.getByRole("button", { name: "Workspace commands" })).toBeVisible();
  await page.keyboard.press("Control+k");
  await page.getByRole("dialog", { name: "Commands" }).getByRole("button", { name }).click();
}

async function openWorkspace(page: Page, action: string) {
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  await page.getByRole("region", { name: "Workspace details" }).getByRole("button", { name: new RegExp("^" + action) }).click();
}

async function mockProjectChats(page: Page, failFullContext = false, extraModels: Record<string, unknown>[] = [], stageable = false) {
  const model = { id: "fixture-qwen", name: "Qwen2.5-Coder 7B Instruct", kind: "local", context: 8192, model: "fixture.gguf", available: true };
  const projects = [{ id: "fixture", name: "Invoice API", source: "D:/example/invoice-api", settings: { instructions: "Preserve public routes.", memory: [] as string[] } }, { id: "worker", name: "Worker service", source: "D:/example/worker", settings: { instructions: "", memory: [] as string[] } }];
  const earlier = { id: "earlier", title: "Inspect invoice totals", model, last_active_at: new Date(Date.now() - 86400000).toISOString() };
  const archived = { id: "archived", title: "Review v1 routes", model, archived: true, last_active_at: new Date(Date.now() - 172800000).toISOString() };
  const sources = [{ id: "primary", kind: "project", label: "Invoice API", path: projects[0].source, primary: true }];
  const calls = { scans: [] as unknown[], chats: [] as Record<string, unknown>[], preparations: [] as Record<string, unknown>[], switches: [] as Record<string, unknown>[], integrations: [] as unknown[], staging: [] as unknown[], projects: [] as unknown[], turns: [] as unknown[] };
  let prepared = model, bound = model, polls = 0;
  let createdProject = "", createdTitle = "New chat";
  const historyChanges = new Map<string, { title?: string; pinned?: boolean }>();
  const deletedHistory = new Set<string>();
  const updateHistory = <T extends { id: string; title: string }>(chat: T) => ({ ...chat, ...historyChanges.get(chat.id) });
  const agentReady = new Set(["fixture-chat"]), agentPolls = new Map<string, number>();
  let branch = "fix-invoice-validation";
  const dependencyRun = {
    id: "scan-fixture", kind: "scan", state: "completed",
    payload: { scope: "dependencies", dependency_mode: "versions" },
    result: { status: "partial", packages: [
      { id: "npm:invoice-utils", name: "invoice-utils", ecosystem: "npm", resolved: ["1.0.0"], resolution: "locked",
        checks: { "1.0.0": { status: "affected", lookup: { status: "fresh" }, advisories: [{ id: "TEST-2026-1" }] } },
        recommendation: { version: "1.0.2", compatibility: "declared_ranges_satisfied" } },
      { id: "Go:example.org/invoice", name: "example.org/invoice", ecosystem: "Go", resolved: ["v1.0.0"], resolution: "minimum_declared",
        checks: { "v1.0.0": { status: "no_known_vulnerabilities", lookup: { status: "fresh" } } } },
    ] },
  };
  const codeScan = { id: "code-fixture", kind: "scan", state: "completed", payload: { scope: "project_code" },
    result: { findings: [], summary: { scanned_files: 4, skipped_entries: 1 } } };
  await page.route("**/api/**", async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
    let answer: unknown;
    if (path.startsWith("/api/chats/") && path.endsWith("/stream")) { await route.fulfill({ status: 204 }); return; }
    if (path === "/api/ide/projects" && method === "POST") { const body = request.postDataJSON(); calls.projects.push(body); const created = { id: "payments", ...body, settings: { instructions: "", memory: [] } }; projects.push(created); answer = created; }
    else if (path === "/api/ide/projects") answer = { projects: projects.map(item => ({ ...item, has_active_run: item.id === "fixture" && calls.turns.length > 0 })) };
    else if (path === "/api/ide/models") answer = { models: [model, ...extraModels] };
    else if (path === "/api/ide/models/prepare") {
      const body = request.postDataJSON(); calls.preparations.push(body); polls = 0;
      prepared = { ...body.model, context: body.context, id: body.context === 8192 ? body.model.id : `${body.model.id}-ctx${body.context}` };
      answer = failFullContext && body.context === 8192 ? { id: "prepare", state: "failed", phase: "failed", model: prepared, error: { code: "does_not_fit", message: "The selected model does not fit in available memory at 8192 tokens. Free memory or choose a shorter context." } } : { id: "prepare", state: "loading", phase: "verifying", message: "Verifying the selected model artifact.", progress: { completed: 512, total: 1024, unit: "bytes" }, model: prepared };
    }
    else if (path === "/api/ide/models/prepare/prepare") answer = { id: "prepare", state: ++polls >= 2 ? "ready" : "loading", phase: "loading", message: "Loading the selected model artifact...", model: prepared };
    else if (path.endsWith("/model-status")) answer = { ready: true, model_id: path.includes("new-chat") ? prepared.id : bound.id, phase: "ready" };
    else if (path.endsWith("/model") && method === "POST") { const body = request.postDataJSON(); calls.switches.push(body); bound = body.model; agentReady.delete("fixture-chat"); answer = { id: "fixture-chat", title: "Review request validation", model: bound }; }
    else if (path.endsWith("/runtime/prepare") || path.endsWith("/runtime-status")) {
      const id = path.split("/")[3], selected = id === "new-chat" ? prepared : bound;
      if (method === "POST" && !agentReady.has(id)) agentPolls.set(id, 0);
      if (method === "GET" && agentPolls.has(id)) { const count = agentPolls.get(id)! + 1; agentPolls.set(id, count); if (count >= 2) agentReady.add(id); }
      answer = { chat_id: id, model_id: selected.id, ready: agentReady.has(id), state: agentReady.has(id) ? "ready" : "loading", phase: agentReady.has(id) ? "ready" : "connecting_tools", message: agentReady.has(id) ? "The coding agent is ready." : "Connecting the configured project tools.", elapsed_seconds: 2 };
    }
    else if (path.endsWith("/checkout")) {
      const identity = { available: true, branch, revision: "fedcba987654" };
      answer = url.searchParams.has("path") ? { ...identity, diffs: [{ kind: "unstaged", diff: "@@ -1 +1 @@\n-retries = 2\n+retries = 3", truncated: false }] }
        : { ...identity, files: [{ path: "config.py", status: " M" }], truncated: false };
    }
    else if (path.endsWith("/chats") && method === "POST") { const body = request.postDataJSON(); createdProject = path.split("/")[4]; calls.chats.push(body); answer = { id: "new-chat", title: "New chat", model: body.model }; }
    else if (path.endsWith("/chats")) {
      const project = path.split("/")[4];
      const recent = project === "fixture" ? [{ id: "fixture-chat", title: "Review request validation", model: bound, last_active_at: new Date().toISOString() }, earlier, archived] : [];
      const rows = project === createdProject ? [{ id: "new-chat", title: createdTitle, model: prepared, last_active_at: new Date().toISOString() }, ...recent] : recent;
      answer = { chats: rows.filter(chat => !deletedHistory.has(chat.id)).map(updateHistory) };
    }
    else if (path.startsWith("/api/chats/") && path.split("/").length === 4 && (method === "PATCH" || method === "DELETE")) {
      const id = path.split("/").at(-1)!;
      if (method === "DELETE") { deletedHistory.add(id); answer = { deleted: true }; }
      else {
        historyChanges.set(id, { ...historyChanges.get(id), ...request.postDataJSON() });
        answer = updateHistory(id === earlier.id ? earlier : { id, title: "Review request validation" });
      }
    }
    else if (path === "/api/chats/fixture-chat") answer = { chat: { id: "fixture-chat", title: "Review request validation", model: bound }, context_usage: {
      input_tokens: 1440, window_tokens: 2048, estimated: false, compacted_messages: 23, recalled_messages: 2, compacted_tool_exchanges: 0,
    }, messages: [
      { info: { id: "user-1", role: "user" }, parts: [{ type: "text", text: "Review the request validation before we change it." }] },
      { info: { id: "agent-1", role: "assistant" }, parts: [{ type: "text", text: "I read the route and its tests. The amount check needs to reject negative values. I will keep the public API unchanged and add a regression for negative amounts." }] }],
      runs: calls.turns.length ? [{ id: "run", state: "running", phase: "starting_runtime" }] : [
        ...calls.scans.some(scan => (scan as { dependency_mode?: string }).dependency_mode === "versions") ? [dependencyRun] : [],
        ...calls.scans.some(scan => (scan as { scope?: string }).scope === "project_code") ? [codeScan] : [],
      ],
      activity: calls.turns.length ? { run_id: "run", phase: "starting_runtime", message: "Starting the coding runtime...", started_at: new Date(Date.now() - 4000).toISOString() } : null, permissions: [], questions: [] };
    else if (path === "/api/chats/new-chat") answer = { chat: { id: "new-chat", title: createdTitle, model: prepared }, messages: [], runs: [], permissions: [], questions: [] };
    else if (path === "/api/chats/earlier" || path === "/api/chats/archived") answer = { chat: path.endsWith("earlier") ? earlier : archived, messages: [], runs: [], permissions: [], questions: [] };
    else if (path.endsWith("/sources") && method === "POST") { const body = request.postDataJSON(); const source = { id: "route-source", kind: body.kind, label: body.path || body.url, path: body.path, primary: false }; sources.push(source); answer = source; }
    else if (path.endsWith("/sources")) answer = { sources };
    else if (path.endsWith("/plan")) answer = { items: [{ content: "Inspect validation and existing tests", status: "completed" }, { content: "Add a negative-amount regression", status: "in_progress" }, { content: "Verify the change", status: "pending" }], text: "", origin: "native_todos", status: "ready" };
    else if (path.endsWith("/subagents")) answer = { subagents: [{ id: "task-1", description: "Review request validation tests", agent: "explore", status: "completed" }] };
    else if (path.endsWith("/changes")) answer = { changes: [{ path: "src/routes.py", status: "modified", before: "before", after: "after-hash", diff: "--- src/routes.py\n+++ src/routes.py\n-return create(amount)\n+if amount < 0:\n+    raise ValueError('Amount must be positive')\n+return create(amount)" }] };
    else if (path.endsWith("/integrations")) { calls.integrations.push(request.postDataJSON()); answer = { integrated: ["src/routes.py"] }; }
    else if (path.endsWith("/files")) answer = { files: [{ path: "src/routes.py", bytes: 150 }, { path: "tests/test_routes.py", bytes: 100 }] };
    else if (path.endsWith("/dependencies")) answer = { packages: [{ id: "PyPI:fastapi", name: "fastapi", ecosystem: "PyPI", installed: ["0.115.0"], locked: ["0.115.0"], declared: [">=0.115"], status: "resolved", relationship: "direct" }] };
    else if (path.endsWith("/findings")) answer = {
      scans: [{ id: "scan-fixture", scope: "model_review", state: "completed", status: "complete", reviewed: 3, skipped: 0 }],
      evidence: path.startsWith("/api/chats/fixture-chat/") ? [{
        id: "case-1", origin: "model_review", scanId: "scan-fixture", status: "grounded", title: "Audit payload needs review",
        weakness: "CWE-532", summary: "Omit the session label from the audit payload.", severity: "high",
        source: { path: "src/session.py", line: 38, quote: 'audit("started", label=label)', sha256: "a".repeat(64), freshness: "current" },
      }] : [],
      advisoryCoverage: { "case-1": "no_match" },
    };
    else if (path.endsWith("/patches")) answer = { candidates: path.startsWith("/api/chats/fixture-chat/") ? [{ candidate_id: "patch-1", file: "src/session.py", rule: "Audit payload", outcome: "passed", receipt_state: "not_verified", created: "2026-11-19T12:00:00Z" }] : [] };
    else if (path.endsWith("/patches/patch-1")) answer = {
      candidate_id: "patch-1", project_id: "fixture", file: "src/session.py", receipt_state: "not_verified", created: "2026-11-19T12:00:00Z", label: "Audit payload",
      finding: { message: "Audit payload needs review" },
      source: { path: "src/session.py", line: 38, quote: 'audit("started", label=label)', freshness: "current" },
      diff: '@@ -38 +38 @@\n-audit("started", label=label)\n+audit("started")',
      steps: { regression: { status: "passed", detail: "Existing audit checks passed" } },
      can_stage: stageable, stage_reason: "No staging receipt", diff_sha256: "b".repeat(64),
    };
    else if (path.endsWith("/patches/patch-1/stage")) { calls.staging.push(request.postDataJSON()); answer = { candidate_id: "patch-1", staged_paths: ["src/session.py"], receipt_id: "receipt-1" }; }
    else if (path.endsWith("/events")) answer = { events: [] };
    else if (path.endsWith("/scans")) { calls.scans.push(request.postDataJSON()); answer = { id: "scan-fixture", state: "queued" }; }
    else if (path.endsWith("/turns")) { const body = request.postDataJSON(); calls.turns.push(body); if (path.includes("new-chat")) createdTitle = body.text; answer = { id: "run", state: "queued" }; }
    else if (path === "/api/shell/pick-folder") answer = { ok: true, id: "pick-source" };
    else if (path === "/api/shell/pick-folder/pick-source") answer = { state: "chosen", path: "D:/example/browsed-service" };
    else answer = fixtureFor(method, path) ?? {};
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
  });
  return { ...calls, switchBranch: (value: string) => { branch = value; } };
}

test("Project settings keep actions visible while editing on a narrow screen", async ({ page }) => {
  await mockProjectChats(page);
  const saved: unknown[] = [];
  await page.route("**/api/ide/projects/fixture", async route => {
    const patch = route.request().postDataJSON();
    saved.push(patch);
    await route.fulfill({ json: { id: "fixture", name: patch.name, source: "D:/example/invoice-api", settings: { instructions: "Preserve public routes.", memory: patch.memory } } });
  });
  await page.route("**/api/ide/extensions", route => route.fulfill({ json: { extensions: [] } }));
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  await page.getByRole("button", { name: "Project settings", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Project settings" });
  await expect(dialog.getByText("D:/example/invoice-api", { exact: true })).toBeVisible();
  await dialog.getByRole("textbox", { name: "Project name" }).fill("Billing API");
  await dialog.getByRole("button", { name: "Agent context" }).click();
  await page.setViewportSize({ width: 390, height: 680 });
  await dialog.getByRole("button", { name: "Add memory entry" }).click();
  await expect(dialog.getByRole("textbox", { name: "Memory entry 1" })).toBeFocused();
  await dialog.getByRole("textbox", { name: "Memory entry 1" }).fill("Keep invoice identifiers stable.");
  await expect(dialog.getByRole("progressbar", { name: "Project guidance usage" })).toHaveAttribute("value", "55");
  await expect(dialog.getByRole("progressbar", { name: "Model context usage" })).toHaveAttribute("value", "1440");
  expect(await dialog.getByRole("progressbar").evaluateAll(meters => meters.map(meter => meter.getAttribute("aria-label"))))
    .toEqual(["Project guidance usage", "Model context usage"]);
  await expect(dialog.getByText(/automatically compacted/)).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Save project settings" })).toBeInViewport();
  await dialog.getByRole("button", { name: "Save project settings" }).click();
  await expect(dialog.getByRole("status")).toHaveText("Project settings saved.");
  expect(saved).toEqual([{ name: "Billing API", memory: ["Keep invoice identifiers stable."] }]);
  await dialog.getByRole("button", { name: "Extensions" }).click();
  await dialog.getByRole("button", { name: "Add extension" }).click();
  await dialog.getByRole("combobox", { name: "Extension type" }).click();
  await page.getByRole("option", { name: "MCP server" }).click();
  await expect(dialog.getByRole("textbox", { name: "MCP configuration (JSON)" })).toBeVisible();
  await expect(dialog.getByRole("textbox", { name: "Local skill path" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("button", { name: "Project settings", exact: true })).toBeFocused();
});

test("Projects centers the empty chat hint and inherits the app palette", async ({ page }) => {
  await mockProjectChats(page);
  await page.goto("/projects?project=fixture&new=1");
  const hint = page.getByText("Ask about your project or open Commands with", { exact: false });
  const composer = page.locator(".ide-composer");
  await expect(hint).toBeVisible();
  await expect(composer).toBeVisible();

  for (const width of [1360, 420]) {
    await page.setViewportSize({ width, height: 820 });
    const [hintBox, composerBox] = await Promise.all([hint.boundingBox(), composer.boundingBox()]);
    expect(hintBox).not.toBeNull();
    expect(composerBox).not.toBeNull();
    expect(Math.abs((hintBox!.x + hintBox!.width / 2) - (composerBox!.x + composerBox!.width / 2))).toBeLessThan(2);
    expect(hintBox!.y + hintBox!.height).toBeLessThan(composerBox!.y);
  }

  const palette = await page.locator(".project-ide").evaluate(node => {
    const tokens = getComputedStyle(node);
    const theme = getComputedStyle(document.documentElement);
    return ["--bg", "--surface", "--border"].map(key => [tokens.getPropertyValue(key), theme.getPropertyValue(key)]);
  });
  for (const [project, theme] of palette) expect(project.trim()).toBe(theme.trim());
});

test("Projects displays streaming text and tool activity before a turn completes", async ({ page }) => {
  await mockProjectChats(page);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  let connections = 0;
  let continueStream!: () => void;
  const continued = new Promise<void>(resolve => { continueStream = resolve; });
  await page.route("**/api/chats/fixture-chat/stream", async route => {
    const first = connections++ === 0;
    if (!first) await continued;
    const snapshot = {
      chat: { id: "fixture-chat", title: "Review request validation" },
      runs: [{ id: "turn-1", state: first ? "running" : "completed" }],
      messages: [{ info: { id: "reply", role: "assistant" }, parts: [
        { type: "text", text: first ? "Reading the project" : "Reading the project revealed a missing check." },
        { type: "tool", tool: "read", state: { status: first ? "running" : "completed" } },
        ...(!first ? [{ type: "tool", tool: "search_project", state: { status: "completed", output: {
          hits: [{ path: "src/validation.ts", start_line: 7, end_line: 8, excerpt: "export function validateRequest() {}" }],
          complete: false, omissions: ["Local index is warming up."],
        } } }] : []),
      ] }],
      permissions: [], questions: [],
    };
    await route.fulfill({ contentType: "text/event-stream", body: `retry: 100\nevent: snapshot\ndata: ${JSON.stringify(snapshot)}\n\n` });
  });
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  await expect(page.getByText("Reading the project", { exact: true })).toBeVisible();
  await expect(page.locator("summary").filter({ hasText: "Read file" })).toContainText("running");
  await expect(page.getByText("Reading the project revealed a missing check.")).toHaveCount(0);
  continueStream();
  await expect(page.getByText("Reading the project revealed a missing check.")).toBeVisible();
  await expect(page.locator("summary").filter({ hasText: "Read file" })).toContainText("completed");
  await page.locator("summary").filter({ hasText: "Search project" }).click();
  await expect(page.getByText("src/validation.ts:7-8", { exact: true })).toBeVisible();
  await expect(page.getByText("Partial search coverage")).toBeVisible();
  expect(errors).toEqual([]);
});

test("Project citations open the workspace editor and folder explorer", async ({ page }) => {
  await mockProjectChats(page);
  await page.route("**/api/chats/fixture-chat", route => route.fulfill({ json: {
    chat: { id: "fixture-chat", title: "Review project" }, runs: [], permissions: [], questions: [],
    messages: [{ info: { id: "answer", role: "assistant" }, parts: [{ type: "text", text: "See `go.mod`, `cert/`, and [the generator](cert/generate.go#L2). `missing.go` is a candidate. `../other.go` is outside the workspace.\n\n```text\ngo.mod\n```" }] }],
  } }));
  await page.route("**/api/chats/fixture-chat/files", route => route.fulfill({ json: { files: [{ path: "go.mod" }, { path: "cert/generate.go" }, { path: "cert/nested/README.md" }] } }));
  const edits: unknown[] = [];
  await page.route("**/api/chats/fixture-chat/file?*", async route => {
    const path = new URL(route.request().url()).searchParams.get("path");
    await route.fulfill({ json: { path, version: "version-1", text: path === "go.mod" ? "module example.org/demo\r\n\r\ngo 1.24\r\n" : "package cert\n// line two\n" } });
  });
  await page.route("**/api/chats/fixture-chat/file", async route => {
    const edit = route.request().postDataJSON(); edits.push(edit);
    if (edit.path === "cert/generate.go") { await route.fulfill({ status: 409, json: { detail: "This file changed after it was opened." } }); return; }
    await route.fulfill({ json: { ...edit, version: "updated" } });
  });
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  const citation = page.getByRole("button", { name: "go.mod", exact: true });
  await expect(citation).toHaveCount(1);
  await citation.click();
  const panel = page.getByRole("complementary", { name: "Workspace files" });
  const editor = panel.getByRole("textbox", { name: "Edit go.mod" });
  await expect(editor).toContainText("module example.org/demo");
  await editor.press("Control+End");
  await editor.pressSequentially("// reviewed");
  await panel.getByRole("button", { name: "Close workspace files" }).click();
  await expect(citation).toBeFocused();
  await citation.click();
  await expect(editor).toContainText("// reviewed");
  await editor.press("Control+s");
  await expect.poll(() => edits).toEqual([{ path: "go.mod", version: "version-1", text: "module example.org/demo\r\n\r\ngo 1.24\r\n// reviewed" }]);
  await panel.getByRole("button", { name: "Close workspace files" }).click();
  await expect(citation).toBeFocused();
  await page.getByRole("button", { name: "cert/", exact: true }).click();
  await expect(panel.getByRole("navigation", { name: "Workspace folder" })).toContainText("cert");
  await panel.getByRole("searchbox").fill("nested");
  await expect(panel.getByRole("button", { name: "generate.go", exact: true })).toHaveCount(0);
  await panel.getByRole("button", { name: "nested", exact: true }).click();
  await expect(panel.getByRole("button", { name: "README.md", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "the generator", exact: true }).click();
  const generator = panel.getByRole("textbox", { name: "Edit cert/generate.go" });
  await expect(generator.locator(".cm-activeLine")).toHaveText("// line two");
  await generator.pressSequentially("// pending ");
  await panel.getByRole("button", { name: "Save file" }).click();
  await expect(panel.getByRole("alert")).toContainText("This file changed after it was opened.");
  await expect(generator).toContainText("// pending");
  await page.setViewportSize({ width: 560, height: 780 });
  await expect(panel).toBeInViewport();
  await expect(generator).toBeVisible();
  await panel.getByRole("button", { name: "Close workspace files" }).click();
  await expect(page.getByRole("button", { name: "../other.go", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "missing.go", exact: true }).click();
  await expect(panel).toContainText("Could not find a unique workspace match for missing.go");
});

test("Projects uses a terminal transcript, a rich composer and optional right panels", async ({ page }) => {
  const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
  const calls = await mockProjectChats(page);
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  const composer = page.getByRole("textbox", { name: "Message agent" });
  await expect(composer).toBeVisible();
  const navigation = page.getByRole("navigation", { name: "Projects", exact: true });
  await page.getByRole("button", { name: "Show project navigation" }).click();
  await expect(navigation.getByRole("button")).toHaveCount(3);
  await expect(navigation.getByRole("button", { name: "Invoice API", exact: true })).toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("heading", { name: "Review request validation", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Workspace", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Workspace details" })).toHaveCount(0);
  await expect(page.locator(".ide-editor, .ide-explorer, .ide-tray, .ide-terminal")).toHaveCount(0);
  for (const name of ["changes", "plan", "subagents", "sources"]) await expect(page.getByRole("button", { name: `Open ${name}`, exact: true })).toHaveCount(0);
  const geometry = await page.locator(".project-chat").evaluate(element => { const main = document.getElementById("content")!.getBoundingClientRect(), area = element.getBoundingClientRect(); return { width: main.width - area.width, height: main.height - area.height, overflow: element.scrollWidth > element.clientWidth }; });
  expect(Math.abs(geometry.width)).toBeLessThan(2); expect(Math.abs(geometry.height)).toBeLessThan(2); expect(geometry.overflow).toBe(false);
  await page.screenshot({ path: "test-results/project-chat-desktop.png", fullPage: true });
  await openWorkspace(page, "Changes");
  await page.getByRole("button", { name: /Agent workspace/ }).click();
  await page.getByRole("button", { name: "modified src/routes.py" }).click();
  await expect(page.getByRole("region", { name: "Workspace diff for src/routes.py" })).toContainText("Amount must be positive");
  await page.getByRole("button", { name: "Mark reviewed" }).click();
  await page.getByRole("button", { name: "Integrate reviewed files" }).click();
  expect(calls.integrations).toEqual([{ paths: ["src/routes.py"], review: { "src/routes.py": "after-hash" } }]);
  await page.getByRole("button", { name: "Close changes", exact: true }).click();
  await openWorkspace(page, "Plan");
  await expect(page.getByText("Add a negative-amount regression", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/project-chat-plan.png", fullPage: true });
  await page.getByRole("button", { name: "Show subagents", exact: true }).click();
  await expect(page.getByText("Review request validation tests", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Show sources", exact: true }).click();
  await page.getByRole("button", { name: "Add source", exact: true }).last().click();
  await page.getByRole("combobox", { name: "Project file", exact: true }).click();
  await page.getByRole("option", { name: "src/routes.py", exact: true }).click();
  await page.getByRole("button", { name: "Add source", exact: true }).last().click();
  await expect(page.locator(".chat-source-chips")).toContainText("src/routes.py");
  await page.getByRole("button", { name: "Close sources" }).click();
  await page.getByRole("button", { name: "Bold text" }).click(); await expect(composer).toHaveValue("**text**");
  await composer.fill("/sc");
  await expect(page.getByRole("menu", { name: "Terminal commands" }).getByRole("menuitem", { name: /\/scan/ })).toBeVisible();
  await composer.fill("/scan dep");
  await expect(page.getByRole("menu", { name: "Scan commands" }).getByRole("menuitem", { name: /\/scan dependencies/ })).toBeVisible();
  await composer.press("Escape");
  expect(calls.scans).toHaveLength(0);
  await composer.fill("/scan dependencies s");
  await page.getByRole("menuitem", { name: /Select packages for source review/ }).click();
  await expect(page.getByRole("heading", { name: "Select packages for source review", exact: true })).toBeVisible();
  await expect(composer).toHaveValue("");
  expect(calls.scans).toHaveLength(0);
  await page.getByRole("checkbox", { name: "Review source for fastapi" }).check(); await page.getByRole("button", { name: "Review 1 packages" }).click();
  expect(calls.scans).toHaveLength(1);
  await page.setViewportSize({ width: 700, height: 760 });
  await expect(navigation).toHaveCount(0);
  await expect(composer).toBeVisible();
  await page.screenshot({ path: "test-results/project-chat-compact.png", fullPage: true });
  await openWorkspace(page, "Plan");
  await expect(page.getByRole("complementary", { name: "Plan panel" })).toBeVisible();
  await page.screenshot({ path: "test-results/project-chat-compact-plan.png", fullPage: true });
  await page.getByRole("button", { name: "Close plan" }).click();
  expect(await page.locator(".project-chat").evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test("Projects resumes within the project and opens a fresh terminal on desktop and compact widths", async ({ page }) => {
  const calls = await mockProjectChats(page);
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  const composer = page.getByRole("textbox", { name: "Message agent" });
  await composer.fill("/resume"); await composer.press("Enter");
  const filter = page.getByRole("combobox", { name: "Filter conversations" });
  const options = page.getByRole("grid", { name: "Project conversations" });
  await expect(options.getByRole("row")).toHaveCount(3);
  await expect(options.getByRole("rowgroup", { name: "Archived" })).toContainText("Review v1 routes");
  await page.screenshot({ path: "test-results/project-terminal-resume.png", fullPage: true });
  await filter.fill("INVOICE TOTALS"); await filter.press("Enter");
  await expect(page).toHaveURL(/project=fixture&chat=earlier/);
  await expect(composer).toBeFocused();
  await page.setViewportSize({ width: 700, height: 760 });
  await composer.fill("/resume"); await composer.press("Enter");
  await expect(options).toBeVisible();
  await page.screenshot({ path: "test-results/project-terminal-compact-resume.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 720 });
  await expect(filter).toBeInViewport();
  await expect(options).toBeInViewport();
  await expect(options.getByRole("button", { name: "Review v1 routes", exact: true })).toHaveCSS("align-items", "flex-start");
  expect(await page.locator(".project-chat").evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await filter.press("Escape");
  await expect(composer).toBeFocused();
  await composer.fill("/new"); await composer.press("Enter");
  await expect(page).toHaveURL(/project=fixture&new=1/);
  await expect(composer).toHaveValue("");
  await expect(page.getByRole("heading", { name: "New conversation" })).toBeVisible();
  expect(await page.locator(".ide-composer-controls").evaluate(element => {
    const model = element.querySelector('[aria-label="Agent model"]')!.getBoundingClientRect();
    const send = element.querySelector('[aria-label="Send message"]')!.getBoundingClientRect();
    return Math.abs((model.top + model.bottom) / 2 - (send.top + send.bottom) / 2);
  })).toBeLessThan(3);
  await page.screenshot({ path: "test-results/project-new-compact.png", animations: "disabled" });
  await page.setViewportSize({ width: 1360, height: 860 });
  await page.screenshot({ path: "test-results/project-new-conversation.png", animations: "disabled" });
  expect(calls.chats).toHaveLength(0); expect(calls.preparations).toHaveLength(0);
});

test("Projects renames, pins and deletes conversations from history", async ({ page }) => {
  await mockProjectChats(page);
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  await page.getByRole("button", { name: "Resume conversation", exact: true }).click();
  const history = page.getByRole("region", { name: "Resume conversation" });
  await history.getByRole("button", { name: "Rename Inspect invoice totals" }).click();
  const title = history.getByRole("textbox", { name: "Conversation title" });
  await title.fill("Invoice follow-up");
  await title.press("Enter");
  await history.getByRole("button", { name: "Pin Invoice follow-up" }).click();
  const pinned = history.getByRole("rowgroup", { name: "Pinned" });
  await expect(pinned).toContainText("Invoice follow-up");
  await page.reload();
  await page.getByRole("button", { name: "Resume conversation", exact: true }).click();
  await expect(pinned).toContainText("Invoice follow-up");
  await pinned.getByRole("button", { name: "Unpin Invoice follow-up" }).click();
  await expect(pinned).toHaveCount(0);
  await history.getByRole("button", { name: "Delete Invoice follow-up" }).click();
  const confirm = page.getByRole("alertdialog", { name: "Delete conversation?" });
  await confirm.getByRole("button", { name: "Cancel" }).click();
  await expect(history.getByRole("button", { name: "Delete Invoice follow-up" })).toBeVisible();
  await history.getByRole("button", { name: "Delete Invoice follow-up" }).click();
  await confirm.getByRole("button", { name: "Delete conversation" }).click();
  await expect(history.getByText("Invoice follow-up", { exact: true })).toHaveCount(0);
  await history.getByRole("button", { name: "Delete Review request validation" }).click();
  await confirm.getByRole("button", { name: "Delete conversation" }).click();
  await expect(page).toHaveURL(/project=fixture&new=1/);
  await expect(page.getByRole("heading", { name: "New conversation" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Message agent" })).toBeFocused();
});

test("Projects restores a planning question and resumes the same saved run after a custom answer", async ({ page }) => {
  await mockProjectChats(page);
  const model = { id: "fixture-qwen", name: "Qwen2.5-Coder 7B Instruct", kind: "local", context: 8192 };
  const question = { id: "choice", questions: [{ question: "Which retry policy?", options: [
    { label: "Reuse policy", description: "Keeps one implementation.", recommended: true },
    { label: "Separate policy", description: "Allows independent timeouts." },
  ] }] };
  let answered = false, resumed = false;
  const savedAt = "2026-11-19T12:00:00Z";
  const plans = [{ content: "Choose the retry policy", status: "in_progress" }];
  await page.route("**/api/chats/fixture-chat", route => route.fulfill({ json: {
    chat: { id: "fixture-chat", title: "Plan retry policy", model },
    messages: [], permissions: [], questions: answered ? [] : [question],
    runs: [{ id: "saved-run", kind: "turn", state: resumed ? "completed" : "interrupted" }],
    recovery: resumed ? null : { run_id: "saved-run", question: answered ? null : question, saved_at: savedAt, attempts: 0, uncertain: [] },
    progress: { mode: "plan", state: resumed ? "completed" : "interrupted", saved_at: savedAt, items: plans },
  } }));
  await page.route("**/api/chats/fixture-chat/questions/choice", route => {
    expect(route.request().postDataJSON()).toEqual({ answers: [["Use explicit deadlines"]] });
    answered = true;
    return route.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/chats/fixture-chat/runs/saved-run/resume", route => {
    expect(answered).toBe(true);
    resumed = true;
    return route.fulfill({ json: { id: "saved-run", state: "queued" } });
  });
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  const form = page.getByRole("form", { name: "Planning question" });
  await expect(form.getByText("Recommended", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Resume saved work" })).toHaveCount(0);
  await page.setViewportSize({ width: 430, height: 800 });
  await form.getByText("Write my own answer", { exact: true }).click();
  await form.getByRole("textbox", { name: /Custom answer/ }).fill("Use explicit deadlines");
  await page.screenshot({ path: "test-results/project-planning-question.png", animations: "disabled" });
  await form.getByRole("button", { name: "Send answer" }).click();
  await expect.poll(() => resumed).toBe(true);
  await expect(form).toHaveCount(0);
});

test("Projects loads the selected model before creating a chat and exposes explicit memory recovery", async ({ page }) => {
  const calls = await mockProjectChats(page, true);
  await page.goto("/projects?project=worker");
  const composer = page.getByRole("textbox", { name: "Message agent" });
  await composer.fill("Review worker startup"); await composer.press("Enter");
  const failure = page.getByRole("dialog", { name: "Model could not load" });
  await expect(failure).toBeVisible(); expect(calls.chats).toHaveLength(0); expect(calls.turns).toHaveLength(0);
  await expect(failure.getByRole("alert")).toContainText("8192 tokens");
  await page.screenshot({ path: "test-results/project-chat-model-memory.png", fullPage: true });
  await failure.getByRole("button", { name: "Retry with 4,096 tokens" }).click();
  await expect(page.getByRole("progressbar", { name: "File verification" })).toBeVisible();
  await page.screenshot({ path: "test-results/project-chat-model-loading.png", fullPage: true });
  expect(calls.chats).toHaveLength(0);
  await expect(page).toHaveURL(/chat=new-chat/);
  await expect(page.getByRole("textbox", { name: "Message agent" })).toBeVisible();
  expect(calls.chats).toHaveLength(1); expect(calls.chats[0].preparation_id).toBe("prepare");
  expect((calls.chats[0].model as { context: number }).context).toBe(4096);
  expect(calls.preparations.map(item => item.context)).toEqual([8192, 4096]);
  expect(calls.turns).toHaveLength(1);
  await expect(page.getByRole("heading", { name: "Review worker startup" })).toBeVisible();
});

test("Projects explains when the selected model fails its real coding tool check", async ({ page }) => {
  const calls = await mockProjectChats(page);
  await page.route("**/api/ide/models/prepare", route => route.fulfill({ json: {
    id: "tool-check", state: "failed", model: route.request().postDataJSON().model,
    error: { code: "tool_check_failed", message: "This model did not complete a real tool call and use its result.",
      action: "Choose another installed coding model." },
  } }));
  await page.goto("/projects?project=worker&new=1");
  const composer = page.getByRole("textbox", { name: "Message agent" });
  await composer.fill("Inspect the project startup");
  await composer.press("Enter");
  const failure = page.getByRole("dialog", { name: "Coding tool check failed" });
  await expect(failure.getByRole("alert")).toContainText("did not complete a real tool call");
  await expect(failure.getByText("Choose another installed coding model.")).toBeVisible();
  expect(calls.chats).toHaveLength(0);
  expect(calls.turns).toHaveLength(0);
  await failure.getByRole("button", { name: "Close", exact: true }).last().click();
  await expect(composer).toHaveValue("Inspect the project startup");
});

test("Projects runs scan actions immediately and opens an independent branch diff", async ({ page }) => {
  const errors: string[] = []; page.on("pageerror", error => errors.push(error.message));
  const calls = await mockProjectChats(page);
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  const composer = page.getByRole("textbox", { name: "Message agent" });
  await composer.fill("Keep this draft");
  await expect(page.getByRole("group", { name: "Agent mode" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Composer actions" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Actions", exact: true })).toHaveCount(0);
  await page.keyboard.press("Control+k");
  const commands = page.getByRole("dialog", { name: "Commands" });
  await expect(commands.getByRole("button", { name: /Review with v40/ })).toHaveCount(0);
  await page.screenshot({ path: "test-results/project-actions.png", animations: "disabled" });
  await commands.getByRole("button", { name: /^Check dependencies / }).click();
  await expect.poll(() => calls.scans).toEqual([expect.objectContaining({ scope: "dependencies", dependency_mode: "versions" })]);
  expect(calls.preparations).toEqual([]);
  await expect(page.getByText("Suggested version")).toBeVisible();
  await expect(page.getByText("1.0.2", { exact: true })).toBeVisible();
  await expect(page.getByText("Resolved version unknown")).toBeVisible();
  await runCommand(page, /^Scan code /);
  const coverage = page.getByRole("group", { name: "Code scan results" });
  await expect(coverage.getByText("Files scanned")).toBeVisible();
  await expect(coverage.getByText("4", { exact: true })).toBeVisible();
  await expect(coverage.getByText("Entries skipped")).toBeVisible();
  await expect(coverage.getByText("1", { exact: true })).toBeVisible();
  expect(calls.preparations).toEqual([]);
  await expect(composer).toHaveValue("Keep this draft");
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
  const workspace = page.getByRole("region", { name: "Workspace details" });
  await expect(workspace.getByText("fix-invoice-validation", { exact: true })).toBeVisible();
  await expect(workspace.getByText("1 file", { exact: true })).toBeVisible();
  for (const name of ["Plan", "Subagents", "Sources", "Review dependency source"]) {
    await expect(workspace.getByRole("button", { name, exact: true })).toBeVisible();
  }
  await page.screenshot({ path: "test-results/project-workspace-panel.png", animations: "disabled" });
  await workspace.getByRole("button", { name: /^Changes/ }).click();
  await expect(composer).toBeHidden();
  await expect(page.getByRole("region", { name: "Changes workspace" })).toBeVisible();
  await expect(page.getByText("fix-invoice-validation", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Unified diff" })).toContainText("+retries = 3");
  calls.switchBranch("maintenance");
  await page.getByRole("button", { name: "Refresh working tree" }).click();
  await expect(page.getByText("maintenance", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/project-worktree.png", animations: "disabled" });
  await page.setViewportSize({ width: 390, height: 800 });
  await expect(page.getByRole("region", { name: "Changes workspace" })).toBeVisible();
  expect(await page.locator(".project-chat").evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await page.screenshot({ path: "test-results/project-worktree-compact.png", animations: "disabled" });
  await page.getByRole("button", { name: "Close changes" }).click();
  await expect(composer).toHaveValue("Keep this draft");
  expect(errors).toEqual([]);
});

test("A receipt-backed patch stages before opening the existing reviewed changes flow", async ({ page }) => {
  const calls = await mockProjectChats(page, false, [], true);
  await page.goto("/projects?project=fixture&chat=fixture-chat&view=patch");
  await page.getByRole("button", { name: "Stage patch" }).click();
  await expect(page.getByRole("region", { name: "Changes workspace" })).toBeVisible();
  expect(calls.staging).toEqual([{ expected_diff_sha256: "b".repeat(64) }]);
  expect(calls.integrations).toEqual([]);
});

test("The v40 reviewer is a text-only choice in the regular composer", async ({ page }) => {
  const reviewer = { id: "aivf/reviewer-v40:latest", tag: "aivf/reviewer-v40:latest", name: "AIVF Reviewer v40",
    kind: "local", context: 8192, available: true, tool_protocol: "text" };
  const calls = await mockProjectChats(page, false, [reviewer]);
  await page.goto("/projects?project=worker&new=1");
  await page.getByRole("combobox", { name: "Agent model" }).click();
  await page.getByRole("option", { name: /AIVF Reviewer v40.*Text only/ }).click();
  await expect(page.getByRole("group", { name: "Agent mode" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Install a tool-capable model" })).toBeVisible();
  const composer = page.getByRole("textbox", { name: "Message agent" });
  await composer.fill("Review this validation check.");
  await composer.press("Enter");
  await expect.poll(() => calls.turns).toEqual([expect.objectContaining({ text: "Review this validation check.", mode: "plan" })]);
  expect(calls.scans).toEqual([]);
  expect(calls.chats[0].model).toMatchObject(reviewer);
});

test("Selecting an Ollama Cloud model requires consent before preparation", async ({ page }) => {
  const cloud = { id: "demo-cloud:cloud", name: "Fixture Cloud", tag: "demo-cloud:cloud", kind: "cloud", location: "cloud", tool_protocol: "native", context: 8192, available: true };
  const calls = await mockProjectChats(page, false, [cloud]);
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  const openModels = async () => { await page.getByRole("combobox", { name: "Agent model" }).click(); };
  await openModels();
  await page.getByRole("option", { name: /Fixture Cloud/ }).click();
  const consent = page.getByRole("dialog", { name: "Allow Ollama Cloud?" });
  await expect(consent).toBeVisible();
  expect(calls.preparations).toEqual([]);
  await consent.getByRole("button", { name: "Cancel" }).click();
  expect(calls.preparations).toEqual([]);
  await openModels();
  await page.getByRole("option", { name: /Fixture Cloud/ }).click();
  await consent.getByRole("button", { name: "Allow for this conversation" }).click();
  await expect.poll(() => calls.preparations).toEqual([{ model: { ...cloud, allow_remote: true }, context: 8192 }]);
});

test("Composer switches an existing chat model with progress and sends without reloading it", async ({ page }) => {
  const small = { id: "fixture-small", name: "Example/Small-Coder, Q4_K_M", kind: "local", context: 8192, model: "small.gguf", available: true };
  const calls = await mockProjectChats(page, false, [small]);
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  const composer = page.getByRole("textbox", { name: "Message agent" });
  await composer.fill("Keep this draft while switching models.");
  await page.getByRole("combobox", { name: "Agent model" }).click();
  await page.getByRole("option", { name: small.name, exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Switching model" });
  await expect(dialog).toContainText("Your current model stays selected");
  await expect(dialog.getByRole("progressbar", { name: "File verification" })).toHaveAttribute("aria-valuenow", "50");
  await page.screenshot({ path: "test-results/project-chat-model-switch.png", fullPage: true });
  await expect(dialog.getByRole("progressbar", { name: "Model loading" })).toBeVisible();
  await expect(dialog.getByRole("progressbar", { name: "Agent preparation" })).toBeVisible();
  await expect(dialog).toContainText("Connecting the configured project tools.");
  await page.screenshot({ path: "test-results/project-chat-agent-preparation.png", fullPage: true });
  await expect(dialog).toBeHidden();
  expect(calls.switches).toHaveLength(1);
  expect(calls.switches[0]).toMatchObject({ preparation_id: "prepare", expected_model_id: "fixture-qwen", model: small });
  await expect(page.getByRole("combobox", { name: "Agent model" })).toContainText(small.name);
  await expect(composer).toHaveValue("Keep this draft while switching models.");
  await expect(page.getByText("Review the request validation before we change it.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(composer).toHaveValue("");
  expect(calls.turns).toHaveLength(1);
  expect(calls.preparations).toHaveLength(1);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: "Starting the coding runtime..." })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "Agent model" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Stop active run" })).toBeVisible();
  await page.screenshot({ path: "test-results/project-chat-runtime-activity.png", fullPage: true });
});

test("Projects offers installed model choices and loads the chosen artifact", async ({ page }) => {
  const small = { id: "fixture-small", name: "Example/Small-Coder, Q4_K_M", kind: "local", context: 8192, model: "small.gguf", available: true };
  const calls = await mockProjectChats(page, false, [small]);
  await page.goto("/projects?project=fixture&chat=fixture-chat");
  const composer = page.getByRole("textbox", { name: "Message agent" });
  await composer.fill("/new"); await composer.press("Enter");
  const choice = page.getByRole("combobox", { name: "Agent model" });
  await expect(choice).toBeVisible();
  await choice.click();
  await expect(page.getByRole("option", { name: "Qwen2.5-Coder 7B Instruct", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: small.name, exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/project-chat-model-options.png", fullPage: true });
  await page.getByRole("option", { name: small.name, exact: true }).click();
  expect(calls.preparations).toHaveLength(0);
  await composer.fill("Review invoice settings"); await composer.press("Enter");
  await expect(page.getByRole("dialog", { name: "Loading selected model" })).toContainText(small.name);
  await expect(page).toHaveURL(/chat=new-chat/);
  expect(calls.preparations[0].model).toEqual(small);
  expect(calls.chats[0].model).toMatchObject({ id: small.id, model: small.model, name: small.name });
});
