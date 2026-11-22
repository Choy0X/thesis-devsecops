import { ArrowLeft } from "lucide-react";
import { useEffect, useState } from "react";
import { api, errorText, type Project } from "../lib/api";
import { ChatWorkspace } from "../components/chat/ChatWorkspace";
import { Tabs } from "../components/hud/Tabs";
import { IconButton } from "../components/kit";
import { Skeleton } from "../components/ui";
import { UnloadModelDialog } from "../components/UnloadModelDialog";

export type ConversationTab = "chat" | "agent";

// A project's conversation with the whole window: one tab bar (Normal Chat, Agentic Mode) in place of two separate pages. Both tabs are the same ChatWorkspace component, mounted
// twice with a different `kind` -- switching tabs only unlocks or hides capabilities on the same page, it is never a different screen underneath. Each instance, once shown, stays
// mounted (hidden, not unmounted, via Tabs' own keepMounted) for the rest of this page's life, so a live agent run keeps streaming in the background exactly as if it were still
// visible. A conversation's mode is decided the moment it is created (a chat and an agent session are two disjoint stores that can never become one another) and never asked again;
// this component only ever decides which already-mounted pane is on screen.
//
// Leaving (the back button) is the one real signal either workspace is done with the model for now (elsewhere a page merely hides, it does not unmount): if a local model is still
// loaded, it is freed first, with a required dialog, instead of being left to sit in memory until the idle timeout notices.

export function ProjectConversationFocus({ projectId, initialTab, initialChatId, initialSessionId, onTabChange, onChatChange, onSessionChange, onBack }: {
  projectId: string; initialTab: ConversationTab; initialChatId?: string | null; initialSessionId?: string | null;
  onTabChange: (tab: ConversationTab) => void; onChatChange?: (id: string | null) => void; onSessionChange?: (id: string | null) => void; onBack: () => void;
}) {
  const [project, setProject] = useState<Project | null>(null);
  const [error, setError] = useState("");
  const [unloading, setUnloading] = useState(false);
  const [tab, setTab] = useState<ConversationTab>(initialTab);
  const [visited, setVisited] = useState<Set<ConversationTab>>(() => new Set([initialTab]));
  useEffect(() => {
    setProject(null); setError("");
    let live = true;
    api.projects().then((r) => {
      const found = r.projects.find((p) => p.id === projectId);
      if (!live) return;
      if (found) setProject(found); else setError("There is no such project.");
    }).catch((e) => { if (live) setError(errorText(e)); });
    return () => { live = false; };
  }, [projectId]);
  const handleBack = async () => {
    try {
      const live = await api.servingStatusLive();
      if (live.slots.llm?.state === "ready") { setUnloading(true); return; }
    } catch { /* ignored: if the status cannot be read, just leave */ }
    onBack();
  };
  const changeTab = (next: string) => {
    const t = next as ConversationTab;
    setTab(t);
    setVisited((v) => (v.has(t) ? v : new Set(v).add(t)));
    onTabChange(t);
  };
  return (
    <div className="flex h-full min-h-0 flex-col gap-3 px-gutter py-4">
      <div className="flex items-center gap-2">
        <IconButton label="Back to the project" onClick={() => void handleBack()}><ArrowLeft size={16} aria-hidden /></IconButton>
        <h1 className="min-w-0 flex-1 truncate text-[1rem] font-semibold">{project ? project.name : "Conversation"}</h1>
      </div>
      {error && (
        <div className="space-y-2">
          <p role="alert" className="text-[0.82rem] text-crit-soft">{error}</p>
          <button type="button" className="text-[0.82rem] text-brand-strong underline" onClick={() => void handleBack()}>Back to the projects</button>
        </div>)}
      {!project && !error && <Skeleton className="h-40 w-full" />}
      {project && (
        <Tabs value={tab} onValueChange={changeTab} className="flex min-h-0 flex-1 flex-col" keepMounted items={[
          { id: "chat", label: "Normal Chat", content: visited.has("chat")
            ? <ChatWorkspace project={project} kind="chat" initialId={initialChatId} startEmpty={initialTab !== "chat" && !initialChatId} onIdChange={onChatChange} /> : null },
          { id: "agent", label: "Agentic Mode", content: visited.has("agent")
            ? <ChatWorkspace project={project} kind="agent" initialId={initialSessionId} startEmpty onIdChange={onSessionChange} /> : null },
        ]} />
      )}
      {unloading && <UnloadModelDialog onDone={() => { setUnloading(false); onBack(); }} />}
    </div>
  );
}
