// A small terminal screen: what a program that prints into a pipe would show on a real terminal.
//
// The page cannot print the raw output of a job. A progress bar redraws itself with a carriage return, a library moves the cursor up to update two bars at once, colours
// arrive as escape codes, and a cursor that is hidden and shown again is not text at all. `Screen` reads that output and keeps the rows a terminal would show.
//
// Understood: printable characters (written at the cursor, over what is there), tab (to the next multiple of 8), line feed (a new row, column 0), carriage return, backspace,
// ESC [ n A B C D E F G (cursor), K (erase in line, 0 1 2), J (erase from the cursor to the end, 0), and m (bold, dim, the eight colours; bright and 256-colour indexes below 16
// map to the same eight hues; anything else is ignored). Everything else (cursor hide and show, titles, other escapes, other control characters) is read and dropped, never printed.
//
// Not emulated: hard wrapping at a number of columns (a long row is one row; the box wraps it), alternate screens, background colours, input. A cursor move cannot reach a row older
// than the ones kept; a move up into rows that were dropped is ignored. Surrogate pairs count as two columns (a column is a UTF-16 unit).

export type Hue = "black" | "red" | "green" | "yellow" | "blue" | "magenta" | "cyan" | "white";
export type Run = { text: string; fg: Hue | null; bold: boolean; dim: boolean };
/** One row of the screen. `id` never changes and is never reused; `rev` changes only when the row's content does, so a row can be drawn again only when it did. */
export type Row = { id: number; runs: readonly Run[]; text: string; rev: number };

export const KEEP_ROWS = 20_000;
const MAX_JUMP = 1000;            // the furthest a cursor movement goes right (a stray "move right 999999999" must not make a huge row)
const MAX_CARRY = 4096;           // the longest unfinished escape sequence kept for the next write

const HUES: readonly Hue[] = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"];
const BOLD = 16;
const DIM = 32;
const FG = 15;                    // the colour is the low four bits: 0 none, 1 to 8 a hue

type Line = { id: number; text: string; sty: number[] | null; rev: number; view: Row | null };      // `sty` is one style code per unit of `text`, null while every unit has style 0

const isParam = (c: number) => c >= 0x30 && c <= 0x3f;
const isInter = (c: number) => c >= 0x20 && c <= 0x2f;
const isFinal = (c: number) => c >= 0x40 && c <= 0x7e;
const isBreak = (c: number) => c < 0x20 || c === 0x7f;

export class Screen {
  private lines: Line[] = [];
  private nextId = 0;
  private dropped = 0;                  // rows that were thrown away from the far end
  private row = 0;                      // the cursor's row, an index into `lines`; lines.length means "the row below the last one, not made yet"
  private col = 0;
  private style = 0;
  private carry = "";                   // the start of an escape sequence that the last write ended in the middle of

  constructor(private readonly keep: number = KEEP_ROWS) {}

  /** How many rows are kept. */
  get length(): number { return this.lines.length; }
  /** How many rows were dropped from the far end. */
  get droppedRows(): number { return this.dropped; }

  /** Forget everything and start from an empty screen. */
  reset() {
    this.lines = [];
    this.nextId = 0;
    this.dropped = 0;
    this.row = 0;
    this.col = 0;
    this.style = 0;
    this.carry = "";
  }

  /** One entry of a job's log: the text, then the end of the line unless the entry ends with a carriage return (the next entry is drawn over this row). */
  entry(text: string) {
    this.entries([text]);
  }

  /** Several entries in a row; the rows beyond the limit are dropped once, at the end. */
  entries(texts: readonly string[]) {
    for (const text of texts) {
      this.read(text);
      if (!text.endsWith("\r")) this.read("\n");
    }
    this.trim();
  }

  /** Read output as a terminal would. A sequence cut in two by the end of `input` is finished by the next call. */
  write(input: string) {
    this.read(input);
    this.trim();
  }

  private read(input: string) {
    const s = this.carry + input;
    this.carry = "";
    const n = s.length;
    let i = 0;
    while (i < n) {
      const c = s.charCodeAt(i);
      if (c === 0x1b) {
        const next = this.escape(s, i);
        if (next < 0) {                                   // unfinished: keep it for the next write, unless it is absurdly long
          if (n - i <= MAX_CARRY) this.carry = s.slice(i);
          break;
        }
        i = next;
      } else if (c === 0x0a) { this.lineFeed(); i++; }
      else if (c === 0x0d) { this.col = 0; i++; }
      else if (c === 0x08) { this.col = Math.max(0, this.col - 1); i++; }
      else if (c === 0x09) { this.col = Math.min(this.col + 8 - (this.col % 8), MAX_JUMP); i++; }
      else if (isBreak(c)) i++;                           // bell, null, record separators and the like are not text
      else {
        let j = i + 1;
        while (j < n && !isBreak(s.charCodeAt(j))) j++;
        this.put(s.slice(i, j));
        i = j;
      }
    }
  }

  /** The last `count` rows (all of them without a count). A row that did not change keeps the same object. */
  tail(count = Infinity): Row[] {
    const from = Math.max(0, this.lines.length - count);
    const out: Row[] = [];
    for (let k = from; k < this.lines.length; k++) out.push(this.viewOf(this.lines[k]));
    return out;
  }

  rows(): Row[] { return this.tail(); }

  // -- escape sequences ------------------------------------------------------------------------------------------------------------------------------

  /** Handle the escape sequence that starts at `at`; returns the index after it, or -1 when the input ends inside it. */
  private escape(s: string, at: number): number {
    const n = s.length;
    if (at + 1 >= n) return -1;
    const d = s.charCodeAt(at + 1);
    if (d === 0x5b) {                                      // ESC [ : a control sequence
      let j = at + 2;
      while (j < n && isParam(s.charCodeAt(j))) j++;
      const paramsEnd = j;
      while (j < n && isInter(s.charCodeAt(j))) j++;
      if (j >= n) return -1;
      if (!isFinal(s.charCodeAt(j))) return j;             // not a valid sequence: drop what was read, go on at the odd character
      this.control(s.slice(at + 2, paramsEnd), s[j], j > paramsEnd);
      return j + 1;
    }
    if (d === 0x5d) {                                      // ESC ] : an operating-system command (a title, a link) ends with BEL or ESC \
      for (let j = at + 2; j < n; j++) {
        const c = s.charCodeAt(j);
        if (c === 0x07) return j + 1;
        if (c === 0x1b) return j + 1 < n ? (s.charCodeAt(j + 1) === 0x5c ? j + 2 : j) : -1;      // ESC \ ends it; another ESC starts the next sequence
      }
      return n - at > MAX_CARRY ? n : -1;                  // never ended: wait for more, or give up on a huge one
    }
    if (isInter(d)) {                                      // ESC ( B and the like: intermediate characters, then one final
      let j = at + 1;
      while (j < n && isInter(s.charCodeAt(j))) j++;
      return j >= n ? -1 : j + 1;
    }
    return at + 2;                                         // a two-character escape (save and restore the cursor, keypad modes): nothing to show
  }

  private control(params: string, final: string, hasIntermediate: boolean) {
    if (hasIntermediate || /^[<=>?]/.test(params)) return;                         // private modes (cursor hide and show, bracketed paste) and odd forms
    const nums = params === "" ? [] : params.split(/[;:]/).map((p) => (p === "" ? NaN : parseInt(p, 10)));
    const n = (k = 0) => (Number.isFinite(nums[k]) && nums[k] > 0 ? nums[k] : 1);   // the count of a movement; 0 or missing means 1
    switch (final) {
      case "A": this.moveRow(-n()); break;
      case "B": this.moveRow(n()); break;
      case "E": this.moveRow(n()); this.col = 0; break;
      case "F": this.moveRow(-n()); this.col = 0; break;
      case "C": this.col = Math.min(this.col + n(), Math.max(MAX_JUMP, this.lineLength())); break;
      case "D": this.col = Math.max(0, this.col - n()); break;
      case "G": this.col = Math.min(n() - 1, Math.max(MAX_JUMP, this.lineLength())); break;
      case "K": this.eraseInLine(Number.isFinite(nums[0]) ? nums[0] : 0); break;
      case "J": if (!Number.isFinite(nums[0]) || nums[0] === 0) this.eraseBelow(); break;
      case "m": this.sgr(nums); break;
      default: break;                                      // everything else is dropped
    }
  }

  private sgr(nums: number[]) {
    const all = nums.length ? nums : [0];
    for (let k = 0; k < all.length; k++) {
      const v = Number.isNaN(all[k]) ? 0 : all[k];
      if (v === 0) this.style = 0;
      else if (v === 1) this.style |= BOLD;
      else if (v === 2) this.style |= DIM;
      else if (v === 22) this.style &= ~(BOLD | DIM);
      else if (v >= 30 && v <= 37) this.style = (this.style & ~FG) | (v - 29);
      else if (v >= 90 && v <= 97) this.style = (this.style & ~FG) | (v - 89);
      else if (v === 39) this.style &= ~FG;
      else if (v === 38 || v === 48 || v === 58) {          // an extended colour: read its parameters so they are not taken for others
        const kind = all[k + 1];
        if (kind === 5) {
          if (v === 38) this.style = (this.style & ~FG) | hueOfIndex(all[k + 2]);
          k += 2;
        } else if (kind === 2) {
          if (v === 38) this.style &= ~FG;                  // a true colour cannot be matched to the theme: the default colour keeps the contrast
          k += 4;
        } else k += 1;
      }
    }
  }

  // -- the screen ------------------------------------------------------------------------------------------------------------------------------------

  private lineLength(): number { return this.row < this.lines.length ? this.lines[this.row].text.length : 0; }

  private moveRow(by: number) {
    const target = this.row + by;
    if (target < 0) { if (this.dropped === 0) this.row = 0; return; }       // above the first row: stop there, unless that row was thrown away (then the move is ignored)
    this.row = Math.min(target, this.lines.length);
  }

  private lineFeed() {
    this.ensureRow();
    this.row++;
    this.col = 0;
  }

  /** The row under the cursor, made if the cursor is below the last one. */
  private ensureRow(): Line {
    if (this.row >= this.lines.length) {
      this.lines.push({ id: this.nextId++, text: "", sty: null, rev: 0, view: null });
      this.row = this.lines.length - 1;
    }
    return this.lines[this.row];
  }

  /** Drop the oldest rows beyond the limit (once per write, not once per row, so a long output costs one move of the array). */
  private trim() {
    const extra = this.lines.length - this.keep;
    if (extra <= 0) return;
    this.lines.splice(0, extra);
    this.dropped += extra;
    this.row = Math.max(0, this.row - extra);
  }

  private touch(line: Line) {
    line.rev++;
    line.view = null;
  }

  private put(chunk: string) {
    const line = this.ensureRow();
    const col = this.col;
    if (col > line.text.length) {                           // writing past the end of the row: the gap is blank
      const gap = col - line.text.length;
      line.text += " ".repeat(gap);
      if (line.sty) for (let g = 0; g < gap; g++) line.sty.push(0);
    }
    const before = line.text.length;
    const end = col + chunk.length;
    line.text = line.text.slice(0, col) + chunk + line.text.slice(end);
    if (this.style !== 0 && !line.sty) line.sty = new Array<number>(before).fill(0);      // styles are kept from the first styled write on; what was written before is plain
    if (line.sty) {                                         // the styles stay one per unit of text: before the chunk, the chunk's own, then what the chunk did not overwrite
      const after = line.sty.slice(end);
      line.sty.length = col;
      for (let k = 0; k < chunk.length; k++) line.sty.push(this.style);
      for (const code of after) line.sty.push(code);
    }
    this.col = end;
    this.touch(line);
  }

  private eraseInLine(mode: number) {
    if (this.row >= this.lines.length) return;                // nothing there to erase, and an empty row is not made for it
    const line = this.lines[this.row];
    if (mode === 0) {
      if (this.col >= line.text.length) return;
      line.text = line.text.slice(0, this.col);
      if (line.sty) line.sty.length = line.text.length;
    } else if (mode === 1) {
      const upto = Math.min(this.col + 1, line.text.length);
      line.text = " ".repeat(upto) + line.text.slice(upto);
      if (line.sty) line.sty.fill(0, 0, upto);
    } else if (mode === 2) {
      line.text = "";
      line.sty = null;
    } else return;
    this.touch(line);
  }

  private eraseBelow() {
    this.eraseInLine(0);
    if (this.row + 1 < this.lines.length) this.lines.splice(this.row + 1);
  }

  private viewOf(line: Line): Row {
    if (line.view) return line.view;
    const text = line.text.trimEnd();
    const runs: Run[] = [];
    if (text) {
      const sty = line.sty;
      let from = 0;
      for (let k = 1; k <= text.length; k++) {
        if (k === text.length || (sty && sty[k] !== sty[from])) {
          const code = sty ? sty[from] : 0;
          const fg = code & FG;
          runs.push({ text: text.slice(from, k), fg: fg ? HUES[fg - 1] : null, bold: (code & BOLD) !== 0, dim: (code & DIM) !== 0 });
          from = k;
        }
      }
    }
    line.view = { id: line.id, runs, text, rev: line.rev };
    return line.view;
  }
}

function hueOfIndex(index: number): number {
  if (!Number.isFinite(index)) return 0;
  if (index >= 0 && index < 8) return index + 1;
  if (index >= 8 && index < 16) return index - 7;
  return 0;                                                 // the colour cube and the greys: the default colour, which keeps the contrast
}
