import { Suspense, useEffect, useLayoutEffect, useRef } from "react";
import { Tooltip } from "./components/hud/Tooltip";
import { Link, Navigate, NavLink, useLocation, useNavigate } from "react-router-dom";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";
import "./index.css";
import { EmptyState, Skeleton } from "./components/ui";
import { DataSyncProvider, SyncButton } from "./lib/dataSync";
import { ActivePage } from "./components/ActivePage";
import { DatabaseBanner } from "./components/DatabaseBanner";
import { DeviceStrip } from "./components/DeviceStrip";
import { JobsTray } from "./components/JobsTray";
import { TitleBar } from "./components/TitleBar";
import { CloseGuard } from "./components/CloseGuard";
import { Dev } from "./lib/devMode";
import { api } from "./lib/api";
import { PageBoundary } from "./components/PageBoundary";
import { PageScope } from "./components/PageScope";
import { SECTIONS, redirectOf, resolve } from "./routes";

// Only the current route is mounted. Small drafts and filters live in workspaceState; backend jobs continue independently.
const loading = <div className="space-y-4"><Skeleton className="h-10 w-1/3" /><Skeleton className="h-48 w-full" /></div>;

/** Animation is cut when the window is drawn without the graphics card (Settings, Performance): every frame is then painted by the processor. The person's own request for less motion
 * is honoured by the stylesheet (prefers-reduced-motion); this adds the setting. */
function useMotionGate() {
  useEffect(() => {
    api.getSettings().then((s) => { if ((s as { ui_gpu?: string }).ui_gpu === "off") document.documentElement.dataset.motion = "reduced"; }).catch(() => undefined);      // ignored: without the setting the motion stays as it is
  }, []);
}

export default function App() {
  useMotionGate();
  const location = useLocation();
  const navigate = useNavigate();
  const current = resolve(location.pathname);
  const projectConversation = current?.sectionId === "projects";
  const deviceProjectId = current?.sectionId === "projects" ? new URLSearchParams(location.search).get("project") : null;         // DeviceStrip's own agent-sandbox chip
  const scroller = useRef<HTMLElement | null>(null);
  const scrolled = useRef(new Map<string, number>());            // where each page was scrolled to, by page key
  const rememberScroll = () => { if (current && scroller.current) scrolled.current.set(current.key, scroller.current.scrollTop); };
  useLayoutEffect(() => { if (scroller.current) scroller.current.scrollTop = scrolled.current.get(current?.key ?? "") ?? 0; }, [current?.key]);
  const go = (path: string) => navigate(path);
  const redirect = location.pathname === "/build" && new URLSearchParams(location.search).get("step") === "review" ? "/pairs?tab=context" : redirectOf(location.pathname);
  if (redirect) return <Navigate to={redirect} replace />;

  // The window is a fixed-height column: the title bar, then the rail and the page area side by side. Only `main` scrolls, so the scrollbar sits beside the page content, below the title
  // bar, and never runs up the window's edge past the window buttons. Each page keeps its own scroll position while another is shown.
  return (
    <DataSyncProvider enabled={!!current?.liveData}>
      <div className="flex h-screen flex-col overflow-hidden bg-bg text-fg">
      <a href="#content" className="skip-link" onClick={(e) => { e.preventDefault(); document.getElementById("content")?.focus(); }}>Skip to the page</a>
      <TitleBar section={current?.title ?? ""} />
      <CloseGuard />
      <div className="flex min-h-0 flex-1">
        <aside className={"app-workbench-rail flex shrink-0 flex-col border-r border-line bg-surface " + (projectConversation ? "is-project" : "")}>
          <Link to="/" aria-label="Vuln Framework home" className="app-workbench-brand">
            <span className="app-workbench-mark" aria-hidden="true" />
            <strong className="app-workbench-rail-label">AIVF</strong>
          </Link>
          <nav aria-label="Sections" className="flex-1 overflow-y-auto px-1.5 pb-4">
            {SECTIONS.map((s) => {
              const Ico = s.icon;
              return (
                <NavLink key={s.id} to={s.path} end={s.path === "/"} title={s.label} aria-label={s.label}
                  className={({ isActive }) => `mb-1 flex w-full items-center gap-2.5 border px-2.5 py-2 text-[0.85rem] transition-colors ${
                    isActive ? "chamfer-sm border-brand-line bg-brand-tint-strong text-fg [--ch-line:var(--brand-line)]" : "border-transparent text-muted hover:bg-surface-2 hover:text-fg"}`}>
                  {({ isActive }) => (<><span className={isActive ? "text-brand-strong" : "text-faint"}><Ico size={18} aria-hidden /></span>
                    <span className="app-workbench-rail-label">{s.label}</span></>)}
                </NavLink>
              );
            })}
          </nav>
        </aside>

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <header className="app-page-header flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line bg-bg px-gutter">
            <h1 className="min-w-0 text-base font-semibold tracking-[-0.015em]">{current?.title ?? "Not found"}</h1>
            {current?.liveData && <SyncButton />}
            <span className="ml-auto"><DeviceStrip projectId={deviceProjectId} /></span>
            <JobsTray />
            <Dev><Tooltip label="Developer mode is on: ids, paths and technical details are shown" side="bottom"><Link to="/settings?tab=developer"
              className="border border-brand-line bg-brand-tint px-2 py-0.5 font-mono text-[0.68rem] uppercase tracking-wider text-brand-strong">Developer mode</Link></Tooltip></Dev>
          </header>
          <DatabaseBanner />
          <main id="content" ref={scroller} tabIndex={-1} onScroll={rememberScroll} className={`relative min-h-0 flex-1 outline-none ${projectConversation ? "overflow-hidden" : "overflow-y-auto"}`}>
            <div className={projectConversation ? "h-full w-full" : "app-page mx-auto w-full max-w-6xl px-gutter"}>
              {current && <section key={current.key} className={projectConversation ? "h-full" : undefined}>
                <PageScope><ActivePage active><PageBoundary name={current.title}><Suspense fallback={loading}>{current.element(go)}</Suspense></PageBoundary></ActivePage></PageScope>
              </section>}
              {!current && (
                <EmptyState title="There is no page at this address" hint="Use the left rail to open a section."
                  action={<Link to="/" className="text-[0.85rem] text-brand-strong hover:underline">Go to the Overview</Link>} />
              )}
            </div>
          </main>
        </div>
      </div>
      </div>
    </DataSyncProvider>
  );
}
