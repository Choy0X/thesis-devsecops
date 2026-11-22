import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DownloadNotice } from "./DownloadNotice";
import { ToastProvider } from "./Toast";

const show = () => render(<ToastProvider><DownloadNotice /></ToastProvider>);
const emit = (detail: unknown) => act(() => { window.dispatchEvent(new CustomEvent("app-download", { detail })); });

describe("DownloadNotice", () => {
  it("says where a saved file went", async () => {
    show();
    emit({ state: "saved", name: "agent-session-1.md", path: "C:\Users\me\Downloads\agent-session-1.md" });
    expect(await screen.findByText("Saved to C:\Users\me\Downloads\agent-session-1.md")).toBeInTheDocument();
  });

  it("says why a file could not be saved", async () => {
    show();
    emit({ state: "failed", name: "audit.jsonl", reason: "the disk is full" });
    expect(await screen.findByText("audit.jsonl could not be saved: the disk is full")).toBeInTheDocument();
  });

  it("says nothing when the person closed the save window", () => {
    show();
    emit({ state: "cancelled", name: "x.md" });
    expect(screen.queryByText(/Saved|could not be saved/)).toBeNull();
  });

  it("ignores an event with no detail and stops listening when it goes", () => {
    const { unmount } = show();
    emit(null);
    unmount();
    emit({ state: "saved", name: "late.md", path: "/x/late.md" });
    expect(screen.queryByText(/late.md/)).toBeNull();
  });
});
