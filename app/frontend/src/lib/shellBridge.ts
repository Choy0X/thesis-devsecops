// The desktop shell (Pyloid) hands the page a bridge to its window: `window.__PYLOID__` with minimize, maximize, close and a few questions.
// In a browser or in Chromium's app mode there is no bridge, and the browser draws its own title bar. Every call here answers even when the bridge
// does not, so the title bar can ask without a guard around each question.

type Slot = (...args: unknown[]) => unknown;
type Bridge = Record<string, Slot>;

const bridge = (): Bridge | null => ((window as unknown as { __PYLOID__?: Bridge }).__PYLOID__ ?? null);

/** True once the shell has put its bridge in the page. */
export const hasBridge = (): boolean => bridge() !== null;

/** Calls a slot of the bridge. A Qt slot hands back its value through a callback; some builds return a promise instead, so both are accepted.
 *  Resolves to undefined when there is no bridge, no such slot, or no answer within a second. */
export function call<T = void>(name: string, ...args: unknown[]): Promise<T | undefined> {
  return new Promise((resolve) => {
    const b = bridge();
    const slot = b?.[name];
    if (!b || typeof slot !== "function") { resolve(undefined); return; }
    const timer = window.setTimeout(() => resolve(undefined), 1000);
    const done = (value: unknown) => { window.clearTimeout(timer); resolve(value as T); };
    try {
      const returned = slot.call(b, ...args, done) as { then?: (a: (v: unknown) => void, b: () => void) => void } | undefined;
      if (returned && typeof returned.then === "function") returned.then(done, () => done(undefined));
      else if (returned !== undefined) done(returned);
      else if (args.length === 0 && !/^(is|get)/.test(name)) done(undefined);               // an action has no answer to wait for
    } catch {
      done(undefined);
    }
  });
}

export const shell = {
  minimize: () => call("minimize"),
  toggleMaximize: () => call("toggleMaximize"),
  close: () => call("close"),
  isMaximized: () => call<boolean>("isMaximized"),
  /** Whether the window has the operating system's frame (false when the page draws its own title bar). */
  hasFrame: () => call<boolean>("getFrame"),
  setFrame: (frame: boolean) => call("setFrame", frame),
};

/** Runs `fn` when the bridge is ready (now if it already is). Returns the function that stops waiting. */
export function whenReady(fn: () => void): () => void {
  if (hasBridge()) { fn(); return () => undefined; }
  const handler = () => fn();
  document.addEventListener("pyloidReady", handler, { once: true });
  return () => document.removeEventListener("pyloidReady", handler);
}
