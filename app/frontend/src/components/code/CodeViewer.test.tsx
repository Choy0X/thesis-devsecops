import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { canMountEditor, CodeViewer } from "./CodeViewer";
import CodeViewerImpl from "./CodeViewerImpl";

// jsdom cannot measure text (no Range.getClientRects), so CodeViewer shows its fallback there, which is what the other tests of the interface read. To see the editor itself the DOM is given
// the few measurements CodeMirror asks for.

function giveTheDomAMeasure() {
  const rect = { x: 0, y: 0, top: 0, left: 0, right: 8, bottom: 20, width: 8, height: 20, toJSON: () => ({}) };
  vi.stubGlobal("Range", class { setStart() {} setEnd() {} getClientRects() { return [rect]; } getBoundingClientRect() { return rect; } });
  Object.defineProperty(document, "createRange", { configurable: true, value: () => new (globalThis.Range as any)() });
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("CodeViewer", () => {
  it("shows its fallback where the DOM cannot measure text", () => {
    expect(canMountEditor()).toBe(false);
    render(<CodeViewer code="x = 1" path="a.py" label="a.py" fallback={<pre>1: x = 1</pre>} />);
    expect(screen.getByText("1: x = 1")).toBeInTheDocument();
    expect(document.querySelector(".cm-editor")).toBeNull();
  });

  it("is the editor where it can run: read-only, numbered from the line the excerpt began at, the text as it is", async () => {
    giveTheDomAMeasure();
    expect(canMountEditor()).toBe(true);
    render(<CodeViewerImpl code={"import os\nprint(os.name)\n"} path="tool.py" startLine={40} marked={[41]} label="tool.py, from line 40" />);
    const editor = await waitFor(() => { const e = document.querySelector(".cm-editor"); expect(e).not.toBeNull(); return e!; });
    expect(editor.textContent).toContain("import os");
    expect(editor.textContent).toContain("print(os.name)");
    const numbers = [...editor.querySelectorAll(".cm-lineNumbers .cm-gutterElement")].map((n) => n.textContent).filter(Boolean);
    expect(numbers.slice(-3)).toEqual(["40", "41", "42"]);                              // (the first element of a gutter is CodeMirror's own hidden spacer, as wide as the widest number)
    expect(numbers).not.toContain("1");
    const content = screen.getByRole("group", { name: "tool.py, from line 40" });
    expect(content).toHaveAttribute("aria-readonly", "true");
    expect(content).toHaveAttribute("tabindex", "0");
    expect(content.getAttribute("contenteditable")).not.toBe("true");
    expect(editor.querySelectorAll(".cm-line.cm-hit")).toHaveLength(1);                 // the marked line
  });
});
