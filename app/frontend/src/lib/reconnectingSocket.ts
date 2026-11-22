export type SocketState = "connecting" | "live" | "reconnecting" | "closed";

export type ReconnectOptions = {
  url: string;
  onMessage: (data: string) => void;
  onState?: (state: SocketState, retryInMs?: number) => void;
  /** Waits between attempts, in milliseconds; the last value repeats. */
  delays?: number[];
  /** Replaceable in tests. */
  make?: (url: string) => WebSocket;
};

/** A WebSocket that opens again after it drops, with growing waits, until close() is called. Returns the closer. */
export function reconnectingSocket(opts: ReconnectOptions): () => void {
  const delays = opts.delays ?? [1000, 2000, 5000, 10000];
  const make = opts.make ?? ((u: string) => new WebSocket(u));
  let ws: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let attempt = 0;
  let stopped = false;

  const open = () => {
    if (stopped) return;
    opts.onState?.(attempt === 0 ? "connecting" : "reconnecting");
    const s = make(opts.url);
    ws = s;
    s.onopen = () => { attempt = 0; opts.onState?.("live"); };
    s.onmessage = (ev) => opts.onMessage(String(ev.data));
    s.onclose = () => {
      if (stopped || ws !== s) return;
      const wait = delays[Math.min(attempt, delays.length - 1)];
      attempt += 1;
      opts.onState?.("reconnecting", wait);
      timer = setTimeout(open, wait);
    };
  };

  open();
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    if (ws) { ws.onclose = null; ws.close(); }
    opts.onState?.("closed");
  };
}

export function stateWords(state: SocketState, retryInMs?: number): string {
  if (state === "live") return "Live";
  if (state === "connecting") return "Connecting...";
  if (state === "reconnecting") return retryInMs ? `Connection lost. Trying again in ${Math.max(1, Math.round(retryInMs / 1000))} s.` : "Connection lost. Trying again...";
  return "Closed";
}
