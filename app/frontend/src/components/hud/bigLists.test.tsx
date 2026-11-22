import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { tokenize } from "../../lib/highlight";
import { visibleWindow } from "../../lib/windowing";
import { DataTable, type Column } from "./DataTable";
import { DiffViewer, parseDiff } from "./DiffViewer";

describe("windowing", () => {
  it("draws the rows in view plus a few either side and stands in for the rest with spacers", () => {
    expect(visibleWindow(1000, 40, 0, 400)).toEqual({ start: 0, end: 16, before: 0, after: 984 * 40 });
    const mid = visibleWindow(1000, 40, 4000, 400);
    expect(mid.start).toBe(94);
    expect(mid.end - mid.start).toBeLessThan(30);
    expect(mid.before + (mid.end - mid.start) * 40 + mid.after).toBe(1000 * 40);
  });

  it("never reaches past the data", () => {
    const w = visibleWindow(10, 40, 100000, 400);
    expect(w.end).toBe(10);
    expect(w.start).toBeLessThanOrEqual(10);
    expect(visibleWindow(0, 40, 0, 400)).toEqual({ start: 0, end: 0, before: 0, after: 0 });
  });
});

type Row = { id: number; name: string; score: number | null };
const columns: Column<Row>[] = [
  { key: "name", label: "Name", sortValue: (r) => r.name },
  { key: "score", label: "Score", sortValue: (r) => r.score, align: "right" },
  { key: "id", label: "Id" },
];
const rows: Row[] = [{ id: 1, name: "beta", score: 5 }, { id: 2, name: "alpha", score: null }, { id: 3, name: "gamma", score: 9 }, { id: 4, name: "item10", score: 1 }, { id: 5, name: "item2", score: 2 }];
const names = () => screen.getAllByRole("row").slice(1).map((r) => within(r).getAllByRole("cell")[0].textContent);

describe("DataTable", () => {
  it("shows a header and one row per record", () => {
    render(<DataTable caption="Records" columns={columns} rows={rows} rowKey={(r) => r.id} />);
    expect(screen.getByRole("table", { name: "Records" })).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Name", "Score", "Id"]);
    expect(names()).toEqual(["beta", "alpha", "gamma", "item10", "item2"]);
  });

  it("sorts ascending, then descending, then back to the given order, and announces it", () => {
    render(<DataTable caption="Records" columns={columns} rows={rows} rowKey={(r) => r.id} />);
    const header = () => screen.getByRole("columnheader", { name: /Name/ });
    fireEvent.click(within(header()).getByRole("button"));
    expect(names()).toEqual(["alpha", "beta", "gamma", "item2", "item10"]);          // numbers inside text sort naturally
    expect(header()).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(within(header()).getByRole("button"));
    expect(names()).toEqual(["item10", "item2", "gamma", "beta", "alpha"]);
    expect(header()).toHaveAttribute("aria-sort", "descending");
    fireEvent.click(within(header()).getByRole("button"));
    expect(names()).toEqual(["beta", "alpha", "gamma", "item10", "item2"]);
    expect(header()).toHaveAttribute("aria-sort", "none");
  });

  it("puts empty values last whichever way it sorts", () => {
    render(<DataTable caption="Records" columns={columns} rows={rows} rowKey={(r) => r.id} />);
    const score = () => within(screen.getByRole("columnheader", { name: /Score/ })).getByRole("button");
    fireEvent.click(score());
    expect(names()[names().length - 1]).toBe("alpha");
    fireEvent.click(score());
    expect(names()[names().length - 1]).toBe("alpha");
  });

  it("leaves the order to the server when the sort is controlled", () => {
    const onSort = vi.fn();
    render(<DataTable caption="Records" columns={columns} rows={rows} rowKey={(r) => r.id} sort={{ key: "name", dir: "desc" }} onSortChange={onSort} />);
    expect(names()).toEqual(["beta", "alpha", "gamma", "item10", "item2"]);
    expect(screen.getByRole("columnheader", { name: /Name/ })).toHaveAttribute("aria-sort", "descending");
    fireEvent.click(within(screen.getByRole("columnheader", { name: /Name/ })).getByRole("button"));
    expect(onSort).toHaveBeenCalledWith(null);
  });

  it("opens a row with the keyboard when rows can be opened", () => {
    const open = vi.fn();
    render(<DataTable caption="Records" columns={columns} rows={rows} rowKey={(r) => r.id} onRowClick={open} />);
    const first = screen.getAllByRole("row")[1];
    first.focus();
    fireEvent.keyDown(first, { key: "Enter" });
    expect(open).toHaveBeenCalledWith(rows[0]);
  });

  it("says so when there is nothing to show", () => {
    render(<DataTable caption="Records" columns={columns} rows={[]} rowKey={(r) => r.id} empty="No records yet." />);
    expect(screen.getByText("No records yet.")).toBeInTheDocument();
  });

  it("keeps 20,000 rows to a few dozen in the document and renders them quickly", () => {
    const many: Row[] = Array.from({ length: 20_000 }, (_, i) => ({ id: i, name: `row ${i}`, score: i % 97 }));
    const started = performance.now();
    render(<DataTable caption="Many" columns={columns} rows={many} rowKey={(r) => r.id} />);
    const took = performance.now() - started;
    expect(screen.getAllByRole("row").length).toBeLessThan(40);
    expect(screen.getByRole("table")).toHaveAttribute("aria-rowcount", "20001");
    expect(took).toBeLessThan(1500);
  });
});

const DIFF = [
  "diff --git a/src/app.py b/src/app.py", "index 1111111..2222222 100644", "--- a/src/app.py", "+++ b/src/app.py",
  "@@ -10,4 +10,5 @@ def run():", " def run(cmd):", "-    os.system(cmd)", "+    subprocess.run([cmd])  # safe", "+    return True", " # end", "",
].join("\n");

describe("parseDiff", () => {
  it("numbers the lines from the hunk header and tells added, removed and unchanged lines apart", () => {
    const rows = parseDiff(DIFF);
    expect(rows.slice(0, 4).map((r) => r.kind)).toEqual(["meta", "meta", "meta", "meta"]);
    expect(rows[4]).toMatchObject({ kind: "hunk" });
    expect(rows.slice(5).map((r) => [r.kind, r.oldNo, r.newNo])).toEqual([["ctx", 10, 10], ["del", 11, null], ["add", null, 11], ["add", null, 12], ["ctx", 12, 13]]);
    expect(rows[6].text).toBe("    os.system(cmd)");
  });

  it("reads several hunks and the no-newline marker", () => {
    const rows = parseDiff("@@ -1,1 +1,1 @@\n-a\n+b\n\\ No newline at end of file\n@@ -20,2 +20,2 @@\n x\n-y\n+z\n");
    expect(rows.map((r) => r.kind)).toEqual(["hunk", "del", "add", "meta", "hunk", "ctx", "del", "add"]);
    expect(rows[5]).toMatchObject({ oldNo: 20, newNo: 20 });
  });
});

describe("parseDiff without a hunk header", () => {
  it("reads +, - and space lines as one hunk from line 1 and keeps a file header as meta", () => {
    const rows = parseDiff("--- a/f.py\n+++ b/f.py\n x\n-y\n+z\n");
    expect(rows.map((r) => r.kind)).toEqual(["meta", "meta", "ctx", "del", "add"]);
    expect(rows.slice(2).map((r) => [r.oldNo, r.newNo])).toEqual([[1, 1], [2, null], [null, 2]]);
  });

  it("does not take a removed line that starts with dashes for a file header", () => {
    expect(parseDiff("-- comment\n+x").map((r) => r.kind)).toEqual(["del", "add"]);
  });
});

describe("tokenize", () => {
  const spell = (line: string, lang: string) => tokenize(line, lang).map((t) => t.text).join("");
  it("spells the line exactly and finds comments, strings, numbers and keywords", () => {
    const line = 'def run(x):  return "a#b" + 42  # done';
    const tokens = tokenize(line, "python");
    expect(spell(line, "python")).toBe(line);
    expect(tokens.filter((t) => t.kind === "keyword").map((t) => t.text)).toEqual(["def", "return"]);
    expect(tokens.find((t) => t.kind === "string")?.text).toBe('"a#b"');
    expect(tokens.find((t) => t.kind === "number")?.text).toBe("42");
    expect(tokens[tokens.length - 1]).toEqual({ text: "# done", kind: "comment" });
  });

  it("knows slash comments in C-like languages and leaves a division alone", () => {
    const ts = tokenize("const a = b / c; // note", "typescript");
    expect(ts[ts.length - 1]).toEqual({ text: "// note", kind: "comment" });
    expect(tokenize("x = a / b", "go").some((t) => t.kind === "comment")).toBe(false);
  });

  it("colours strings and numbers even when the language is unknown", () => {
    const tokens = tokenize("call('x', 3)", "brainfuck");
    expect(tokens.some((t) => t.kind === "string") && tokens.some((t) => t.kind === "number") && !tokens.some((t) => t.kind === "keyword")).toBe(true);
  });
});

describe("DiffViewer", () => {
  it("shows the counts, the signs and the line numbers, and says which lines changed in words", () => {
    render(<DiffViewer diff={DIFF} language="python" />);
    expect(screen.getByText("2 added")).toBeInTheDocument();
    expect(screen.getByText("1 removed")).toBeInTheDocument();
    expect(screen.getAllByText("added:").length).toBe(2);
    expect(screen.getByText("removed:")).toBeInTheDocument();
  });

  it("tints an added line green and a removed line red, with the sign beside it", () => {
    const { container } = render(<DiffViewer diff={DIFF} language="python" />);
    const add = container.querySelector('[data-kind="add"]')!, del = container.querySelector('[data-kind="del"]')!;
    expect(add.className).toContain("bg-diff-add-bg");
    expect(add.className).toContain("border-diff-add");
    expect(del.className).toContain("bg-diff-del-bg");
    expect(del.className).toContain("border-diff-del");
    expect(add.textContent).toContain("+");
    expect(del.textContent).toContain("-");
  });

  it("still marks added and removed lines in text without a hunk header", () => {
    const { container } = render(<DiffViewer diff={"-old\n+new\n same"} />);
    expect(container.querySelectorAll('[data-kind="add"]').length).toBe(1);
    expect(container.querySelectorAll('[data-kind="del"]').length).toBe(1);
  });

  it("keeps a diff of 20,000 lines to a few dozen lines in the document", () => {
    const lines = ["@@ -1,20000 +1,20000 @@", ...Array.from({ length: 20_000 }, (_, i) => (i % 3 === 0 ? `-old ${i}` : i % 3 === 1 ? `+new ${i}` : ` same ${i}`))];
    const started = performance.now();
    const { container } = render(<DiffViewer diff={lines.join("\n")} language="python" />);
    const took = performance.now() - started;
    expect(container.querySelectorAll("[data-kind]").length).toBeLessThan(60);
    expect(took).toBeLessThan(1500);
  });

  it("says so when there is no change", () => {
    render(<DiffViewer diff="" />);
    expect(screen.getByText("There is no change to show.")).toBeInTheDocument();
  });
});
