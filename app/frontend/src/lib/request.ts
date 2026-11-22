// The one place the app talks to its backend over HTTP.
//
// * A GET that is already in flight is shared, so two panels asking for the same view send one request.
// * A successful GET is kept for a few seconds (a revisit does not fetch again); any write clears everything kept.
// * A GET belongs to the page that asked for it: when that page is left, its requests are aborted instead of piling up behind the
//   few connections a browser allows per host. Writes are never aborted by navigation.
// * A GET gives up after a minute (per attempt); the backend's "still computing" answers (202) are asked again, up to 15 minutes, and
//   that wait ends at once when the caller aborts.
import { ApiError } from "./apiError";
import { warming } from "./warming";

export const CACHE_TTL_MS = 3000;
/** For summaries the backend itself keeps: asked again only after this long, or sooner when a write, a finished job or a changed data stamp clears them. */
export const VIEW_TTL_MS = 30_000;
export const GET_TIMEOUT_MS = 60_000;
export const WARMING_GIVE_UP_MS = 15 * 60 * 1000;

export type RequestOptions = {
  /** Aborting this signal ends the request (and, for a shared GET, only that caller's wait). */
  signal?: AbortSignal;
  /** How long a successful GET stays reusable, in milliseconds; 0 always asks the server (polling). */
  ttl?: number;
  /** Per attempt; defaults to a minute for GET and to none for writes (a write may legitimately run long). */
  timeoutMs?: number;
  /** A POST that only reads (a preview, a candidate list): it is not a reason to forget what was kept. */
  readOnly?: boolean;
  /** A request that belongs to the whole app, not to the page on screen (not aborted when the page is left). */
  global?: boolean;
};

/** Thrown when the caller cancelled the request; pages that were left do not need to report it. */
export class AbortedError extends Error {
  constructor() {
    super("The request was cancelled");
    this.name = "AbortError";
  }
}
export const isAbort = (e: unknown): boolean => e instanceof Error && e.name === "AbortError";

// -- the page scope -----------------------------------------------------------------------------------------------------------------
let scope: AbortController | null = null;
export const getRequestScope = () => scope;
export function setRequestScope(next: AbortController | null) { scope = next; }

// -- helpers --------------------------------------------------------------------------------------------------------------------------
/** One signal that aborts when any of the given ones does (AbortSignal.any is not available in older embedded browsers). */
export function anySignal(signals: (AbortSignal | null | undefined)[]): AbortSignal {
  const list = signals.filter((s): s is AbortSignal => !!s);
  const out = new AbortController();
  for (const s of list) {
    if (s.aborted) { out.abort(); break; }
    s.addEventListener("abort", () => out.abort(), { once: true });
  }
  return out.signal;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new AbortedError()); return; }
    const done = () => { clearTimeout(timer); reject(new AbortedError()); };
    const timer = setTimeout(() => { signal.removeEventListener("abort", done); resolve(); }, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}

// -- one request, with the "still computing" loop ------------------------------------------------------------------------------------
async function run<T>(url: string, init: RequestInit, signal: AbortSignal, timeoutMs: number | undefined): Promise<T> {
  const started = Date.now();
  const headers = new Headers({ "content-type": "application/json", "x-slow": "poll" });
  new Headers(init.headers).forEach((value, name) => headers.set(name, value));
  let waiting = false;
  try {
    for (let attempt = 0; ; attempt++) {
      if (signal.aborted) throw new AbortedError();
      const attemptAbort = new AbortController();
      let timedOut = false;
      const timer = timeoutMs ? setTimeout(() => { timedOut = true; attemptAbort.abort(); }, timeoutMs) : null;
      try {
        let r: Response;
        try {
          r = await fetch(url, {
            ...init, signal: anySignal([signal, attemptAbort.signal]),
            headers,
          });
        } catch (cause) {
          if (signal.aborted) throw new AbortedError();
          if (timedOut) throw ApiError.timeout(url, timeoutMs!);
          throw ApiError.unreachable(url, cause);
        }
        if (r.status === 202 && r.headers.get("x-slow-cache") === "warming") {
          if (!waiting) { waiting = true; warming.start(url); }
          if (Date.now() - started > WARMING_GIVE_UP_MS) {
            throw new ApiError({ status: 202, title: "Still computing", detail: "The server is still computing this view on a large store.",
              hint: "Try again in a few minutes.", code: "warming_timeout", technical: `202 ${url}
warming for more than ${WARMING_GIVE_UP_MS} ms` });
          }
        } else {
          if (!r.ok) throw ApiError.fromBody(r.status, await r.text().catch(() => ""), url);
          try { return await r.json(); }
          catch (cause) {
            if (signal.aborted) throw new AbortedError();
            if (timedOut) throw ApiError.timeout(url, timeoutMs!);
            throw cause;
          }
        }
      } finally {
        if (timer) clearTimeout(timer);
      }
      await sleep(Math.min(3000, 1000 + attempt * 500), signal);        // a 202: ask again shortly
    }
  } finally {
    if (waiting) warming.done(url);
  }
}

// -- shared GETs and the short-lived cache ------------------------------------------------------------------------------------------
type Flight = { promise: Promise<unknown>; users: number; controller: AbortController; generation: string };
const flights = new Map<string, Flight>();
const activeFlights = new Set<Flight>();
const kept = new Map<string, { value: unknown; until: number }>();
const revisions = new Map<string, number>();
let globalRevision = 0;

function resource(url: string): string {
  const name = url.split('?')[0].split('/')[2] || url;
  if (['data', 'dataset', 'code', 'campaigns'].includes(name)) return 'data';
  if (['finetune', 'training'].includes(name)) return 'training';
  if (['ide', 'projects'].includes(name)) return 'projects';
  if (['models', 'hub', 'serving', 'gpu', 'model-access'].includes(name)) return 'models';
  return name;
}
const generation = (url: string) => `${globalRevision}:${revisions.get(resource(url)) || 0}`;

/** Invalidate only resources affected by a write; Settings can change any backend. */
export function invalidateRequestResource(url: string) {
  const name = resource(url);
  if (name === 'settings') { clearRequestCache(); return; }
  const affected = new Set([name]);
  for (const key of affected) revisions.set(key, (revisions.get(key) || 0) + 1);
  for (const key of kept.keys()) if (affected.has(resource(key))) kept.delete(key);
  epoch++;
}

// Counts every time the kept answers were thrown away: a page that was hidden can tell from it whether anything happened meanwhile.
let epoch = 0;
export const requestEpoch = () => epoch;

/** Forget every kept answer (a write, a finished job or a changed data stamp makes them untrustworthy). */
export function clearRequestCache() { kept.clear(); globalRevision++; epoch++; }

/** Abort every request in flight and forget everything kept (tests start each case from nothing). */
export function resetRequests() {
  for (const flight of activeFlights) flight.controller.abort();
  activeFlights.clear();
  flights.clear();
  kept.clear();
  revisions.clear(); globalRevision++;
  scope = null;
}

function join<T>(flight: Flight, signal: AbortSignal): Promise<T> {
  flight.users++;
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const leave = () => { settled = true; flight.users--; signal.removeEventListener("abort", onAbort); };
    const onAbort = () => {
      if (settled) return;
      leave();
      if (flight.users === 0) flight.controller.abort();                // nobody is waiting any more: stop the request itself
      reject(new AbortedError());
    };
    flight.promise.then(                                                // always handled, even when this caller has already left
      (value) => { if (!settled) { leave(); resolve(value as T); } },
      (error) => { if (!settled) { leave(); reject(error); } },
    );
    if (signal.aborted) { onAbort(); return; }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export function request<T>(url: string, init: RequestInit & RequestOptions = {}): Promise<T> {
  const { signal: own, ttl = CACHE_TTL_MS, timeoutMs, global: isGlobal, readOnly, ...rest } = init;
  const method = (rest.method || "GET").toUpperCase();

  if (method !== "GET") {                                               // a write: never shared, never aborted by navigation
    const done = run<T>(url, rest, anySignal([own]), timeoutMs);
    return readOnly ? done : done.finally(() => invalidateRequestResource(url));
  }

  const signal = anySignal([own, isGlobal ? null : scope?.signal]);
  if (ttl > 0) {
    const hit = kept.get(url);
    if (hit && hit.until > Date.now()) return Promise.resolve(hit.value as T);
  }
  let flight = flights.get(url);
  if (flight && (flight.controller.signal.aborted || flight.generation !== generation(url))) { flights.delete(url); flight = undefined; }
  if (!flight) {
    const controller = new AbortController();
    const created: Flight = { promise: Promise.resolve(), users: 0, controller, generation: generation(url) };
    activeFlights.add(created);
    created.promise = run<T>(url, rest, controller.signal, timeoutMs ?? GET_TIMEOUT_MS)
      .then((value) => { if (ttl > 0 && created.generation === generation(url)) kept.set(url, { value, until: Date.now() + ttl }); return value; })
      .finally(() => { activeFlights.delete(created); if (flights.get(url) === created) flights.delete(url); });
    flights.set(url, created);
    flight = created;
  }
  return join<T>(flight, signal);
}
