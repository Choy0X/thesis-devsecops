import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, clearRequestCache } from "./api";

// The conversations of a project change with every turn, so they are never read from the short-lived cache of GET requests: after a turn the page reads them again and must see them.

const answers = (...bodies: unknown[]) => {
  const fetchMock = vi.fn();
  for (const b of bodies) fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(b), { status: 200, headers: { "content-type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
};

beforeEach(() => clearRequestCache());
afterEach(() => vi.unstubAllGlobals());

describe("the conversations of a project are read fresh", () => {
  it("reads one conversation from the server every time", async () => {
    const f = answers({ id: "c1", messages: [1] }, { id: "c1", messages: [1, 2] });
    expect((await api.projectChatGet("p", "c1")).messages).toHaveLength(1);
    expect((await api.projectChatGet("p", "c1")).messages).toHaveLength(2);
    expect(f).toHaveBeenCalledTimes(2);
  });

  it("reads the list from the server every time", async () => {
    const f = answers({ chats: [], damaged: 0 }, { chats: [{ id: "c1" }], damaged: 0 });
    expect((await api.projectChats("p")).chats).toHaveLength(0);
    expect((await api.projectChats("p")).chats).toHaveLength(1);
    expect(f).toHaveBeenCalledTimes(2);
  });

  it("sends the right requests for the rest", async () => {
    const f = answers({}, {}, { ok: true }, { stopped: true });
    await api.projectChatCreate("p", { title: "t", params: { temperature: 0.2 } });
    await api.projectChatUpdate("p", "c1", { title: "new" });
    await api.projectChatDelete("p", "c1");
    await api.projectChatStop("p", "c1");
    const calls = f.mock.calls.map(([url, init]) => [new URL(url as string, "http://x").pathname, (init as RequestInit | undefined)?.method, (init as RequestInit | undefined)?.body]);
    expect(calls).toEqual([
      ["/api/projects/p/chats", "POST", JSON.stringify({ title: "t", params: { temperature: 0.2 } })],
      ["/api/projects/p/chats/c1", "PATCH", JSON.stringify({ title: "new" })],
      ["/api/projects/p/chats/c1", "DELETE", undefined],
      ["/api/projects/p/chats/c1/stop", "POST", undefined]]);
  });

  it("streams a turn as events, one for each line, also when a line is cut between two chunks", async () => {
    const enc = new TextEncoder();
    const body = new ReadableStream({ start(c) { c.enqueue(enc.encode('{"type":"saved","user_id":"u","assistant_id":"a","title":"t"}\n{"type":"to')); c.enqueue(enc.encode('ken","text":"Hi"}\n{"type":"done","sources":[],"usage":null,"model":"m"}')); c.close(); } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
    const seen: string[] = [];
    await api.projectChatSend("p", "c1", { text: "q", allow_remote: false }, new AbortController().signal, (e) => seen.push(e.type));
    expect(seen).toEqual(["saved", "token", "done"]);
  });

  it("rejects with the sentence of a refusal before the stream starts, and ends quietly when the signal is aborted", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: false, error: "This conversation is still being answered.", code: "busy" }), { status: 409, headers: { "content-type": "application/json" } })));
    await expect(api.projectChatSend("p", "c1", { text: "q", allow_remote: false }, new AbortController().signal, () => undefined)).rejects.toMatchObject({ code: "busy", status: 409 });
    const controller = new AbortController();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => { controller.abort(); return Promise.reject(new DOMException("aborted", "AbortError")); }));
    await expect(api.projectChatSend("p", "c1", { text: "q", allow_remote: false }, controller.signal, () => undefined)).resolves.toBeUndefined();
  });
});
