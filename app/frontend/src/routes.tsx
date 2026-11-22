import { lazy, type ReactNode } from "react";
import {
  Boxes, CircleGauge, Cpu, Container, Database as DatabaseIcon, FileCode2, FolderGit2, Hammer, ShieldAlert, Settings as SettingsIcon, Workflow, type LucideIcon,
} from "lucide-react";
import { Overview } from "./sections/Overview";
import { isChunkError, reloadOnce } from "./lib/staleBuild";

// The map of the interface: the eleven sections of the left rail. An address of the earlier layout (/legacy/<old id>) still works: it leads to the
// section that holds that page now. Every page is loaded the first time it is opened.

// A page file that is gone because the app was rebuilt under this window: reload the window once (it then fetches the new files) and keep the loading state meanwhile; if it cannot, the
// error goes to the page's boundary, which says the app was updated.
const page = <T extends Record<string, unknown>>(load: () => Promise<T>, name: keyof T) =>
  lazy(() => load().then((m) => ({ default: m[name] as React.ComponentType<any> })).catch((err) => {
    if (isChunkError(err) && reloadOnce()) return new Promise<never>(() => undefined);
    throw err;
  }));

const ModelHubPage = page(() => import("./pages/ModelHubPage"), "ModelHubPage");
const CollectionsPage = page(() => import("./pages/CollectionsPage"), "CollectionsPage");
const CampaignsPage = page(() => import("./pages/CampaignsPage"), "CampaignsPage");
const RecordsPage = page(() => import("./pages/RecordsPage"), "RecordsPage");
const CodePairsPage = page(() => import("./pages/CodePairsPage"), "CodePairsPage");
const BuildPage = page(() => import("./pages/BuildPage"), "BuildPage");
const TrainPage = page(() => import("./pages/TrainPage"), "TrainPage");
const ProjectsPage = page(() => import("./pages/ProjectsPage"), "ProjectsPage");
const SettingsPage = page(() => import("./pages/SettingsPage"), "SettingsPage");
const SandboxPage = page(() => import("./pages/SandboxPage"), "SandboxPage");

export type Section = { id: string; path: string; label: string; title: string; icon: LucideIcon; element: (go: (path: string) => void) => ReactNode; liveData?: boolean };

/** The eleven sections of the left rail, in order. */
export const SECTIONS: Section[] = [
  { id: "overview", path: "/", label: "Overview", title: "Overview", icon: CircleGauge, element: (go) => <Overview go={(id) => go(legacyTarget(id))} /> },
  { id: "models", path: "/models", label: "Model Hub", title: "Model Hub", icon: Boxes, element: () => <ModelHubPage /> },
  { id: "collections", path: "/collections", label: "CVE Collections", title: "CVE Collections", icon: ShieldAlert, element: () => <CollectionsPage />, liveData: true },
  { id: "campaigns", path: "/campaigns", label: "Campaigns", title: "Campaigns", icon: Workflow, element: () => <CampaignsPage />, liveData: true },
  { id: "records", path: "/records", label: "Source records", title: "Source records", icon: DatabaseIcon, element: () => <RecordsPage />, liveData: true },
  { id: "pairs", path: "/pairs", label: "Collected data", title: "Collected data", icon: FileCode2, element: () => <CodePairsPage />, liveData: true },
  { id: "build", path: "/build", label: "Build dataset", title: "Build dataset", icon: Hammer, element: () => <BuildPage />, liveData: true },
  { id: "train", path: "/train", label: "Train", title: "Train", icon: Cpu, element: () => <TrainPage /> },
  { id: "sandbox", path: "/sandbox", label: "Sandbox", title: "Docker sandbox", icon: Container, element: () => <SandboxPage /> },
  { id: "projects", path: "/projects", label: "Projects", title: "Projects", icon: FolderGit2, element: () => <ProjectsPage /> },
  { id: "settings", path: "/settings", label: "Settings", title: "Settings", icon: SettingsIcon, element: () => <SettingsPage /> },
];

/** Where a link written for the earlier layout (an old page id) goes now. */
const MOVED: Record<string, string> = {
  data: "/records", pairs: "/pairs", train: "/train", build: "/build", results: "/train", finetune: "/train", engine: "/train", review: "/pairs?tab=context",
  collection: "/collections", patch: "/projects?tab=patch", response: "/projects?tab=response", sandbox: "/sandbox", settings: "/settings", logs: "/settings?tab=developer",
  migration: "/settings?tab=developer", database: "/settings?tab=developer", overview: "/",
};
export const legacyTarget = (id: string): string => MOVED[id] ?? "/";

export type Resolved = { key: string; title: string; element: (go: (path: string) => void) => ReactNode; liveData: boolean; sectionId: string | null };

const KitGallery = page(() => import("./sections/KitGallery"), "KitGallery");

/** Pages that are not in the rail: the component gallery, reached from Settings, Developer. */
const HIDDEN: Record<string, { key: string; title: string; element: () => ReactNode }> = {
  "/kit": { key: "kit", title: "Component kit", element: () => <KitGallery /> },
};

/** The page a path shows: a section of the rail, a page outside it, or none (a path nobody owns). */
export function resolve(pathname: string): Resolved | null {
  const clean = pathname.replace(/\/+$/, "") || "/";
  const section = SECTIONS.find((s) => s.path === clean);
  if (section) return { key: section.id, title: section.title, element: section.element, liveData: !!section.liveData, sectionId: section.id };
  const hidden = HIDDEN[clean];
  if (hidden) return { key: hidden.key, title: hidden.title, element: hidden.element, liveData: false, sectionId: null };
  return null;
}

/** An address of the earlier layout (/legacy/<old id>) names no page any more; it leads to the section that holds that page now, or null for any other address. */
export function redirectOf(pathname: string): string | null {
  const m = /^\/legacy\/([a-z]+)\/?$/.exec(pathname);
  return m ? legacyTarget(m[1]) : null;
}

