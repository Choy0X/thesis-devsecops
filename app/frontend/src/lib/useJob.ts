import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api, errorText, type JobProgress } from "./api";
import { streamOf } from "./jobStreams";
import { refreshJobs } from "./jobsList";
import { VIEW, type LogLine } from "./logBuffer";

export type JobStatus = "idle" | "running" | "done" | "error" | "cancelled" | "interrupted";
export type JobState = { id: string | null; status: JobStatus; logs: LogLine[]; earlier: number; error: string;
  progress: JobProgress | null; failure: unknown | null };

const NOTHING = () => () => undefined;

// Runs a backend stage, or follows one that is already running, and gives the console its output and progress. The connection to the
// job is shared with every other console that follows the same job (lib/jobStreams.ts); this hook only holds which job it follows,
// how many lines this console shows, and a start that failed before there was a job.
export function useJob() {
  const [id, setId] = useState<string | null>(null);
  const [starting, setStarting] = useState<{ status: JobStatus; error: string; failure: unknown | null } | null>(null);
  const [shown, setShown] = useState(VIEW);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const stream = id ? streamOf(id) : null;
  const snap = useSyncExternalStore(stream ? stream.subscribe : NOTHING, stream ? stream.getSnapshot : () => null);

  const logs = useMemo(() => (stream ? stream.buffer.window(shown) : []), [stream, snap?.version, shown]);   // eslint-disable-line react-hooks/exhaustive-deps
  const earlier = stream ? stream.buffer.earlier(shown) : 0;

  // Follow an existing job (also used to re-attach after switching tabs).
  const attach = useCallback((jobId: string) => {
    if (!mounted.current) return;                      // a late answer for a component that is gone
    setStarting(null);
    setShown(VIEW);
    setId(jobId);
  }, []);

  // `launch` replaces the generic start call for stages that have their own validating route.
  const run = useCallback(async (stage: string, params: Record<string, any> = {},
                                launch?: (params: Record<string, any>) => Promise<{ id: string }>) => {
    setId(null);
    setShown(VIEW);
    setStarting({ status: "running", error: "", failure: null });
    let job;
    try { job = await (launch ? launch(params) : api.startJob(stage, params)); }
    catch (e: any) {
      if (mounted.current) setStarting({ status: "error", error: errorText(e), failure: e });
      return;
    }
    refreshJobs();                                     // the shared list learns of the new job at once
    attach(job.id);
  }, [attach]);

  const cancel = useCallback(async () => {
    if (id) await api.cancelJob(id).catch(() => undefined);   // ignored: the next message shows whether the job really stopped
  }, [id]);

  /** Draw another page of the lines that were kept but are not on screen. */
  const showEarlier = useCallback(() => setShown((n) => n + VIEW), []);

  /** Every row that is kept (not only the ones on screen) as plain text, one per line, read when asked: what Copy puts on the clipboard. */
  const text = useCallback(() => (stream ? stream.buffer.text() : ""), [stream]);

  const state: JobState = snap
    ? { id, status: snap.status, logs, earlier, error: snap.error, progress: snap.progress, failure: null }
    : starting
      ? { id: null, status: starting.status, logs: [], earlier: 0, error: starting.error, progress: null, failure: starting.failure }
      : { id: null, status: "idle", logs: [], earlier: 0, error: "", progress: null, failure: null };
  return { ...state, run, attach, cancel, showEarlier, text };
}
