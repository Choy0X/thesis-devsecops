import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, api: { gpuMode: vi.fn() } };
});
import { api, type GpuMode } from "./api";
import { MODE_WORDS, POLL_MS, modeTitle, useGpuMode } from "./gpuMode";

const serve: GpuMode = { mode: "serve", holders: [{ mode: "serve", what: "the local model server (a.gguf)", stoppable: true }] };

function Probe({ enabled = true }: { enabled?: boolean }) {
  const m = useGpuMode(enabled, 100);
  return <p>{m ? MODE_WORDS[m.mode] : "unknown"}</p>;
}

beforeEach(() => { vi.mocked(api.gpuMode).mockReset(); });

describe("the words", () => {
  it("names each mode and says what holds the card", () => {
    expect(Object.values(MODE_WORDS)).toEqual(["GPU idle", "GPU training", "GPU serving", "GPU indexing"]);
    expect(modeTitle(null)).toBe("The graphics card's state is not known yet");
    expect(modeTitle({ mode: "idle", holders: [] })).toBe("No model is using the graphics card");
    expect(modeTitle({ mode: "train", holders: [{ mode: "train", what: "a training", stoppable: false }, { mode: "serve", what: "the server", stoppable: true }] })).toBe("The graphics card is in use by a training and the server");
    expect(POLL_MS).toBeGreaterThanOrEqual(1000);
  });
});

describe("useGpuMode", () => {
  it("reads the mode, reads it again later, and keeps the last answer when a read fails", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.gpuMode).mockResolvedValue(serve);
      render(<Probe />);
      expect(await screen.findByText("GPU serving")).toBeInTheDocument();
      vi.mocked(api.gpuMode).mockResolvedValue({ mode: "idle", holders: [] });
      await act(async () => { await vi.advanceTimersByTimeAsync(150); });
      expect(screen.getByText("GPU idle")).toBeInTheDocument();
      vi.mocked(api.gpuMode).mockImplementation(() => Promise.reject(new Error("down")));
      await act(async () => { await vi.advanceTimersByTimeAsync(250); });
      expect(screen.getByText("GPU idle")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not read at all while it is not enabled", async () => {
    render(<Probe enabled={false} />);
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(api.gpuMode).not.toHaveBeenCalled();
    expect(screen.getByText("unknown")).toBeInTheDocument();
  });
});
