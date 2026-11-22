import { useEffect } from "react";
import { useToast } from "./Toast";

// The desktop window saves a file the page downloads (an Export, an audit log) with the system's Save window (app/downloads.py) and tells the page how it ended with a DOM event, ``app-download``.
// This says it: where the file went, or why it did not. A save window the person closed is not worth a message. In a browser the event never comes: the browser shows its own download.

export type DownloadEvent = { state: "saved" | "failed" | "cancelled"; name: string; path?: string; reason?: string };

export function DownloadNotice() {
  const toast = useToast();
  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent<DownloadEvent>).detail;
      if (!d || typeof d !== "object") return;
      if (d.state === "saved") toast.success(d.path ? `Saved to ${d.path}` : `Saved ${d.name}`);
      else if (d.state === "failed") toast.error(`${d.name || "The file"} could not be saved: ${d.reason || "the download did not finish"}`);
    };
    window.addEventListener("app-download", on);
    return () => window.removeEventListener("app-download", on);
  }, [toast]);
  return null;
}
