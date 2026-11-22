import { expect, test, type Page, type Route } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// The chat of a project, on the built app with the API answered here: conversations that are kept, a streamed answer with its thinking, its code and the files it read, Stop, asking again,
// editing a question, renaming and deleting a conversation, and the chat with the whole window. The server's side (saving the question first, the answer as it is written) is tested in
// pytest and in the desktop integration test; here the page is given what the server would say.

type Chat = { schema: string; id: string; title: string; created_at: string; updated_at: string; params: { system: string; temperature: number | null; top_p: number | null; max_tokens: number | null }; messages: any[] };
const PROJECT = { id: "shop-aaaaaa", name: "Shop", folders: [{ path: "C:/code/shop", languages: { Python: 4 }, ecosystems: ["PyPI"], manifests: { PyPI: ["requirements.txt"] }, files_seen: 4, truncated: false }],
  languages: { Python: 4 }, ecosystems: ["PyPI"], created_at: "2026-10-09T10:00:00Z", updated_at: "2026-10-09T10:00:00Z",
  authorization: { acknowledged: true, at: "2026-10-09T10:00:05Z", statement: "I own this code." }, last_scan: null };
const json = (route: Route, body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const lines = (events: unknown[]) => events.map((e) => JSON.stringify(e)).join("\n") + "\n";

/** The server's side of the chat, in memory: the conversations and what a turn does to them. */
function server(page: Page, answer: (n: number, body: any) => { events: unknown[]; text: string; extra?: object }, hold?: { until: Promise<void> }) {
  const db = new Map<string, Chat>();
  const sent: { path: string; body: any }[] = [];
  let turns = 0, ids = 0;
  const made = (title = ""): Chat => { const id = `c0000000000${++ids}`.slice(-12); const c: Chat = { schema: "chat/1", id, title, created_at: "2026-10-13T09:00:00.000Z", updated_at: `2026-10-13T09:0${ids}:00.000Z`, params: { system: "", temperature: null, top_p: null, max_tokens: null }, messages: [] }; db.set(id, c); return c; };
  const summary = (c: Chat) => ({ id: c.id, title: c.title || "New conversation", created_at: c.created_at, updated_at: c.updated_at, message_count: c.messages.length, preview: c.messages.at(-1)?.content ?? "" });
  const base = `**/api/projects/${PROJECT.id}`;
  const ready = page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    let body: any = null;
    try { body = request.postData() ? JSON.parse(request.postData()!) : null; } catch { body = null; }
    const m = path.match(/^\/api\/projects\/shop-aaaaaa\/chats(?:\/([0-9a-z]{12}))?(?:\/(messages|stop))?$/);
    if (path === "/api/projects") return json(route, { projects: [PROJECT], statement: "I own this code." });
    if (path === "/api/projects/shop-aaaaaa/chat/endpoint") return json(route, { configured: true, model: "qwen2.5-coder-7b", host: "127.0.0.1", local: true, provider: "managed" });
    if (m) {
      const [, id, action] = m;
      if (!id && request.method() === "GET") return json(route, { chats: [...db.values()].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).map(summary), damaged: 0 });
      if (!id && request.method() === "POST") { const c = made(body?.title ?? ""); return json(route, c); }
      const chat = id ? db.get(id) : undefined;
      if (!chat) return json(route, { ok: false, error: "There is no such conversation.", code: "not_found" }, 404);
      if (!action && request.method() === "GET") return json(route, chat);
      if (!action && request.method() === "PATCH") { if (body?.title) chat.title = body.title; if (body?.params) chat.params = { ...chat.params, ...body.params }; return json(route, chat); }
      if (!action && request.method() === "DELETE") { db.delete(chat.id); return json(route, { ok: true }); }
      if (action === "stop") { sent.push({ path: "stop", body }); release(); return json(route, { stopped: true }); }
      if (action === "messages") {
        sent.push({ path: "messages", body });
        const turn = answer(++turns, body);
        if (body.regenerate && chat.messages.at(-1)?.role === "assistant") chat.messages.pop();
        if (body.edit_of) chat.messages.splice(chat.messages.findIndex((x) => x.id === body.edit_of));
        const uid = `u${turns}${chat.id}`.slice(0, 12), aid = `a${turns}${chat.id}`.slice(0, 12);
        if (!body.regenerate) chat.messages.push({ id: uid, role: "user", content: body.text, created_at: "2026-10-13T09:10:00.000Z" });
        if (!chat.title) chat.title = String(body.text).slice(0, 60);
        chat.messages.push({ id: aid, role: "assistant", content: turn.text, created_at: "2026-10-13T09:10:01.000Z", model: "qwen2.5-coder-7b", ...turn.extra });
        chat.updated_at = `2026-10-13T10:0${turns % 10}:00.000Z`;
        if (hold) await hold.until;
        return route.fulfill({ status: 200, contentType: "application/x-ndjson", headers: { "cache-control": "no-store" }, body: lines([{ type: "saved", user_id: uid, assistant_id: aid, title: chat.title }, ...turn.events]) });
      }
    }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixtureFor(request.method(), path) ?? {}) });
  });
  let release: () => void = () => undefined;
  return { db, sent, ready, made, setRelease: (fn: () => void) => { release = fn; }, base };
}

const FIRST = {
  text: "The login is in **auth.py**.\n\n```python\ndef login(user):\n    return check(user)\n```\n\nSee also [the docs](https://example.org/docs).",
  events: [
    { type: "start", model: "qwen2.5-coder-7b", model_label: "qwen2.5-coder-7b", host: "127.0.0.1", local: true },
    { type: "thinking", text: "I should read auth.py first." },
    { type: "tool", name: "read_file", arguments: "{}", ok: true, summary: "Read auth.py lines 1 to 40 of 120" },
    { type: "token", text: "The login is in **auth.py**.\n\n```python\ndef login(user):\n    return check(user)\n```\n\nSee also [the docs](https://example.org/docs)." },
    { type: "done", sources: [{ tool: "read_file", summary: "Read auth.py lines 1 to 40 of 120" }], usage: { prompt_tokens: 812, completion_tokens: 60, seconds: 1.6, tokens_per_second: 37.5 }, model: "qwen2.5-coder-7b", model_label: "qwen2.5-coder-7b" },
  ],
  extra: { thinking: "I should read auth.py first.", tools: [{ ok: true, summary: "Read auth.py lines 1 to 40 of 120" }], sources: [{ tool: "read_file", summary: "Read auth.py lines 1 to 40 of 120" }],
    usage: { prompt_tokens: 812, completion_tokens: 60, seconds: 1.6, tokens_per_second: 37.5 } },
};
const plain = (text: string) => ({ text, events: [{ type: "start", model: "qwen2.5-coder-7b", model_label: "qwen2.5-coder-7b", host: "127.0.0.1", local: true }, { type: "token", text }, { type: "done", sources: [], usage: null, model: "qwen2.5-coder-7b", model_label: "qwen2.5-coder-7b" }], extra: { sources: [] } });

async function send(page: Page, text: string) {
  await page.getByRole("textbox", { name: "Your question" }).fill(text);
  await page.keyboard.press("Enter");
}

test("a question is answered with markdown, highlighted code, its thinking and the files it read, and the conversation is kept and named", async ({ page }) => {
  const s = server(page, () => FIRST);
  await s.ready;
  await page.goto(`/projects?project=${PROJECT.id}&focus=chat`);
  await expect(page.getByLabel("Questions to start with")).toBeVisible();
  await send(page, "Where is the login?");
  const thread = page.getByRole("list", { name: "The conversation" });
  await expect(thread.getByText("Where is the login?")).toBeVisible();
  await expect(thread.locator("strong", { hasText: "auth.py" })).toBeVisible();
  await expect(thread.locator("pre code.language-python")).toBeVisible();
  await expect(thread.locator(".hljs-keyword").first()).toHaveText("def");
  await expect(thread.getByText("python", { exact: true })).toBeVisible();
  await expect(thread.getByRole("link", { name: "the docs" })).toHaveAttribute("href", "https://example.org/docs");
  await expect(thread.getByRole("link", { name: "the docs" })).toHaveAttribute("rel", "noopener noreferrer");
  await expect(thread.getByText("Thinking", { exact: true })).toBeVisible();
  await expect(thread.getByText("I should read auth.py first.")).toBeHidden();                     // folded once the answer has begun
  await thread.getByText("Thinking", { exact: true }).click();
  await expect(thread.getByText("I should read auth.py first.")).toBeVisible();
  await expect(thread.getByText("Looked at: Read auth.py lines 1 to 40 of 120")).toBeVisible();
  await expect(thread.getByText("Model qwen2.5-coder-7b")).toBeVisible();
  await expect(thread.getByText("60 tokens, 37.5 tokens a second, 1.6 s (prompt 812 tokens)")).toBeVisible();
  expect(s.sent[0].body).toEqual({ text: "Where is the login?", allow_remote: false });
  await expect(page.getByRole("navigation", { name: "Conversations" }).getByRole("button", { name: /^Where is the login\?/ })).toHaveAttribute("aria-current", "true");
  await page.reload();
  await expect(page.getByRole("list", { name: "The conversation" }).getByText("Where is the login?")).toBeVisible();          // kept: read again from the server
});

test("the last answer is asked again, a question is edited, a conversation is renamed and another deleted, each after the right question", async ({ page }) => {
  const s = server(page, (n) => (n === 1 ? plain("First answer.") : n === 2 ? plain("Second answer.") : plain("Answer to the edited question.")));
  await s.ready;
  await page.goto(`/projects?project=${PROJECT.id}&focus=chat`);
  await send(page, "A question");
  const thread = page.getByRole("list", { name: "The conversation" });
  await expect(thread.getByText("First answer.")).toBeVisible();
  await thread.getByRole("button", { name: "Regenerate the answer" }).click();
  await expect(thread.getByText("Second answer.")).toBeVisible();
  await expect(thread.getByText("First answer.")).toHaveCount(0);
  expect(s.sent.at(-1)!.body).toEqual({ regenerate: true, allow_remote: false });
  await thread.getByRole("button", { name: "Edit and send again" }).click();
  await expect(page.getByText("Sending it again removes the answers that came after this question.")).toBeVisible();
  await page.getByRole("textbox", { name: "Your question, edited" }).fill("A better question");
  await page.getByRole("button", { name: "Send again" }).click();
  await expect(thread.getByText("Answer to the edited question.")).toBeVisible();
  await expect(thread.getByText("Second answer.")).toHaveCount(0);
  expect(s.sent.at(-1)!.body).toMatchObject({ text: "A better question", allow_remote: false });
  expect(s.sent.at(-1)!.body.edit_of).toBeTruthy();

  const list = page.getByRole("navigation", { name: "Conversations" });
  await list.getByRole("button", { name: "New chat" }).click();
  await send(page, "Another thing");
  await expect(thread.getByText("Another thing")).toBeVisible();
  await list.getByRole("button", { name: /^A question/ }).click();
  await expect(thread.getByText("Answer to the edited question.")).toBeVisible();
  await list.getByRole("button", { name: "Rename A question" }).click();
  await list.getByRole("textbox", { name: "New name of A question" }).fill("Login talk");
  await page.keyboard.press("Enter");
  await expect(list.getByRole("button", { name: /^Login talk/ })).toBeVisible();
  await list.getByRole("button", { name: "Delete Another thing" }).click();
  await expect(page.getByRole("alertdialog")).toContainText('Delete "Another thing"?');
  await page.getByRole("alertdialog").getByRole("button", { name: "Delete it" }).click();
  await expect(list.getByRole("button", { name: /^Another thing/ })).toHaveCount(0);
  await expect(list.getByRole("button", { name: /^Login talk/ })).toBeVisible();
  expect([...s.db.values()].map((c) => c.title)).toEqual(["Login talk"]);
});

test("Stop is a request to the server, and the answer that was written stays on the page as stopped", async ({ page }) => {
  let open: () => void = () => undefined;
  const until = new Promise<void>((resolve) => { open = resolve; });
  const s = server(page, () => ({ text: "Half an ans", events: [{ type: "start", model: "qwen2.5-coder-7b", host: "127.0.0.1", local: true }, { type: "token", text: "Half an ans" },
    { type: "done", sources: [], usage: null, model: "qwen2.5-coder-7b", stopped: true }], extra: { stopped: true, sources: [] } }), { until });
  s.setRelease(open);
  await s.ready;
  await page.goto(`/projects?project=${PROJECT.id}&focus=chat`);
  await send(page, "A long question");
  const thread = page.getByRole("list", { name: "The conversation" });
  await expect(thread.getByText("Writing...")).toBeVisible();
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(thread.getByText("Half an ans")).toBeVisible();
  await expect(thread.getByText("Stopped. What was written so far is kept.")).toBeVisible();
  expect(s.sent.map((x) => x.path)).toEqual(["messages", "stop"]);
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeVisible();
});

test("choosing Chat on the project page takes the whole window, and going back returns to it", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  const s = server(page, () => FIRST);
  await s.ready;
  s.made("An earlier talk");
  await page.goto(`/projects?project=${PROJECT.id}`);
  await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();
  await page.getByText("Chat", { exact: true }).first().click();
  await expect(page).toHaveURL(/focus=chat/);
  await expect(page.getByRole("heading", { level: 1, name: "Chat about Shop" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Sections" })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "Conversations" })).toBeVisible();
  await expect(page.getByRole("region", { name: "This conversation's settings" })).toBeVisible();
  await send(page, "Where is the login?");
  await expect(page.getByRole("list", { name: "The conversation" }).locator(".hljs-keyword").first()).toBeVisible();
  const view = page.viewportSize()!;
  const fits = await page.evaluate(() => ({ scroll: document.documentElement.scrollHeight, inner: window.innerHeight, box: document.querySelector("textarea")!.getBoundingClientRect().bottom }));
  expect(fits.scroll, "the window does not scroll").toBeLessThanOrEqual(view.height);
  expect(fits.box, "the box for a question is in view").toBeLessThanOrEqual(view.height);
  await page.getByRole("button", { name: "Back to the project" }).click();
  await expect(page).not.toHaveURL(/focus=chat/);
  await expect(page.getByRole("navigation", { name: "Sections" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Shop" }).first()).toBeVisible();
});
