import { useEffect, useRef, useState } from "react";
import { api, errorText } from "../lib/api";
import { Button } from "./ui";

export const PICK_POLL_MS = 500;

/** The operating system's folder window, asked for through the app's window; where the app has no window of its own (a browser) it says so and the path is typed. */
export function BrowseButton({ onChosen, title = "Choose the folder of the project" }: { onChosen: (path: string) => void; title?: string }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  const browse = async () => {
    setBusy(true); setNote("");
    try {
      const r = await api.pickFolder(title);
      if (!r.ok || !r.id) { setNote(r.detail || "A folder window is not available here; type the path."); return; }
      for (;;) {
        await new Promise((res) => setTimeout(res, PICK_POLL_MS));
        if (!alive.current) return;
        const s = await api.pickFolderResult(r.id);
        if (s.state === "chosen" && s.path) { onChosen(s.path); return; }
        if (s.state === "cancelled") return;
      }
    } catch (e) { setNote(errorText(e)); } finally { if (alive.current) setBusy(false); }
  };
  return (
    <>
      <Button variant="ghost" loading={busy} onClick={browse} aria-label="Browse for a folder">Browse</Button>
      {note && <p role="status" className="basis-full text-[0.76rem] text-muted">{note}</p>}
    </>
  );
}
