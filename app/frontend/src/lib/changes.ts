import { useEffect, useRef } from "react";
import type { Job } from "./api";
import { clearRequestCache, invalidateRequestResource } from "./request";

// What the app shows about the models and the llama.cpp program is read from the disk by the backend, and the pages that show it read it once when they open (Settings stays
// mounted for the whole session). So when something changes it, the page that changed it says so here: a download, a conversion or an import that ended, the llama.cpp install,
// a model that was deleted. Saying so forgets what the request layer kept and calls every page that listens to that topic, so each reads again.

export type Topic = "models" | "program";

/** The jobs that change something the app lists, and what they change. A download that failed or was stopped counts too: part of it is on the disk. */
export const STAGE_TOPICS: Record<string, Topic[]> = {
  hf_download: ["models"], install_model: ["models"], convert_gguf: ["models"], convert_adapter: ["models"], import_finetune_outputs: ["models"],
  install_ollama_model: ["models"],
};

const listeners = new Set<{ topics: Topic[]; call: () => void }>();

/** Something changed: forget what was kept and tell the listeners of these topics (each once). */
export function notifyChanged(...topics: Topic[]): void {
  if (topics.includes('program')) clearRequestCache();
  else if (topics.includes('models')) invalidateRequestResource('/api/models');
  const changed = new Set(topics);
  for (const l of [...listeners]) if (l.topics.some((t) => changed.has(t))) l.call();
}

export function subscribeChanged(topics: Topic[], call: () => void): () => void {
  const entry = { topics, call };
  listeners.add(entry);
  return () => { listeners.delete(entry); };
}

/** Calls `reload` whenever one of the topics changes, while the component is mounted (the latest function is called). */
export function useOnChanged(topics: Topic[], reload: () => void): void {
  const latest = useRef(reload);
  latest.current = reload;
  const key = topics.join(",");
  useEffect(() => subscribeChanged(key.split(",") as Topic[], () => latest.current()), [key]);
}

/** Which topics the jobs in this list changed since `seen` (the ids of the relevant jobs already told about; null before the first list). A job counts once, when it is over: also
 *  when it started and ended between two looks at the list. The first list only records what was already over. */
export function topicsOfFinished(jobs: Job[], seen: Set<string> | null): { topics: Topic[]; seen: Set<string> } {
  const next = new Set(seen ?? []);
  const topics = new Set<Topic>();
  for (const j of jobs) {
    const wanted = STAGE_TOPICS[j.stage];
    if (!wanted || j.status === "running" || next.has(j.id)) continue;
    next.add(j.id);
    if (seen) wanted.forEach((t) => topics.add(t));
  }
  return { topics: [...topics], seen: next };
}

/** For tests. */
export function resetChanges(): void { listeners.clear(); }
