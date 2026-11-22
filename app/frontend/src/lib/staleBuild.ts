// The built app is a set of files with the build's hash in their names. A window that was opened before the app was rebuilt still asks for the old names, and they are gone: every page that
// has not been opened yet then fails to load. A reload fetches the new index and fixes it, so the window reloads itself once. `reloadOnce` remembers (in the tab) that it did, and does not do
// it again within half a minute, so a file that is really missing can never make the window reload forever.

const MARK = "aivf.reloadedAt";
const WINDOW_MS = 30_000;
const CHUNK = /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|ChunkLoadError|Loading chunk .+ failed/i;

/** Is this the error of a page file that could not be fetched (in the words Chromium, Firefox and the bundler use)? */
export function isChunkError(err: unknown): boolean {
  if (err == null) return false;
  const text = err instanceof Error ? `${err.name} ${err.message}` : String(err);
  return CHUNK.test(text);
}

/** Reload the window unless it did so within the last half minute (or the tab cannot remember that it did). Answers whether it reloaded. */
export function reloadOnce(now: number = Date.now(), reload: () => void = () => window.location.reload()): boolean {
  try {
    const last = Number(sessionStorage.getItem(MARK) ?? 0);
    if (last && now - last < WINDOW_MS) return false;
    sessionStorage.setItem(MARK, String(now));
  } catch {
    return false;                                  // without storage a second reload could not be stopped, so none is made
  }
  reload();
  return true;
}
