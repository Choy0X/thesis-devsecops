import { Input, Textarea, IdeSelect, Checkbox, Radio } from "./Controls";
import { useEffect, useRef, useState } from "react";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { endpoint, get, write } from "./client";

type Session = { id: string; status: string };
export function IdeTerminal({ chatId }: { chatId: string }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const host = useRef<HTMLDivElement>(null);
  const base = `${endpoint(chatId)}/terminals`;
  useEffect(() => {
    let alive = true;
    void get<{ terminals: Session[] }>(base).then(data => { if (alive) { setSessions(data.terminals); setSelected(data.terminals.find(item => item.status === "running")?.id || data.terminals[0]?.id || ""); } }).catch(e => { if (alive) setError(String(e.message || e)); });
    return () => { alive = false; };
  }, [base]);
  useEffect(() => {
    if (!selected || !host.current) return;
    let alive = true;
    let sequence = 0;
    const controller = new AbortController();
    const styles = getComputedStyle(host.current);
    const term = new XTerm({ fontFamily: "'JetBrains Mono', monospace", fontSize: 12, cursorBlink: true, theme: { background: styles.backgroundColor, foreground: styles.color, cursor: styles.getPropertyValue("--brand-strong").trim() } });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host.current);
    const themeRoot = host.current.closest(".project-ide");
    const themeObserver = new MutationObserver(() => {
      if (!host.current) return;
      const next = getComputedStyle(host.current);
      term.options.theme = { background: next.backgroundColor, foreground: next.color, cursor: next.getPropertyValue("--brand-strong").trim() };
    });
    if (themeRoot) themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const resize = () => {
      if (!alive) return;
      try { fit.fit(); void write(`${base}/${selected}/resize`, { cols: term.cols, rows: term.rows }).catch(e => { if (alive) setError(e.message); }); } catch { /* ignored: A hidden tray cannot be measured until it opens. */ }
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host.current);
    const input = term.onData(data => { void write(`${base}/${selected}/input`, { data }).catch(e => { if (alive) setError(e.message); }); });
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await get<{ chunks: { sequence: number; data: string }[]; sequence: number; overflow?: boolean; status: string }>(`${base}/${selected}?after_sequence=${sequence}`, controller.signal);
        if (!alive) return;
        if (result.overflow) term.writeln("\r\n[Earlier terminal output was truncated]\r\n");
        for (const chunk of result.chunks) { if (chunk.sequence > sequence) { term.write(chunk.data); sequence = chunk.sequence; } }
        sequence = Math.max(sequence, result.sequence);
        setSessions(items => items.map(item => item.id === selected ? { ...item, status: result.status } : item));
      } catch (e) { if (alive) setError(e instanceof Error ? e.message : String(e)); }
      if (alive) timer = setTimeout(poll, 600);
    };
    resize();
    void poll();
    return () => { alive = false; controller.abort(); clearTimeout(timer); observer.disconnect(); themeObserver.disconnect(); input.dispose(); term.dispose(); };
  }, [base, selected]);
  const create = async () => {
    setBusy(true); setError("");
    try { const session = await write<Session>(base, { cols: 100, rows: 24 }); setSessions(items => [...items, session]); setSelected(session.id); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  const close = async () => {
    setBusy(true);
    try { await write(`${base}/${selected}`, undefined, "DELETE"); setSessions(items => items.filter(item => item.id !== selected)); setSelected(""); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  return <div className="ide-terminal">
    <div className="ide-toolbar">
      <IdeSelect aria-label="Terminal session" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Select terminal</option>{sessions.map((session, index) => <option key={session.id} value={session.id}>Terminal {index + 1} ({session.status})</option>)}</IdeSelect>
      <button disabled={busy} onClick={() => void create()}>New terminal</button>
      {selected && <button disabled={busy} onClick={() => void close()}>End terminal</button>}
    </div>
    {error && <p className="ide-error" role="alert">{error}</p>}
    {selected ? <div className="ide-terminal-host" ref={host} /> : <div className="ide-empty">Open a terminal in this chat's workspace to run commands.</div>}
  </div>;
}

