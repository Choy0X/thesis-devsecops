import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { servingUnload: vi.fn() } };
});
import { api } from "../lib/api";
import { UnloadModelDialog } from "./UnloadModelDialog";

beforeEach(() => {
  vi.mocked(api.servingUnload).mockReset();
});

describe("UnloadModelDialog", () => {
  it("asks the gateway to unload everything loaded, shows a spinner while it is in flight, and calls onDone once it resolves", async () => {
    let resolve: () => void = () => undefined;
    vi.mocked(api.servingUnload).mockReturnValue(new Promise((r) => { resolve = () => r({ unloaded: ["llm", "embed"], status: {} } as any); }));
    const onDone = vi.fn();
    render(<UnloadModelDialog onDone={onDone} />);
    expect(await screen.findByRole("dialog", { name: "Unloading the model" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Unloading");
    expect(api.servingUnload).toHaveBeenCalledWith();
    expect(onDone).not.toHaveBeenCalled();
    resolve();
    await waitFor(() => expect(onDone).toHaveBeenCalled());
  });

  it("cannot be dismissed while unloading: no Close button, and Esc does nothing", async () => {
    vi.mocked(api.servingUnload).mockReturnValue(new Promise(() => undefined));
    const onDone = vi.fn();
    render(<UnloadModelDialog onDone={onDone} />);
    await screen.findByRole("status");
    expect(screen.queryByRole("button", { name: "Close" })).toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onDone).not.toHaveBeenCalled();
  });

  it("shows the reason and a way to leave anyway when the unload fails", async () => {
    vi.mocked(api.servingUnload).mockRejectedValue(new Error("The gateway did not answer."));
    const onDone = vi.fn();
    render(<UnloadModelDialog onDone={onDone} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The gateway did not answer.");
    fireEvent.click(screen.getByRole("button", { name: "Leave anyway" }));
    expect(onDone).toHaveBeenCalled();
  });
});
