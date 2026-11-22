import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { fileDraftsFor, resetFileDrafts, unsavedFiles } from "../../lib/fileDrafts";
import { get, write } from "./client";
import { WorkspaceFiles } from "./WorkspaceFiles";

// The editor is CodeMirror in the app; here a textarea with the same contract (onChange, onSave on Ctrl+S).
vi.mock("./Editor", () => ({
  Editor: ({ text, onChange, onSave, readOnly }: { text: string; onChange: (value: string) => void; onSave: () => void; readOnly?: boolean }) =>
    <textarea aria-label="File text" value={text} readOnly={readOnly} onChange={event => onChange(event.target.value)}
      onKeyDown={event => { if (event.ctrlKey && event.key === "s") { event.preventDefault(); onSave(); } }} />,
}));
vi.mock("./client", async original => {
  const actual = await original<typeof import("./client")>();
  return { ...actual, get: vi.fn(), write: vi.fn() };
});

let saved = "first line\nsecond line\n";
beforeEach(() => {
  resetFileDrafts(); vi.clearAllMocks(); saved = "first line\nsecond line\n";
  vi.mocked(get).mockImplementation(async path => {
    if (path.endsWith("/files")) return { files: [{ path: "src/app.py" }, { path: "notes.txt" }] } as never;
    if (path.includes("/file?")) return { path: new URLSearchParams(path.split("?")[1]).get("path"), text: saved, version: "v1" } as never;
    throw new Error(`Unexpected GET ${path}`);
  });
});

function drawer(onClose = vi.fn()) {
  return render(<WorkspaceFiles chatId="chat" reference={{ path: "src/app.py" }} drafts={fileDraftsFor("chat")} readOnly={false} onClose={onClose} onSaved={vi.fn()} />);
}

it("saves with the version it opened and shows Saved", async () => {
  vi.mocked(write).mockImplementation(async (_path, body) => ({ path: "src/app.py", text: (body as { text: string }).text, version: "v2" }) as never);
  drawer();
  const editor = await screen.findByRole("textbox", { name: "File text" });
  fireEvent.change(editor, { target: { value: "first line\nchanged\n" } });
  expect(unsavedFiles("chat")).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: /Save file/ }));
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/chats/chat/file", { path: "src/app.py", text: "first line\nchanged\n", version: "v1" }, "PUT"));
  expect(await screen.findByText("Saved")).toBeInTheDocument();
  expect(unsavedFiles("chat")).toBe(0);
});

it("saves with Ctrl+S", async () => {
  vi.mocked(write).mockImplementation(async (_path, body) => ({ path: "src/app.py", text: (body as { text: string }).text, version: "v2" }) as never);
  drawer();
  const editor = await screen.findByRole("textbox", { name: "File text" });
  fireEvent.change(editor, { target: { value: "edited\n" } });
  fireEvent.keyDown(editor, { key: "s", ctrlKey: true });
  await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
});

it("keeps the edit and offers a reload when the file changed on the server", async () => {
  vi.mocked(write).mockRejectedValue(new Error("The file changed since it was opened. Reload it before saving."));
  drawer();
  const editor = await screen.findByRole("textbox", { name: "File text" });
  fireEvent.change(editor, { target: { value: "my edit\n" } });
  fireEvent.click(screen.getByRole("button", { name: /Save file/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("changed since it was opened");
  expect(editor).toHaveValue("my edit\n");
  expect(unsavedFiles("chat")).toBe(1);
  saved = "server text\n";
  fireEvent.click(screen.getByRole("button", { name: "Discard edits and reload" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "File text" })).toHaveValue("server text\n"));
  expect(unsavedFiles("chat")).toBe(0);
});

it("writes CRLF files back with CRLF line endings", async () => {
  saved = "one\r\ntwo\r\n";
  vi.mocked(write).mockImplementation(async (_path, body) => ({ path: "src/app.py", text: (body as { text: string }).text, version: "v2" }) as never);
  drawer();
  const editor = await screen.findByRole("textbox", { name: "File text" });
  fireEvent.change(editor, { target: { value: "one\ntwo\nthree\n" } });
  fireEvent.click(screen.getByRole("button", { name: /Save file/ }));
  await waitFor(() => expect(write).toHaveBeenCalledWith("/api/chats/chat/file", expect.objectContaining({ text: "one\r\ntwo\r\nthree\r\n" }), "PUT"));
});

it("keeps unsaved edits when the drawer closes and opens again", async () => {
  const first = drawer();
  fireEvent.change(await screen.findByRole("textbox", { name: "File text" }), { target: { value: "draft text\n" } });
  first.unmount();
  expect(unsavedFiles("chat")).toBe(1);
  drawer();
  expect(await screen.findByRole("textbox", { name: "File text" })).toHaveValue("draft text\n");
  expect(screen.getByText("Unsaved")).toBeInTheDocument();
  await act(async () => { await Promise.resolve(); });
  expect(vi.mocked(get).mock.calls.filter(([path]) => path.includes("/file?"))).toHaveLength(1);
});
