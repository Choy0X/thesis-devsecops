import { useSyncExternalStore } from "react";

// Views the backend is still computing (it answers 202 "warming" while a scan of a large store runs). The header shows them, so a page
// waiting on one says why instead of showing a silent skeleton.
const open = new Map<string, number>();            // url -> how many requests of it are waiting
let startedAt = 0;
let snapshot = { count: 0, since: 0 };
const listeners = new Set<() => void>();

function publish() {
  const count = open.size;
  if (count !== snapshot.count) {
    if (count && !snapshot.count) startedAt = Date.now();
    snapshot = { count, since: count ? startedAt : 0 };
    listeners.forEach((l) => l());
  }
}

export const warming = {
  start(url: string) { open.set(url, (open.get(url) || 0) + 1); publish(); },
  done(url: string) {
    const n = (open.get(url) || 0) - 1;
    if (n > 0) open.set(url, n); else open.delete(url);
    publish();
  },
};

export function useWarming() {
  return useSyncExternalStore((cb) => { listeners.add(cb); return () => { listeners.delete(cb); }; }, () => snapshot);
}
