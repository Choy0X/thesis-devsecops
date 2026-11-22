import { Component, type ErrorInfo, type ReactNode } from "react";
import { Dev } from "../lib/devMode";
import { isChunkError } from "../lib/staleBuild";

// A page that throws while it draws must not take the whole window with it. The rail, the title bar and the other pages stay; this page says it hit an error,
// lets the person try it again, and keeps the technical text for Developer mode.

type Props = { children: ReactNode; name?: string };
type State = { error: Error | null };

export class PageBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`The ${this.props.name ?? "page"} failed to draw`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    if (isChunkError(this.state.error)) {          // a try-again cannot work: the rejected import is remembered by the page loader; only a reload fetches the new files
      return (
        <div role="alert" className="border border-warn-line bg-warn-tint p-5">
          <p className="text-[0.95rem] font-semibold text-warn">The app was updated while this window was open.</p>
          <p className="mt-1 text-[0.82rem] text-muted">This page belongs to the earlier version and its files are gone. Reload the app to get the new version; nothing you saved is lost.</p>
          <button type="button" onClick={() => window.location.reload()} className="mt-3 border border-line px-3 py-1.5 text-[0.82rem] hover:border-line-strong">Reload the app</button>
        </div>
      );
    }
    return (
      <div role="alert" className="border border-crit-line bg-crit-tint p-5">
        <p className="text-[0.95rem] font-semibold text-crit-soft">This page hit an error and could not be drawn.</p>
        <p className="mt-1 text-[0.82rem] text-muted">The rest of the app still works. Try the page again; if it keeps failing, the details in Developer mode say what went wrong.</p>
        <Dev><pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap bg-terminal p-2 font-mono text-[0.72rem] text-faint">{String(this.state.error.stack || this.state.error.message)}</pre></Dev>
        <button type="button" onClick={() => this.setState({ error: null })}
          className="mt-3 border border-line px-3 py-1.5 text-[0.82rem] hover:border-line-strong">Try the page again</button>
      </div>
    );
  }
}
