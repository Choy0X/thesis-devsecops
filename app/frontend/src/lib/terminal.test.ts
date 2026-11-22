import { describe, expect, it } from "vitest";
import fixture from "./hfDownloadLog.fixture.json";
import { KEEP_ROWS, Screen } from "./terminal";

const ESC = "\x1b";
const texts = (s: Screen) => s.rows().map((r) => r.text);
const screenOf = (entries: string[], keep?: number) => { const s = new Screen(keep); s.entries(entries); return s; };
const writeAll = (...chunks: string[]) => { const s = new Screen(); chunks.forEach((c) => s.write(c)); return s; };

describe("Screen: lines", () => {
  it("keeps one row per line and a blank line as a blank row", () => {
    expect(texts(screenOf(["first", "", "third"]))).toEqual(["first", "", "third"]);
  });

  it("shows nothing for a screen that was never written to, and no empty row below the last line", () => {
    expect(new Screen().rows()).toEqual([]);
    expect(texts(writeAll("a\nb\n"))).toEqual(["a", "b"]);
  });

  it("starts a new row at column 0 on a line feed", () => {
    expect(texts(writeAll("abc\n  d"))).toEqual(["abc", "  d"]);
  });

  it("trims blanks at the end of a row and keeps the ones inside", () => {
    expect(texts(screenOf(["a   b    "]))).toEqual(["a   b"]);
  });

  it("expands a tab to the next multiple of 8 and lets a backspace be overwritten", () => {
    expect(texts(screenOf(["a\tb", "12345678\tx", "abc\b\bXY"]))).toEqual(["a" + " ".repeat(7) + "b", "12345678" + " ".repeat(8) + "x", "aXY"]);
  });
});

describe("Screen: redraws", () => {
  it("draws over the row on a carriage return, the way a progress bar updates itself", () => {
    expect(texts(screenOf(["bar  0%\r", "bar 50%\r", "bar 100%"]))).toEqual(["bar 100%"]);
  });

  it("goes on below a bar once the bar ends its line", () => {
    expect(texts(screenOf(["a 1%\r", "a 100%", "next"]))).toEqual(["a 100%", "next"]);
  });

  it("leaves the tail of a longer row that a shorter text did not cover, as a terminal does", () => {
    expect(texts(screenOf(["downloading 100%\r", "done"]))).toEqual(["doneloading 100%"]);
  });

  it("erases the rest of the row first when the program asks to", () => {
    expect(texts(writeAll(`downloading 100%\r${ESC}[Kdone\n`))).toEqual(["done"]);
  });

  it("reads the raw text of a tqdm bar written in one go", () => {
    expect(texts(writeAll("x: 0%|          | 0/10\rx: 50%|#####     | 5/10\rx: 100%|##########| 10/10\n"))).toEqual(["x: 100%|##########| 10/10"]);
  });

  it("moves up to update two bars at once", () => {
    const s = writeAll("bar1 10%\nbar2 20%\n", `${ESC}[2A\rbar1 60%\n`, "bar2 90%\n");
    expect(texts(s)).toEqual(["bar1 60%", "bar2 90%"]);
  });

  it("stops a move up at the first row and a move down at the row below the last", () => {
    const s = writeAll(`one\n${ESC}[9Ax`);                  // the cursor was on the row below "one"; it goes up to the first row, column 0
    expect(texts(s)).toEqual(["xne"]);
    s.write(`${ESC}[9B\ry`);                                    // down as far as it goes: the row below the last, where the text lands (no blank row between; the column is kept, so back to 0 first)
    expect(texts(s)).toEqual(["xne", "y"]);
  });

  it("erases in a line: to the end, to the start (the cursor's cell included) and the whole line", () => {
    expect(texts(writeAll(`abcdef\r${ESC}[3C${ESC}[K`))).toEqual(["abc"]);
    expect(texts(writeAll(`abcdef\r${ESC}[2C${ESC}[1K`))).toEqual(["   def"]);
    expect(texts(writeAll(`abcdef${ESC}[2K\rxy`))).toEqual(["xy"]);
    expect(texts(writeAll(`abcdef${ESC}[0K`))).toEqual(["abcdef"]);
  });

  it("makes no empty row when there is nothing to erase", () => {
    expect(writeAll(`${ESC}[2K${ESC}[K`).rows()).toEqual([]);
  });

  it("erases from the cursor to the end of the screen: the rest of the row and the rows below it", () => {
    const s = writeAll(`one\ntwo\nthree\n${ESC}[2A\r${ESC}[J`);       // up from below "three" to "two"
    expect(texts(s)).toEqual(["one", ""]);
    s.write("x");
    expect(texts(s)).toEqual(["one", "x"]);
  });

  it("moves to a column, left, and by whole lines", () => {
    expect(texts(writeAll(`abcdef\r${ESC}[4GX${ESC}[2D!`))).toEqual(["ab!Xef"]);      // column 4, X over d (now column 5), two left (column 3 is index 2), ! over c
    expect(texts(writeAll(`a\nb\nc\n${ESC}[3F${ESC}[1CZ`))).toEqual(["aZ", "b", "c"]);       // three lines up to the start of "a", one to the right
  });
});

describe("Screen: colour", () => {
  it("keeps colour and weight as runs of text", () => {
    const [row] = screenOf([`${ESC}[31mred${ESC}[0m plain ${ESC}[1;32mbold green${ESC}[22;39m end`]).rows();
    expect(row.text).toBe("red plain bold green end");
    expect(row.runs).toEqual([
      { text: "red", fg: "red", bold: false, dim: false },
      { text: " plain ", fg: null, bold: false, dim: false },
      { text: "bold green", fg: "green", bold: true, dim: false },
      { text: " end", fg: null, bold: false, dim: false },
    ]);
  });

  it("maps bright colours and the first 16 of the 256-colour indexes to the same eight hues, and anything else to the default colour", () => {
    const fg = (code: string) => screenOf([`${ESC}[${code}mx`]).rows()[0].runs[0].fg;
    expect([fg("91"), fg("38;5;2"), fg("38;5;12"), fg("38;5;200"), fg("38;2;10;20;30"), fg("44"), fg("1")]).toEqual(["red", "green", "blue", null, null, null, null]);
  });

  it("does not take the numbers of an extended colour for other codes", () => {
    const run = screenOf([`${ESC}[38;5;1;4mx`]).rows()[0].runs[0];          // colour index 1, then underline (ignored); the 1 is not "bold"
    expect(run).toEqual({ text: "x", fg: "red", bold: false, dim: false });
  });

  it("keeps a colour across rows until it is reset, and dim", () => {
    const s = screenOf([`${ESC}[2;33mone`, "two", `${ESC}[mthree`]);
    expect(s.rows().map((r) => r.runs.map((x) => [x.fg, x.dim]))).toEqual([[["yellow", true]], [["yellow", true]], [[null, false]]]);
  });

  it("styles only what was written when it overwrites the middle of a row", () => {
    const s = writeAll(`abcdef\r${ESC}[2C${ESC}[31mXY${ESC}[0m`);
    expect(s.rows()[0].runs.map((r) => [r.text, r.fg])).toEqual([["ab", null], ["XY", "red"], ["ef", null]]);
    s.write(`\r${ESC}[32mQ`);
    expect(s.rows()[0].runs.map((r) => [r.text, r.fg])).toEqual([["Q", "green"], ["b", null], ["XY", "red"], ["ef", null]]);
  });

  it("blanks the colour of the cells an erase clears", () => {
    const s = writeAll(`${ESC}[31mred text${ESC}[0m\r${ESC}[3C${ESC}[1K`);
    expect(s.rows()[0].runs).toEqual([{ text: "    ", fg: null, bold: false, dim: false }, { text: "text", fg: "red", bold: false, dim: false }]);
  });
});

describe("Screen: what is not text", () => {
  it("drops cursor hide and show, titles, links, charset selections, cursor positions and unknown sequences", () => {
    const s = screenOf([
      `${ESC}[?25lhi${ESC}[?25h`,
      `${ESC}]0;a title\x07x`,
      `${ESC}]8;;http://a${ESC}\\link${ESC}]8;;${ESC}\\`,
      `${ESC}(Bplain`,
      `${ESC}7s${ESC}8t`,
      `${ESC}[5;10Hhome${ESC}[2 q`,
      "bell\x07 and null\x00 here",
    ]);
    expect(texts(s)).toEqual(["hi", "x", "link", "plain", "st", "home", "bell and null here"]);
  });

  it("does not read a private sequence as a colour, even when it ends the way a colour does", () => {
    const run = screenOf([`${ESC}[>4;2mx`, `${ESC}[?31mfoo`]).rows();             // modifyOtherKeys and a private mode: not "dim" and not "red"
    expect(run.map((r) => r.runs.map((x) => [x.text, x.fg, x.dim]))).toEqual([[["x", null, false]], [["foo", null, false]]]);
  });

  it("drops the record separator of a progress report and other control characters, never printing them", () => {
    expect(texts(screenOf(["a\x1eb\x7fc"]))).toEqual(["abc"]);
  });

  it("finishes a sequence that one write ended in the middle of", () => {
    const s = writeAll("a", `${ESC}[3`, "1mred", ESC, "[0m!");
    expect(s.rows()[0].runs.map((r) => [r.text, r.fg])).toEqual([["a", null], ["red", "red"], ["!", null]]);
  });

  it("does not wait for ever for a sequence that never ends, and loses only that", () => {
    const s = new Screen();
    s.write(`${ESC}]0;${"x".repeat(10_000)}`);
    s.entry("after");
    expect(texts(s)).toEqual(["after"]);
  });

  it("limits a movement to the right so a stray number cannot make a huge row", () => {
    const s = writeAll(`${ESC}[999999999Cx`);
    expect(s.rows()[0].text.length).toBe(1001);
  });
});

describe("Screen: rows", () => {
  it("gives a row an id that does not change and is not used again after the rows below it are erased", () => {
    const s = screenOf(["a", "b", "c"]);
    expect(s.rows().map((r) => r.id)).toEqual([0, 1, 2]);
    s.write(`${ESC}[2A${ESC}[J`);                            // up to "b", erase from there: "c" goes, "b" is blank
    s.entries(["d", "e"]);                                    // "d" is written over the blank "b"; "e" is a new row
    expect(s.rows().map((r) => [r.id, r.text])).toEqual([[0, "a"], [1, "d"], [3, "e"]]);
  });

  it("changes `rev` and the row object only for the row that changed", () => {
    const s = screenOf(["fixed", "bar 1%\r"]);
    const [fixed, bar] = s.rows();
    s.entry("bar 2%\r");                                      // drawn over "bar 1%"
    const [fixed2, bar2] = s.rows();
    expect(fixed2).toBe(fixed);
    expect(bar2).not.toBe(bar);
    expect(bar2.text).toBe("bar 2%");
    expect(bar2.rev).toBeGreaterThan(bar.rev);
    expect(bar2.id).toBe(bar.id);
  });

  it("returns the last rows of a long output with `tail`", () => {
    const s = screenOf(Array.from({ length: 100 }, (_, i) => `line ${i}`));
    expect(s.tail(3).map((r) => r.text)).toEqual(["line 97", "line 98", "line 99"]);
    expect(s.tail(1000)).toHaveLength(100);
  });

  it("drops rows from the far end beyond the limit, and a move up into them is ignored", () => {
    const s = screenOf(Array.from({ length: 12 }, (_, i) => `row ${i}`), 5);
    expect(texts(s)).toEqual(["row 7", "row 8", "row 9", "row 10", "row 11"]);
    expect(s.droppedRows).toBe(7);
    expect(s.rows()[0].id).toBe(7);
    s.write(`${ESC}[9Ax`);          // would reach rows that are gone: ignored, so the x lands below the last row (and not over "row 7", which is where a clamped move would put it)
    expect(texts(s)).toEqual(["row 8", "row 9", "row 10", "row 11", "x"]);
  });

  it("holds a very long output within the limit", () => {
    const s = new Screen();
    s.entries(Array.from({ length: KEEP_ROWS + 500 }, (_, i) => `line ${i}`));
    expect(s.length).toBe(KEEP_ROWS);
    expect(s.rows()[0].text).toBe("line 500");
    expect(s.rows()[KEEP_ROWS - 1].text).toBe(`line ${KEEP_ROWS + 499}`);
  });

  it("starts again with reset", () => {
    const s = screenOf(["a", "b"]);
    s.reset();
    expect(s.rows()).toEqual([]);
    s.entry("c");
    expect(s.rows().map((r) => [r.id, r.text])).toEqual([[0, "c"]]);
  });
});

describe("Screen: the entries of a job's log", () => {
  it("replays a download the way a terminal showed it, with the bar kept to one row", () => {
    const s = screenOf([
      "Model     org/tiny",
      "Plan      7 files, 6.5 MB",
      `${ESC}[32mmodel.safetensors${ESC}[0m   0%|          | 0.00/6.5M\r`,
      `${ESC}[32mmodel.safetensors${ESC}[0m  62%|######    | 4.0M/6.5M\r`,
      `${ESC}[32mmodel.safetensors${ESC}[0m 100%|##########| 6.5M/6.5M`,
      "Finished  6.5 MB in 3.2 s",
    ]);
    expect(texts(s)).toEqual(["Model     org/tiny", "Plan      7 files, 6.5 MB", "model.safetensors 100%|##########| 6.5M/6.5M", "Finished  6.5 MB in 3.2 s"]);
    expect(s.rows()[2].runs[0]).toEqual({ text: "model.safetensors", fg: "green", bold: false, dim: false });
  });

  it("replays the log of a real download (tqdm bars at three positions, a library warning) with every redraw drawn over its bar and no debris", () => {
    const rows = screenOf(fixture.logs).rows();
    expect(fixture.logs.length).toBe(34);
    expect(rows.length).toBeLessThan(fixture.logs.length / 2);                                    // 34 entries, most of them redraws: a handful of rows
    for (const r of rows) {
      expect(r.text).not.toMatch(/[\u0000-\u0008\u000b-\u001f\u007f]/);                           // no escape, carriage return or other control character is shown
      expect(r.runs.map((x) => x.text).join("")).toBe(r.text);
    }
    const blocks = String.fromCharCode(0x2588).repeat(10);
    expect(rows.map((r) => r.text)).toContain(`Download complete: ${blocks}| 4.95MB,   ???B/s`);
    expect(rows[rows.length - 1].text).toMatch(/^trl-internal-testing\/tiny-random-LlamaForCausalLM@2c542e4731: 7 files, 0\.01 GB, in /);
  });

  it("is the same as writing the text with its line ends", () => {
    const entries = ["a", "b 1%\r", "b 2%\r", "b 3%", "", "c"];
    expect(screenOf(entries).rows()).toEqual(writeAll("a\nb 1%\rb 2%\rb 3%\n\nc\n").rows());
  });
});
