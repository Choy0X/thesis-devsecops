import { render, screen, fireEvent, within, waitFor } from "@testing-library/react";
import { vi, test, expect } from "vitest";
import { SandboxDetail } from "./Sandbox";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { sandbox } from "../lib/api";
vi.mock("../components/Terminal", () => ({ Terminal: () => null }));
vi.mock("./SandboxPolicy", () => ({ SandboxLimits: () => null }));
vi.mock("./SandboxFiles", () => ({ SandboxFiles: () => null }));
vi.mock("../lib/reconnectingSocket", () => ({ reconnectingSocket: () => () => {}, stateWords: () => "connected" }));
vi.mock("../lib/api", () => ({ sandbox: {
  deletionPreview: vi.fn(async () => ({ container: "full-id", image: { status: "retained_in_use", reason: "Another container uses the image." } })),
  action: vi.fn(async () => ({ deleted: false, reason: "Docker still lists the container; deletion was not confirmed." })),
} }));
test("keeps a failed deletion visible and retryable", async () => {
  const deleted = vi.fn();
  render(<ConfirmProvider><SandboxDetail cid="full-id" status="running" onChanged={() => {}} onDeleted={deleted} onRecreated={() => {}} /></ConfirmProvider>);
  expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  const dialog = await screen.findByRole("alertdialog");
  expect(dialog).toHaveTextContent("Another container uses the image.");
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete the sandbox" }));
  expect(await screen.findByText(/Docker still lists/)).toBeTruthy();
  expect(deleted).not.toHaveBeenCalled();
  await waitFor(() => expect(sandbox.action).toHaveBeenCalledWith("full-id", "delete"));
  expect(screen.getByRole("button", { name: "Delete" })).toBeEnabled();
});
