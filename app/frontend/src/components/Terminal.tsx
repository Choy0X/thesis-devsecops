import { useEffect, useRef, useState } from "react";
import { Button } from "./ui";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

// The terminal is drawn by xterm, which needs colour values rather than classes: they are read from the design tokens (index.css).
const token = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "transparent";

// Interactive PTY terminal attached to a container's exec session over WebSocket.
export function Terminal({ cid }: { cid: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [closed, setClosed] = useState(false);
  const [session, setSession] = useState(0);
  useEffect(() => {
    setClosed(false);
    if (!ref.current) return;
    const term = new XTerm({
      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      fontSize: 12, cursorBlink: true, convertEol: true,
      theme: { background: token("--terminal"), foreground: token("--muted"), cursor: token("--brand-strong"),
               selectionBackground: token("--brand-line") },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(ref.current);
    try { fit.fit(); } catch {}

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/sandbox/${cid}/terminal`);
    ws.binaryType = "arraybuffer";
    const resize = () => {
      try { fit.fit(); } catch {}
      if (ws.readyState === 1) ws.send(`\x00resize:${term.rows}x${term.cols}`);
    };
    ws.onopen = resize;
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") term.write(ev.data);
      else term.write(new Uint8Array(ev.data));
    };
    ws.onclose = () => { term.write("\r\n\x1b[90m[session closed]\x1b[0m\r\n"); setClosed(true); };
    const dispose = term.onData((d) => { if (ws.readyState === 1) ws.send(d); });
    window.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); dispose.dispose(); ws.close(); term.dispose(); };
  }, [cid, session]);
  return (
    <div className="space-y-2">
      <div ref={ref} className="h-64 overflow-hidden rounded-lg border border-line bg-terminal p-2" />
      {closed && (
        <div className="flex items-center gap-3 text-[0.8rem] text-muted" role="status">
          The session ended, for example because the sandbox was stopped or the connection dropped.
          <Button onClick={() => setSession((n) => n + 1)}>Start a new session</Button>
        </div>
      )}
    </div>
  );
}
