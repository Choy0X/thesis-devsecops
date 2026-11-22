import { useEffect, useState } from "react";
import { Hint } from "./kit";
import { useWarming } from "../lib/warming";

// Shown while the backend computes a view of the large store. It counts the views, not the requests, and shows how long it has taken.
export function ComputingChip() {
  const { count, since } = useWarming();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!count) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [count]);
  if (!count) return null;
  const s = Math.max(0, Math.round((now - since) / 1000));
  return (
    <Hint label="The store is large, so some summaries take a while the first time. Prepare the store (Database page) makes them fast." data-testid="computing-chip"
      className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border border-warn-line bg-warn-tint px-2.5 py-0.5 text-[0.72rem] font-medium text-warn no-underline">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      computing {count} view{count === 1 ? "" : "s"} · {s} s
    </Hint>
  );
}
