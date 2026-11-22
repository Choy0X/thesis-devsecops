import { useSyncExternalStore } from "react";

// Unsaved editor drafts of Projects workspace files. They live outside any component, so closing the drawer,
// switching conversations or opening another page keeps them; the composer and the close guard read the counts.
export type DraftFile = { path: string; text: string; version: string };
export type FileDraft = { file: DraftFile; text: string };

const listeners = new Set<() => void>();
let revision = 0;
const notify = () => { revision += 1; for (const listener of listeners) listener(); };

class ObservedDrafts extends Map<string, FileDraft> {
  set(path: string, draft: FileDraft) { super.set(path, draft); notify(); return this; }
  delete(path: string) { const removed = super.delete(path); if (removed) notify(); return removed; }
  clear() { const had = this.size > 0; super.clear(); if (had) notify(); }
}

const byChat = new Map<string, ObservedDrafts>();

export function fileDraftsFor(chatId: string): Map<string, FileDraft> {
  let drafts = byChat.get(chatId);
  if (!drafts) { drafts = new ObservedDrafts(); byChat.set(chatId, drafts); }
  return drafts;
}

/** Unsaved files of one conversation, or of every conversation when no id is given. */
export function unsavedFiles(chatId?: string): number {
  if (chatId !== undefined) return byChat.get(chatId)?.size ?? 0;
  let total = 0;
  for (const drafts of byChat.values()) total += drafts.size;
  return total;
}

export function useUnsavedFiles(chatId?: string): number {
  useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener); }; }, () => revision);
  return unsavedFiles(chatId);
}

/** Save every draft of a conversation with the version it was opened at; the first failure stops and names the file. */
export async function saveFileDrafts(chatId: string, save: (path: string, text: string, version: string) => Promise<DraftFile>): Promise<void> {
  const drafts = fileDraftsFor(chatId);
  for (const [path, draft] of [...drafts.entries()]) {
    try {
      await save(path, draft.text, draft.file.version);
    } catch (reason) {
      throw new Error(`${path} could not be saved: ${reason instanceof Error ? reason.message : String(reason)}`);
    }
    if (drafts.get(path) === draft) drafts.delete(path);
  }
}

export function resetFileDrafts() { byChat.clear(); notify(); }
