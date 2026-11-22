import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { CreateProjectDialog } from "./CreateProjectDialog";
import { get, write } from "./client";

vi.mock("./client", async original => ({ ...await original<typeof import("./client")>(), get: vi.fn(), write: vi.fn() }));
beforeEach(() => { vi.clearAllMocks(); });
const created = { id: "new-project", name: "Payments", source: "D:/code/payments" };
function show() {
  const onCreated = vi.fn(), onClose = vi.fn();
  render(<CreateProjectDialog onCreated={onCreated} onClose={onClose} />);
  return { onCreated, onClose, dialog: screen.getByRole("dialog", { name: "Create project" }) };
}

it("requires a name and a browsed folder without offering path entry", async () => {
  vi.mocked(write).mockImplementation(async path => path === "/api/shell/pick-folder" ? { ok: true, id: "pick" } as never : created as never);
  vi.mocked(get).mockResolvedValue({ state: "chosen", path: created.source } as never);
  const { onCreated, dialog } = show();
  const submit = within(dialog).getByRole("button", { name: "Create project" });
  expect(submit).toBeDisabled();
  expect(within(dialog).getAllByRole("textbox")).toHaveLength(1);
  fireEvent.change(within(dialog).getByRole("textbox", { name: "Project name" }), { target: { value: "Payments" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Browse folders" }));
  expect(await screen.findByText(created.source)).toBeInTheDocument();
  expect(submit).toBeEnabled();
  fireEvent.click(submit);
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
  expect(write).toHaveBeenCalledWith("/api/ide/projects", { name: "Payments", source: created.source });
});

it.each([
  ["C:\\code\\payments\\", "payments"],
  ["/home/projects/order service///", "order service"],
])("suggests the last folder of %s when the project name is blank", async (path, name) => {
  vi.mocked(write).mockImplementation(async route => route === "/api/shell/pick-folder" ? { ok: true, id: "pick" } as never : { ...created, name, source: path } as never);
  vi.mocked(get).mockResolvedValue({ state: "chosen", path } as never);
  const { onCreated, dialog } = show();
  const nameField = within(dialog).getByRole("textbox", { name: "Project name" });
  fireEvent.change(nameField, { target: { value: "   " } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Browse folders" }));
  expect(await screen.findByText(path)).toBeInTheDocument();
  expect(nameField).toHaveValue(name);
  fireEvent.click(within(dialog).getByRole("button", { name: "Create project" }));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith({ ...created, name, source: path }));
  expect(write).toHaveBeenCalledWith("/api/ide/projects", { name, source: path });
});

it("suggests a dropped folder name and preserves an edited name after another drop", () => {
  show();
  const name = screen.getByRole("textbox", { name: "Project name" });
  const zone = screen.getByRole("group", { name: "Project source selection" });
  fireEvent(zone, new CustomEvent("project-source-folder", { bubbles: true, detail: { path: "D:/code/payments" } }));
  expect(name).toHaveValue("payments");
  fireEvent.change(name, { target: { value: "Customer payments" } });
  fireEvent(zone, new CustomEvent("project-source-folder", { bubbles: true, detail: { path: "D:/code/payment-gateway/" } }));
  expect(name).toHaveValue("Customer payments");
});

it.each(["native-first", "dom-first"])("accepts a %s folder drop and keeps the selection read-only", async order => {
  vi.mocked(write).mockResolvedValue(created as never);
  const { dialog, onCreated } = show();
  fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), { target: { value: "Payments" } });
  const zone = screen.getByRole("group", { name: "Project source selection" });
  const domDrop = () => fireEvent.drop(zone, { dataTransfer: { files: [], items: [] } });
  if (order === "dom-first") domDrop();
  fireEvent(zone, new CustomEvent("project-source-folder", { bubbles: true, detail: { path: created.source } }));
  if (order === "native-first") domDrop();
  expect(screen.getByText(created.source)).toBeInTheDocument();
  expect(within(dialog).getAllByRole("textbox")).toHaveLength(1);
  fireEvent.click(within(dialog).getByRole("button", { name: "Create project" }));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created));
});

it("keeps cancelled native picking and cancelling the dialog free of project creation", async () => {
  vi.mocked(write).mockResolvedValue({ ok: true, id: "pick" } as never);
  vi.mocked(get).mockResolvedValue({ state: "cancelled", path: null } as never);
  const { onCreated, onClose, dialog } = show();
  fireEvent.click(screen.getByRole("button", { name: "Browse folders" }));
  await waitFor(() => expect(get).toHaveBeenCalled());
  expect(within(dialog).getByRole("button", { name: "Create project" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(onCreated).not.toHaveBeenCalled();
  expect(write).not.toHaveBeenCalledWith("/api/ide/projects", expect.anything());
});

it("reports unavailable browsing without asking for a typed path", async () => {
  vi.mocked(write).mockResolvedValue({ ok: false } as never);
  show(); fireEvent.click(screen.getByRole("button", { name: "Browse folders" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("requires the desktop app");
  expect(screen.getAllByRole("textbox")).toHaveLength(1);
});

it("rejects individual files and multiple folders", () => {
  show(); const target = screen.getByRole("group", { name: "Project source selection" });
  fireEvent.drop(target, { dataTransfer: { files: [new File(["code"], "app.py")], items: [{ kind: "file", webkitGetAsEntry: () => ({ isDirectory: false }) }] } });
  expect(screen.getByRole("alert")).toHaveTextContent("individual file");
  fireEvent.drop(target, { dataTransfer: { files: [new File([], "first"), new File([], "second")], items: [] } });
  expect(screen.getByRole("alert")).toHaveTextContent("one project folder");
});

it.each(["timeout", "native-result"])("never submits the previous folder while a new drop awaits %s", async result => {
  vi.mocked(write).mockResolvedValue({ ok: true, id: "pick" } as never);
  vi.mocked(get).mockResolvedValue({ state: "chosen", path: created.source } as never);
  const { dialog } = show();
  fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), { target: { value: "Payments" } });
  fireEvent.click(screen.getByRole("button", { name: "Browse folders" }));
  await screen.findByText(created.source);
  const submit = within(dialog).getByRole("button", { name: "Create project" });
  const zone = screen.getByRole("group", { name: "Project source selection" });
  expect(submit).toBeEnabled();
  vi.useFakeTimers();
  try {
    fireEvent.drop(zone, { dataTransfer: { files: [new File([], "replacement")], items: [{ kind: "file", webkitGetAsEntry: () => ({ isDirectory: true }) }] } });
    expect(submit).toBeDisabled();
    expect(screen.queryByText(created.source)).not.toBeInTheDocument();
    if (result === "native-result") {
      fireEvent(zone, new CustomEvent("project-source-folder", { bubbles: true, detail: { path: "D:/code/replacement" } }));
      expect(screen.getByText("D:/code/replacement")).toBeInTheDocument();
      expect(submit).toBeEnabled();
    }
    act(() => vi.advanceTimersByTime(800));
    if (result === "timeout") {
      expect(screen.getByRole("alert")).toHaveTextContent("Use Browse folders");
      expect(submit).toBeDisabled();
    } else expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(write).not.toHaveBeenCalledWith("/api/ide/projects", expect.anything());
  } finally { vi.useRealTimers(); }
});
