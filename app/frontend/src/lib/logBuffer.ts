// The output of one job, kept so a console can show the latest rows cheaply.
//
// The entries of a job's log are fed to a terminal screen (lib/terminal.ts), so a progress bar that redraws itself is one row, colours are kept and no escape code is shown. Each
// row gets a number that never changes (and a revision that changes only when its content does), so a row keeps its identity as new output arrives and only the rows that
// changed are drawn. A reader draws only the last `count` rows (VIEW by default); the rest (up to KEEP) wait behind "load earlier". The buffer is shared by every console that
// follows the same job, so each reader asks for its own window.

import { KEEP_ROWS, Screen, type Row, type Run } from "./terminal";

export type Tone = "error" | "warn" | "ok" | "plain";
/** One row of a job's output. `runs` are the pieces of text with their colour; `rev` changes only when the row's content does. */
export type LogLine = { id: number; text: string; tone: Tone; runs: readonly Run[]; rev: number };

export const VIEW = 300;
export const KEEP = KEEP_ROWS;

const WARNING_LINE = /^\s*warn(ing)?\b/i;                    // a line that says it is a warning is one, whatever words it uses ("... higher rate limits ...")
const ERROR = /error|fail|refus|trace|rate limit/i;
const WARN = /warn|skip|pending|note|waiting|deferred/i;
const OK = /done|complete|written|wrote|indexed|saved/i;

export function toneOf(text: string): Tone {
  return WARNING_LINE.test(text) ? "warn" : ERROR.test(text) ? "error" : WARN.test(text) ? "warn" : OK.test(text) ? "ok" : "plain";
}

export class LogBuffer {
  private screen = new Screen(KEEP);
  private next = 0;
  private made = new WeakMap<Row, LogLine>();            // a row that did not change keeps its LogLine, so a console draws it once

  /** How many entries have been added since the start (the rows dropped from the far end included): the numbering the server uses. */
  get received(): number { return this.next; }

  /** Add entries at the end (each is a line, unless it ends with a carriage return: the next one is drawn over it). */
  push(texts: readonly string[]) {
    this.screen.entries(texts);
    this.next += texts.length;
  }

  /** Start over with these entries (a reconnect or a poll sends the whole log again). */
  reset(texts: readonly string[] = []) {
    this.screen.reset();
    this.next = 0;
    this.push(texts);
  }

  /** Add entries that the server numbers from `start`: only the part past what is already here is appended; if the entries begin later
   *  than this buffer ends (a long log was cut to its tail), the numbering skips ahead so an entry keeps its server number. */
  place(start: number, texts: readonly string[]) {
    if (start > this.next) this.next = start;
    const skip = this.next - start;
    if (skip < texts.length) this.push(skip > 0 ? texts.slice(skip) : texts);
  }

  /** The last `count` rows. A new array each time; a row that did not change is the same object as before. */
  window(count = VIEW): LogLine[] { return this.screen.tail(count).map((row) => this.line(row)); }

  /** How many older rows are kept but not inside a window of `count`. */
  earlier(count = VIEW): number { return Math.max(0, this.screen.length - count); }

  /** Every kept row as plain text, one per line (what Copy puts on the clipboard). */
  text(): string { return this.screen.rows().map((row) => row.text).join("\n"); }

  private line(row: Row): LogLine {
    let line = this.made.get(row);
    if (!line) {
      line = { id: row.id, text: row.text, tone: toneOf(row.text), runs: row.runs, rev: row.rev };
      this.made.set(row, line);
    }
    return line;
  }
}
