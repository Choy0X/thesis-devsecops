import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LogBuffer, type LogLine } from "../lib/logBuffer";
import { JobTerminal, statusLine } from "./JobTerminal";

const rowsOf = (...entries: string[]): LogLine[] => { const b = new LogBuffer(); b.push(entries); return b.window(); };
const noop = () => undefined;

function terminal(over: Partial<React.ComponentProps<typeof JobTerminal>> = {}) {
  return <JobTerminal title="Download a model" rows={rowsOf("one", "two")} earlier={0} onLoadEarlier={noop} status="running" {...over} />;
}

/** jsdom has no layout: give the scroll box the sizes a browser would. */
function layout(el: HTMLElement, sizes: { scrollHeight: number; clientHeight: number; scrollTop?: number }) {
  Object.defineProperty(el, "scrollHeight", { value: sizes.scrollHeight, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: sizes.clientHeight, configurable: true });
  el.scrollTop = sizes.scrollTop ?? 0;
}

afterEach(() => { vi.restoreAllMocks(); });

describe("JobTerminal", () => {
  it("draws the rows in order, in a log region that has a name, can be focused and does not announce each row", () => {
    render(terminal());
    const log = screen.getByRole("log", { name: "Output of Download a model" });
    expect(log).toHaveAttribute("tabindex", "0");
    expect(log).toHaveAttribute("aria-live", "off");
    expect(Array.from(log.children).map((c) => c.textContent)).toEqual(["one", "two"]);
  });

  it("names the log as asked", () => {
    render(terminal({ ariaLabel: "Output of the process" }));
    expect(screen.getByRole("log", { name: "Output of the process" })).toBeInTheDocument();
  });

  it("says what it is, what runs, in what state and how many lines it holds, the earlier ones included", () => {
    const { rerender } = render(terminal({ earlier: 5 }));
    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(screen.getByText("Download a model")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("7 lines")).toBeInTheDocument();
    rerender(terminal({ rows: rowsOf("only"), status: "done" }));
    expect(screen.getByText("1 line")).toBeInTheDocument();
    expect(screen.getByText("finished")).toBeInTheDocument();
    for (const [status, word] of [["error", "failed"], ["cancelled", "stopped"], ["interrupted", "cut off"]]) {
      rerender(terminal({ status }));
      expect(screen.getByText(word)).toBeInTheDocument();
    }
  });

  it("keeps the colours a program wrote and gives a plain row the colour of what it says", () => {
    render(terminal({ rows: rowsOf("\x1b[32mok\x1b[0m and \x1b[1;31mbad\x1b[0m", "Traceback (most recent call last)", "all quiet") }));
    const log = screen.getByRole("log");
    expect(screen.getByText("ok")).toHaveClass("text-ok");
    expect(screen.getByText("bad")).toHaveClass("text-crit", "font-semibold");
    expect(screen.getByText("and")).toHaveClass("text-muted");
    expect(log.children[1]).toHaveClass("text-crit");
    expect(log.children[2]).toHaveClass("text-muted");
  });

  it("draws dim text faint, unless it has a colour of its own", () => {
    render(terminal({ rows: rowsOf("\x1b[2mquiet\x1b[0m \x1b[2;32mquiet green\x1b[0m") }));
    expect(screen.getByText("quiet")).toHaveClass("text-faint");
    expect(screen.getByText("quiet green")).toHaveClass("text-ok");
  });

  it("shows no escape code and draws a redraw once", () => {
    render(terminal({ rows: rowsOf("\x1b[2Kbar 10%\r", "bar 100%") }));
    const log = screen.getByRole("log");
    expect(log.children).toHaveLength(1);
    expect(log.textContent).toBe("bar 100%");
  });

  it("says it waits while a job runs and has written nothing, and that there is none once it is over", () => {
    const { rerender } = render(terminal({ rows: [], status: "running" }));
    expect(screen.getByText("waiting for output...")).toBeInTheDocument();
    rerender(terminal({ rows: [], status: "done" }));
    expect(screen.getByText("no output")).toBeInTheDocument();
    rerender(terminal({ rows: [], status: "done", none: "No output is kept for this run." }));
    expect(screen.getByText("No output is kept for this run.")).toBeInTheDocument();
  });

  it("offers to load earlier rows and asks for them", () => {
    const more = vi.fn();
    render(terminal({ earlier: 1200, onLoadEarlier: more }));
    fireEvent.click(screen.getByRole("button", { name: "Load earlier lines (1,200 more)" }));
    expect(more).toHaveBeenCalledTimes(1);
  });

  it("has no earlier-rows button when there are none", () => {
    render(terminal());
    expect(screen.queryByRole("button", { name: /Load earlier/ })).toBeNull();
  });
});

describe("JobTerminal: status line", () => {
  it("reads the transfer as one line while it runs and draws a steady cursor after it", () => {
    const p = { state: "downloading", unit: "bytes" as const, processed: 4 * 1024 ** 2, total: 6.5 * 1024 ** 2, rate: 2.4 * 1024 ** 2, eta: 1 };
    const { container } = render(terminal({ progress: p }));
    expect(container.querySelector(".border-t")).toHaveTextContent("61% 4.0 MB of 6.5 MB 2.4 MB/s 0:01 left");
    expect(container.querySelector('[aria-hidden="true"].inline-block')).not.toBeNull();
  });

  it("is not shown once the job is over, nor for a job that does not count bytes", () => {
    const p = { state: "downloading", unit: "bytes" as const, processed: 10, total: 100 };
    const { container, rerender } = render(terminal({ progress: p, status: "done" }));
    expect(screen.queryByText(/of 100 B/)).toBeNull();
    expect(container.querySelector(".border-t")).toBeNull();                   // no status line at all, and no cursor, once the job is over
    rerender(terminal({ progress: { state: "mining", processed: 10, total: 100 }, status: "running" }));
    expect(screen.queryByText(/10%/)).toBeNull();
  });

  it("is a function of the report: percent, sizes, speed and time left, each only when known", () => {
    expect(statusLine(null)).toBe("");
    expect(statusLine({ unit: "files", processed: 1, total: 2 })).toBe("");
    expect(statusLine({ unit: "bytes", processed: 5, total: 0 })).toBe("");
    expect(statusLine({ unit: "bytes", processed: 512, total: 1024 })).toBe("50%  512 B of 1 kB");
    expect(statusLine({ unit: "bytes", processed: 1024 ** 2, total: 2 * 1024 ** 2, rate: 1024 ** 2, eta: 75 })).toBe("50%  1.0 MB of 2.0 MB  1.0 MB/s  1:15 left");
    expect(statusLine({ unit: "bytes", processed: 100, total: 100, rate: 50, eta: 0 })).toBe("100%  100 B of 100 B  50 B/s");
    expect(statusLine({ unit: "bytes", processed: 150, total: 100 })).toBe("100%  150 B of 100 B");
  });
});

describe("JobTerminal: scrolling", () => {
  it("follows the newest row while the reader is at the end", () => {
    const { rerender } = render(terminal());
    const log = screen.getByRole("log");
    layout(log, { scrollHeight: 500, clientHeight: 200 });
    rerender(terminal({ rows: rowsOf("one", "two", "three") }));
    expect(log.scrollTop).toBe(500);
  });

  it("stops following when the reader scrolls up, shows a way back, and does not move their place", () => {
    const { rerender } = render(terminal());
    const log = screen.getByRole("log");
    expect(screen.queryByRole("button", { name: /Jump to latest/ })).toBeNull();
    layout(log, { scrollHeight: 500, clientHeight: 200, scrollTop: 100 });
    fireEvent.scroll(log);
    expect(screen.getByRole("button", { name: /Jump to latest/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute("aria-pressed", "false");
    rerender(terminal({ rows: rowsOf("one", "two", "three") }));
    expect(log.scrollTop).toBe(100);
  });

  it("goes back to the end and follows again from Jump to latest", () => {
    render(terminal());
    const log = screen.getByRole("log");
    layout(log, { scrollHeight: 500, clientHeight: 200, scrollTop: 100 });
    fireEvent.scroll(log);
    fireEvent.click(screen.getByRole("button", { name: /Jump to latest/ }));
    expect(log.scrollTop).toBe(500);
    expect(screen.queryByRole("button", { name: /Jump to latest/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute("aria-pressed", "true");
  });

  it("follows again when the reader scrolls back to the end by hand", () => {
    render(terminal());
    const log = screen.getByRole("log");
    layout(log, { scrollHeight: 500, clientHeight: 200, scrollTop: 100 });
    fireEvent.scroll(log);
    log.scrollTop = 290;                                     // within a few pixels of the end (500 - 200)
    fireEvent.scroll(log);
    expect(screen.queryByRole("button", { name: /Jump to latest/ })).toBeNull();
  });

  it("lets the reader stay where they are with the Follow control, even at the end", () => {
    const { rerender } = render(terminal());
    const log = screen.getByRole("log");
    layout(log, { scrollHeight: 500, clientHeight: 200, scrollTop: 300 });
    fireEvent.click(screen.getByRole("button", { name: "Follow" }));
    expect(screen.getByRole("button", { name: "Follow" })).toHaveAttribute("aria-pressed", "false");
    layout(log, { scrollHeight: 700, clientHeight: 200, scrollTop: 300 });
    rerender(terminal({ rows: rowsOf("one", "two", "three") }));
    expect(log.scrollTop).toBe(300);
    fireEvent.click(screen.getByRole("button", { name: "Follow" }));
    expect(log.scrollTop).toBe(700);
  });

  it("keeps the row the reader was looking at where it was when older rows are added on top", () => {
    const { rerender } = render(terminal({ earlier: 50 }));
    const log = screen.getByRole("log");
    layout(log, { scrollHeight: 300, clientHeight: 200, scrollTop: 0 });
    fireEvent.scroll(log);                                   // at the top: not at the end
    fireEvent.click(screen.getByRole("button", { name: /Load earlier lines/ }));
    layout(log, { scrollHeight: 700, clientHeight: 200, scrollTop: 0 });
    rerender(terminal({ earlier: 0, rows: rowsOf("a", "b", "c", "d") }));
    expect(log.scrollTop).toBe(400);                         // 700 - 300: the same rows are in view
  });
});

describe("JobTerminal: Copy", () => {
  const clipboard = (writeText: (text: string) => Promise<void>) => Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });

  it("puts every kept row on the clipboard as plain text when it is given the text, and says so", async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    clipboard(write);
    render(terminal({ copyText: () => "all\nthe\nrows" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy the output of Download a model" }));
    await waitFor(() => expect(screen.getByText("Copied")).toBeInTheDocument());
    expect(write).toHaveBeenCalledWith("all\nthe\nrows");
  });

  it("copies the rows on screen when it is given no text", async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    clipboard(write);
    render(terminal({ rows: rowsOf("\x1b[31mred\x1b[0m", "plain") }));
    fireEvent.click(screen.getByRole("button", { name: /Copy the output/ }));
    await waitFor(() => expect(write).toHaveBeenCalledWith("red\nplain"));
  });

  it("says so when the clipboard refuses, and goes back to Copy after a moment", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    clipboard(vi.fn().mockRejectedValue(new Error("denied")));
    render(terminal());
    fireEvent.click(screen.getByRole("button", { name: /Copy the output/ }));
    await waitFor(() => expect(screen.getByText("Not copied")).toBeInTheDocument());
    act(() => { vi.advanceTimersByTime(3000); });
    expect(screen.getByText("Copy")).toBeInTheDocument();
    vi.useRealTimers();
  });
});
