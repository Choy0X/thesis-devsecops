import { api, wsUrl, clearRequestCache, type JobProgress } from "./api";
import { LogBuffer } from "./logBuffer";

// One connection per job, shared by every console that follows it. Output that arrives faster than the screen refreshes is collected
// and announced once per frame. A socket that drops is reopened with a growing pause; a socket that never opens, or keeps failing,
// is replaced by a poll. The stream closes shortly after its last follower leaves.

export type StreamStatus = "running" | "done" | "error" | "cancelled" | "interrupted";
export type StreamSnapshot = { version: number; status: StreamStatus; error: string; progress: JobProgress | null };

const RECONNECT_MS = [500, 1000, 2000, 4000, 8000, 15000];     // then the poll takes over

const nextFrame = (cb: () => void): number =>
  typeof requestAnimationFrame === "function" ? requestAnimationFrame(cb) : window.setTimeout(cb, 16);
const cancelFrame = (id: number) =>
  typeof cancelAnimationFrame === "function" ? cancelAnimationFrame(id) : window.clearTimeout(id);

export class JobStream {
  readonly buffer = new LogBuffer();
  snapshot: StreamSnapshot = { version: 0, status: "running", error: "", progress: null };
  private listeners = new Set<() => void>();
  private pending: string[] = [];
  private pendingStart: number | null = null;      // the server's number of pending[0]
  private frame: number | null = null;
  private socket: WebSocket | null = null;
  private poller: number | null = null;
  private retry: number | null = null;
  private disposal: number | null = null;
  private attempt = 0;
  private started = false;

  constructor(readonly id: string, private onDispose: () => void) {}

  /** Follow the stream; the first follower opens the connection. Returns the function that stops following. */
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    if (this.disposal != null) { clearTimeout(this.disposal); this.disposal = null; }
    if (!this.started) this.start();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0 && this.disposal == null) {
        this.disposal = window.setTimeout(() => { this.disposal = null; if (this.listeners.size === 0) this.dispose(); }, 0);   // a re-subscribe in the same tick keeps it
      }
    };
  };

  getSnapshot = () => this.snapshot;

  private get running() { return this.snapshot.status === "running"; }

  private emit(patch: Partial<StreamSnapshot> = {}) {
    this.snapshot = { ...this.snapshot, ...patch, version: this.snapshot.version + 1 };
    this.listeners.forEach((l) => l());
  }

  private start() {
    this.started = true;
    this.connect();
  }

  private flush = () => {
    this.frame = null;
    if (this.pending.length) {
      this.buffer.place(this.pendingStart ?? this.buffer.received, this.pending);
      this.pending = [];
      this.pendingStart = null;
    }
    this.emit();
  };
  private schedule() { if (this.frame == null) this.frame = nextFrame(this.flush); }
  private flushNow() { if (this.frame != null) { cancelFrame(this.frame); } this.flush(); }

  private finish(status: StreamStatus, error: string) {
    this.flushNow();
    clearRequestCache();                                        // a finished job has probably changed what the views show
    this.stopTransport();
    this.emit({ status, error });
  }

  private startPolling() {
    if (this.poller) clearInterval(this.poller);
    this.poller = window.setInterval(async () => {
      try {
        const job = await api.getJob(this.id, this.buffer.received);
        if (!this.started) return;
        this.buffer.place(job.log_start ?? 0, job.logs || []);
        this.emit({ progress: job.progress || null });
        if (job.status !== "running") this.finish(job.status as StreamStatus, job.error);
      } catch { /* ignored: a failed poll is retried by the next tick */ }
    }, 1000);
  }

  private connect() {
    if (this.pending.length) this.flushNow();          // so the cursor we send is exact
    let socket: WebSocket;
    try { socket = new WebSocket(wsUrl(this.id, this.buffer.received)); } catch { this.startPolling(); return; }   // a reconnect asks only for what was missed
    this.socket = socket;
    let opened = false;
    socket.onopen = () => { opened = true; };
    socket.onmessage = (ev) => {
      opened = true;
      this.attempt = 0;
      const m = JSON.parse(ev.data);
      if (m.type === "logs") {
        this.pending.push(...m.lines);                         // `start` is the server's number of the first line
        this.pendingStart = this.pendingStart ?? (typeof m.start === "number" ? m.start : this.buffer.received + this.pending.length - m.lines.length);
        this.schedule();
      } else if (m.type === "log") { this.pending.push(m.line); this.pendingStart = this.pendingStart ?? this.buffer.received; this.schedule(); }
      else if (m.type === "progress") this.emit({ progress: m.progress });
      else if (m.type === "status") this.finish(m.status, m.error || "");
    };
    socket.onerror = () => undefined;                           // a close follows and decides what to do
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      if (!this.started || !this.running) return;
      if (!opened) { this.startPolling(); return; }             // it never opened (blocked, or the server refused): poll instead
      const n = this.attempt++;
      if (n >= RECONNECT_MS.length) { this.startPolling(); return; }
      this.retry = window.setTimeout(() => {
        this.retry = null;
        if (this.started && this.running) this.connect();
      }, RECONNECT_MS[n]);
    };
  }

  private stopTransport() {
    if (this.socket) { this.socket.onopen = this.socket.onmessage = this.socket.onclose = this.socket.onerror = null; this.socket.close(); this.socket = null; }
    if (this.poller) { clearInterval(this.poller); this.poller = null; }
    if (this.retry) { clearTimeout(this.retry); this.retry = null; }
  }

  private dispose() {
    this.started = false;
    this.stopTransport();
    if (this.frame != null) { cancelFrame(this.frame); this.frame = null; }
    this.onDispose();
  }
}

const streams = new Map<string, JobStream>();

/** The stream of a job (made on first use). Follow it with `stream.subscribe`. */
export function streamOf(id: string): JobStream {
  let s = streams.get(id);
  if (!s) {
    const made: JobStream = new JobStream(id, () => { if (streams.get(id) === made) streams.delete(id); });
    streams.set(id, made);
    s = made;
  }
  return s;
}

/** Close every stream (tests start each case from nothing). */
export function resetStreams() {
  streams.clear();
}
