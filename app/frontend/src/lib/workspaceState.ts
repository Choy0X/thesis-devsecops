import { useState, type Dispatch, type SetStateAction } from "react";

// Small UI values survive route unmounts. Server data and active requests stay with their page.
const values = new Map<string, unknown>();
export function resetWorkspaceState() { values.clear(); }
export function clearWorkspaceValue(key: string) { values.delete(key); }
export function carryWorkspaceDraft(from: string, to: string) {
  const draft = values.get(from);
  if (typeof draft === "string" && draft.trim() && !draft.trimStart().startsWith("/") && !values.get(to)) {
    values.set(to, draft);
  }
}
export function useWorkspaceState<T>(key: string, initial: T | (() => T)): [T, Dispatch<SetStateAction<T>>] {
  const [value, setValue] = useState<T>(() => {
    if (values.has(key)) return values.get(key) as T;
    const next = initial instanceof Function ? initial() : initial;
    values.set(key, next);
    return next;
  });
  const set: Dispatch<SetStateAction<T>> = next => setValue(current => {
    const resolved = next instanceof Function ? next(current) : next;
    values.set(key, resolved);
    return resolved;
  });
  return [value, set];
}
