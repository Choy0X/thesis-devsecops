import { useMemo } from "react";

function diffRows(diff: string) {
  let oldLine = 0;
  let newLine = 0;
  let oldRemaining = 0;
  let newRemaining = 0;
  return diff.replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n").map(text => {
    const hunk = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(text);
    if (hunk) {
      oldLine = Number(hunk[1]); oldRemaining = Number(hunk[2] ?? 1);
      newLine = Number(hunk[3]); newRemaining = Number(hunk[4] ?? 1);
      return { text, tone: "hunk" };
    }
    if (text[0] === "\\" || (/^(--- |\+\+\+ |diff |index )/.test(text) && !oldRemaining && !newRemaining)) return { text, tone: "header" };
    if (text[0] === "-") {
      const old = oldRemaining > 0 ? oldLine++ : undefined;
      oldRemaining = Math.max(0, oldRemaining - 1);
      return { text, tone: "remove", old };
    }
    if (text[0] === "+") {
      const next = newRemaining > 0 ? newLine++ : undefined;
      newRemaining = Math.max(0, newRemaining - 1);
      return { text, tone: "add", next };
    }
    if (text[0] === " " && oldRemaining > 0 && newRemaining > 0) {
      oldRemaining--; newRemaining--;
      return { text, tone: "context", old: oldLine++, next: newLine++ };
    }
    return { text, tone: "header" };
  });
}

export function DiffView({ diff }: { diff: string }) {
  const rows = useMemo(() => diffRows(diff), [diff]);
  const additions = rows.filter(row => row.tone === "add").length;
  const removals = rows.filter(row => row.tone === "remove").length;
  return <>
    <div className="chat-diff-count" aria-label={additions + " additions, " + removals + " deletions"}>
      <span className="text-ok">+{additions}</span><span className="text-crit-soft">-{removals}</span>
    </div>
    <div className="chat-diff-lines" role="region" aria-label="Unified diff" tabIndex={0}>
      {rows.map((row, index) => <div className="chat-diff-line" data-tone={row.tone} key={index}>
        <span aria-hidden="true">{row.old}</span><span aria-hidden="true">{row.next}</span><code>{row.text || " "}</code>
      </div>)}
    </div>
  </>;
}
