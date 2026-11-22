import { carryWorkspaceDraft, clearWorkspaceValue, useWorkspaceState } from "../../lib/workspaceState";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, ChevronRight, Folder, FolderOpen, History, ListTodo, PackageSearch, PanelLeftClose, PanelLeftOpen, Paperclip, Plus, Settings2, Users, X } from "lucide-react";
import { useLocation, useSearchParams } from "react-router-dom";
import { Dialog } from "../../components/hud/Dialog";
import { CheckoutChanges } from "./CheckoutChanges";
import { useOnChanged } from "../../lib/changes";
import { useOnActivate } from "../../components/ActivePage";
import { Conversation } from "./Conversation";
import type { OpenWorkspaceReference, WorkspaceReference } from "../../components/chat/Markdown";
import { WorkbenchPalette, type PaletteEntry } from "./WorkbenchPalette";
import { WorkspacePanel } from "./WorkspacePanel";
import { WorkspaceFiles, type FileDrafts } from "./WorkspaceFiles";
import { fileDraftsFor, saveFileDrafts, useUnsavedFiles } from "../../lib/fileDrafts";
import { PatchReview } from "./PatchReview";
import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import { Dependencies } from "./Dependencies";
import { CreateProjectDialog } from "./CreateProjectDialog";
import { ProjectNavigation } from "./ProjectNavigation";
import { ChangesPanel, DetailPanel, ObservedPanel, SourcesPanel, type ChatPanel, type ChatSource } from "./ChatPanels";
import { ModelLoadingDialog, type Preparation } from "./ModelLoadingDialog";
import { activeRun, endpoint, get, requestKey, write, type Change, type Chat, type ChatDetail, type Dependency, type Model, type Project, type TurnMode } from "./client";
import { friendlyRunError } from "./runError";
import { prepareChatRuntime, type PreparationProgress, type RuntimePreparation } from "./runtimePreparation";
import { clockSpan } from "../../lib/format";
import { projectActions, parseCommand, type ScanCommand } from "./state";
import type { ScanProgressEvent, ScanProgressSnapshot } from "./ScanProgress";
import "./projectIde.css";
import "./projectChat.css";

type FirstRequest = { kind: "turn"; text: string; mode: string } | { kind: "scan"; command: ScanCommand };
type ProvisionalChat = { id: string; model: Model; key?: string };
type LoadTarget = { kind: "create"; projectId: string; draftKey: string; model: Model; request: FirstRequest; key: string; created?: ProvisionalChat; reopened: boolean; resolve: () => void; reject: (reason: Error) => void } | { kind: "switch"; chatId: string; previousModelId?: string; model: Model; applied?: Chat } | { chatId: string; model: Model; resolve: () => void; reject: (reason: Error) => void } & ({ kind: "send"; text: string; mode: TurnMode } | { kind: "resume"; runId: string });
const storageKey = "projects-ide-last-project";
const isCloud = (model: Model) => model.location === "cloud" || model.kind === "cloud";
const isSameModel = (left: Model | undefined, right: Model) => left?.id === right.id && left.digest === right.digest && left.endpoint === right.endpoint && left.tool_protocol === right.tool_protocol;
const runnable = (model: Model) => model.available !== false && model.tool_protocol !== null;
const defaultModel = (rows: Model[], previous: string) => {
  const preferred = rows.filter(model => runnable(model) && !isCloud(model) && model.tool_protocol !== "text" && model.coding_status !== "failed");
  return preferred.find(model => model.id === previous)?.id || preferred.find(model => model.coding_status === "verified")?.id ||
    preferred.find(model => model.selected)?.id || preferred[0]?.id || "";
};
const provisionalKey = (projectId: string) => `project-provisional-chat.${projectId}`;
function readProvisionalChat(projectId: string): ProvisionalChat | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(provisionalKey(projectId)) || "null");
    if (typeof value?.id === "string" && typeof value.model?.id === "string") {
      const key = typeof value.key === "string" && value.key.trim() && value.key.length <= 128 ? value.key : undefined;
      return { id: value.id, model: value.model, key };
    }
  } catch {
    // Ignore a malformed local pointer and use the current project history.
  }
}
const panelNames: Record<ChatPanel, string> = { plan: "Plan", subagents: "Subagents", sources: "Sources", dependencies: "Dependency review" };
const destinations = [{ id: "plan", icon: ListTodo, label: "Plan" }, { id: "subagents", icon: Users, label: "Subagents" }, { id: "sources", icon: Paperclip, label: "Sources" }] as const;
const normalizeDetail = (detail: ChatDetail): ChatDetail => ({
  ...detail, messages: detail.messages || [], runs: detail.runs || [], permissions: detail.permissions || [], questions: detail.questions || [],
});

// The route keeps its historical component name; the product surface is project chats.
export function ProjectIde() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const route = useRef(location.pathname); route.current = location.pathname;
  const currentParams = useRef(params); currentParams.current = params;
  const openingDraft = useRef("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectActivity, setProjectActivity] = useState<Record<string, boolean>>({});
  const [models, setModels] = useState<Model[]>([]);
  const [modelId, setModelId] = useWorkspaceState("agent.modelId", "");
  const [draftModelOverride, setDraftModelOverride] = useState<{ projectId: string; model: Model }>();
  const projectId = params.get("project") || localStorage.getItem(storageKey) || "";
  const chatId = params.get("chat") || "";
  const isNew = params.get("new") === "1";
  const view = params.get("view");
  const agentChanges = view === "agent-changes" || view === "patch";
  const changesOpen = view === "changes" || agentChanges;
  const currentProject = useRef(projectId); currentProject.current = projectId;
  const currentChat = useRef(chatId); currentChat.current = chatId;
  const [chats, setChats] = useState<Record<string, Chat[]>>({});
  const chatListRevisions = useRef(new Map<string, number>());
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const [chatErrors, setChatErrors] = useState<Record<string, string>>({});
  const [fetchedDetail, setDetail] = useState<ChatDetail | null>(null);
  const [changes, setChanges] = useState<Change[]>([]);
  const [sources, setSources] = useState<ChatSource[]>([]);
  const [panelErrors, setPanelErrors] = useState({ changes: "", sources: "" });
  const [panelLoading, setPanelLoading] = useState({ changes: true, sources: true });
  const [panel, setPanel] = useState<ChatPanel | null>(null);
  const [fileDrawer, setFileDrawer] = useState<{ chatId: string; reference?: WorkspaceReference; drafts: FileDrafts }>();
  // Sends that are preparing, by conversation: switching conversations cancels the others' preparation.
  const sendingFor = useRef(new Map<string, AbortController>());
  const [sending, setSending] = useState<ReadonlySet<string>>(new Set());
  const filesButton = useRef<HTMLButtonElement>(null);
  const fileTrigger = useRef<HTMLElement>();
  const [proposal, setProposal] = useState<{ chatId: string; findingId: string }>();
  const [addingSource, setAddingSource] = useState(false);
  const [navigation, setNavigation] = useState(() => localStorage.getItem("projects-list-open") === "true");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [addProject, setAddProject] = useState(false);
  const [settings, setSettings] = useState(false);
  const commandButton = useRef<HTMLButtonElement>(null);
  const settingsButton = useRef<HTMLButtonElement>(null);
  const workspaceButton = useRef<HTMLButtonElement>(null);
  const [loadTarget, setLoadTarget] = useState<LoadTarget | null>(null);
  const [runtimeWarm, setRuntimeWarm] = useState<RuntimePreparation | null>(null);
  const [packageSelection, setPackageSelection] = useState<string[] | null>(null);
  const [dependencies, setDependencies] = useState<Dependency[]>([]);
  const [dependencyError, setDependencyError] = useState("");
  const [dependencyLoading, setDependencyLoading] = useState(false);
  const [scanProgress, setScanProgress] = useState<ScanProgressSnapshot | null>(null);
  const [startingScan, setStartingScan] = useState<ScanCommand | null>(null);
  const [resumeQuery, setResumeQuery] = useState<string | null>(null);
  const [palette, setPalette] = useState(false);
  const [cloudConsent, setCloudConsent] = useState<{ model: Model; projectId: string; chatId: string }>();
  const paletteTrigger = useRef<HTMLElement | null>(null);
  const submissionLock = useRef(false);
  const automaticResume = useRef(new Set<string>());
  const focusComposer = useRef<string | null>(null);
  const pendingDependencyChat = useRef("");
  const detailRevision = useRef(0);
  const changesHeading = useRef<HTMLHeadingElement>(null);
  const surface = useRef<HTMLDivElement>(null);
  const project = projects.find(item => item.id === projectId);
  const detail = fetchedDetail?.chat.id === chatId ? fetchedDetail : null;
  const visibleFiles = fileDrawer?.chatId === chatId && !changesOpen && !panel ? fileDrawer : undefined;
  const draftKey = chatId || `project.${projectId}.new`;
  const currentDraft = useRef(draftKey); currentDraft.current = draftKey;
  const provisionalChat = readProvisionalChat(projectId);
  const projectChats = chats[projectId]?.filter(chat => chat.id !== provisionalChat?.id);
  const showDraft = !chatId && projectChats && (isNew || !projectChats.some(chat => !chat.archived));
  const override = draftModelOverride?.projectId === projectId ? draftModelOverride.model : undefined;
  const draftModel = (override && models.some(item => item.id === override.id && runnable(item)) && override) || provisionalChat?.model || models.find(item => item.id === modelId && runnable(item) && !isCloud(item));
  const agentModels = models.filter(model => model.tool_protocol !== null);
  const composerModels = draftModel && !agentModels.some(item => item.id === draftModel.id) ? [...agentModels, draftModel] : agentModels;
  const active = activeRun(detail?.runs || []);
  const runtimeBusy = runtimeWarm?.chat_id === chatId && ["queued", "loading"].includes(runtimeWarm.state);
  const unsavedFiles = useUnsavedFiles(chatId);
  const working = busy || sending.has(chatId) || !!loadTarget || runtimeBusy;
  const modelSwitchDisabled = working || !!active || !!detail?.chat.archived || Boolean(chatId && !detail);
  const detailError = panel === "sources" ? panelErrors.sources : "";
  const detailLoading = panel === "sources" && panelLoading.sources;
  const scanDisabled = busy || !!loadTarget || !!active || !!detail?.chat.archived || (chatId ? !detail : !projectChats);

  const showChanges = (next: "changes" | "agent-changes" | null, restoreFocus = true) => {
    const search = new URLSearchParams(currentParams.current);
    if (next) search.set("view", next); else search.delete("view");
    setParams(search, { replace: changesOpen });
    if (!next && restoreFocus) requestAnimationFrame(() => workspaceButton.current?.focus());
  };
  useEffect(() => { if (changesOpen) changesHeading.current?.focus(); }, [changesOpen]);

  const navigate = (id: string, chat = "", replace = false, fresh = false) => {
    if (route.current !== "/projects") return;
    const next = new URLSearchParams(currentParams.current);
    if (id) next.set("project", id); else next.delete("project");
    if (chat) { next.set("chat", chat); localStorage.setItem(`project-last-chat.${id}`, chat); } else next.delete("chat");
    if (fresh) next.set("new", "1"); else next.delete("new");
    next.delete("view");
    next.delete("focus"); next.delete("session");
    if (next.toString() === currentParams.current.toString()) return;
    openingDraft.current = fresh ? id : "";
    currentParams.current = next; setParams(next, { replace });
  };
  const selectChat = (id: string, chat = "", replace = false) => {
    navigate(id, chat, replace); setSettings(false); setNotice("");
    if (surface.current && surface.current.clientWidth > 0 && surface.current.clientWidth <= 700) setNavigation(false);
  };
  const reloadModels = () => { void get<{ models: Model[] }>("/api/ide/models").then(result => { setModels(result.models); setModelId(current => defaultModel(result.models, current)); }).catch(reason => setError(friendlyRunError(reason))); };
  useOnChanged(["models", "program"], reloadModels);
  useOnActivate(reloadModels);
  const closePanel = useCallback(() => { setPanel(null); setPackageSelection(null); setAddingSource(false); setProposal(undefined); requestAnimationFrame(() => workspaceButton.current?.focus()); }, []);
  const openPanel = (next: ChatPanel, add = false) => { setFileDrawer(undefined); setAddingSource(add); setPanel(next); };
  const openFiles = useCallback((reference: WorkspaceReference | undefined, trigger: HTMLElement) => {
    const id = currentChat.current;
    if (!id) return;
    const drafts = fileDraftsFor(id);
    fileTrigger.current = trigger;
    setPanel(null);
    setFileDrawer({ chatId: id, reference, drafts });
  }, []);
  const openReference: OpenWorkspaceReference = openFiles;
  const closeFiles = () => {
    setFileDrawer(undefined);
    requestAnimationFrame(() => {
      const trigger = fileTrigger.current;
      if (trigger?.isConnected) trigger.focus(); else filesButton.current?.focus();
    });
  };
  const openHistory = (query = "") => { setResumeQuery(query); setHistoryRefresh(value => value + 1); };
  const newConversation = () => {
    setResumeQuery(null);
    focusComposer.current = `project.${projectId}.new`;
    if (!provisionalChat) clearWorkspaceValue(`agent.draft.${focusComposer.current}`);
    navigate(projectId, "", false, true);
    if (!chatId && isNew) surface.current?.querySelector<HTMLTextAreaElement>('[aria-label="Message agent"]')?.focus();
  };
  useEffect(() => { localStorage.setItem("projects-list-open", String(navigation)); }, [navigation]);
  useEffect(() => {
    let compact = false;
    const measure = () => { const width = surface.current?.clientWidth || 0; if (width > 0 && width <= 700 && !compact) setNavigation(false); compact = width > 0 && width <= 700; };
    measure(); const observer = new ResizeObserver(measure); if (surface.current) observer.observe(surface.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const dismiss = (event: KeyboardEvent) => { if (event.defaultPrevented || (event.target instanceof Element && event.target.closest('[role="dialog"],[role="menu"],[role="listbox"]'))) return; if (event.key === "Escape" && panel) { closePanel(); if (packageSelection !== null) requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('[aria-label="Message agent"]')?.focus()); } };
    window.addEventListener("keydown", dismiss); return () => window.removeEventListener("keydown", dismiss);
  }, [panel, packageSelection, closePanel]);
  useEffect(() => {
    const controller = new AbortController();
    void get<{ models: Model[] }>("/api/ide/models", controller.signal).then(result => {
      if (controller.signal.aborted) return;
      setModels(result.models); setModelId(current => defaultModel(result.models, current));
    }).catch(reason => { if (!controller.signal.aborted) setError(friendlyRunError(reason)); });
    void get<{ projects: Project[] }>("/api/ide/projects", controller.signal).then(p => {
      if (controller.signal.aborted) return;
      setProjects(p.projects);
      const selected = currentParams.current.get("project") || localStorage.getItem(storageKey);
      if (!p.projects.some(item => item.id === selected)) {
        localStorage.removeItem(storageKey);
        navigate(p.projects[0]?.id || "", "", true);
      }
    }).catch(reason => { if (!controller.signal.aborted) setError(friendlyRunError(reason)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);
  const closePalette = (restoreFocus = true) => {
    setPalette(false);
    const trigger = paletteTrigger.current;
    if (restoreFocus) requestAnimationFrame(() => { if (trigger?.isConnected) trigger.focus(); });
  };
  const openPalette = (trigger?: HTMLElement) => {
    paletteTrigger.current = trigger ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    setPalette(true);
  };
  useEffect(() => {
    const commands = (event: KeyboardEvent) => {
      if (route.current !== "/projects" || event.defaultPrevented || event.repeat || !(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "k") return;
      if (document.querySelector('[role="dialog"]')) return;
      event.preventDefault();
      openPalette();
    };
    window.addEventListener("keydown", commands);
    return () => window.removeEventListener("keydown", commands);
  }, []);
  const invalidateChatList = (id: string) => {
    const revisions = chatListRevisions.current;
    revisions.set(id, (revisions.get(id) ?? 0) + 1);
  };
  const loadChats = useCallback(async (id: string, signal?: AbortSignal) => {
    const revision = chatListRevisions.current.get(id);
    const isCurrent = () => !signal?.aborted && revision === chatListRevisions.current.get(id);
    try {
      const result = await get<{ chats: Chat[] }>(`/api/ide/projects/${encodeURIComponent(id)}/chats`, signal);
      if (!isCurrent()) return;
      setChats(previous => ({ ...previous, [id]: result.chats }));
      setChatErrors(previous => { const next = { ...previous }; delete next[id]; return next; });
    } catch (reason) { if (isCurrent()) setChatErrors(previous => ({ ...previous, [id]: friendlyRunError(reason) })); }
  }, []);
  useEffect(() => {
    if (!projectId || location.pathname !== "/projects") return;
    const controller = new AbortController();
    void loadChats(projectId, controller.signal);
    return () => controller.abort();
  }, [projectId, location.pathname, historyRefresh, loadChats]);
  useEffect(() => {
    if (!projectId || location.pathname !== "/projects") return;
    localStorage.setItem(storageKey, projectId);
    if (isNew && !chatId) {
      openingDraft.current = "";
      return;
    }
    // The history can update before the router commits the requested draft.
    if (openingDraft.current === projectId) return;
    const rows = chats[projectId]; if (!rows) return;
    const provisionalId = readProvisionalChat(projectId)?.id;
    if (rows.some(chat => chat.id === chatId && chat.id !== provisionalId)) {
      localStorage.setItem(`project-last-chat.${projectId}`, chatId);
      return;
    }
    const available = rows.filter(chat => !chat.archived && chat.id !== provisionalId);
    const last = localStorage.getItem(`project-last-chat.${projectId}`);
    navigate(projectId, available.find(chat => chat.id === last)?.id || available[0]?.id || "", true);
  }, [chats, projectId, chatId, isNew, location.pathname]);
  useEffect(() => {
    if (focusComposer.current !== draftKey || (chatId && !detail)) return;
    surface.current?.querySelector<HTMLTextAreaElement>('[aria-label="Message agent"]')?.focus();
    focusComposer.current = null;
  }, [draftKey, chatId, detail?.chat.id]);
  useEffect(() => {
    if (loading || !projectId || location.pathname !== "/projects") return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await get<{ projects: Project[] }>("/api/ide/projects", controller.signal);
        if (controller.signal.aborted) return;
        setProjectActivity(Object.fromEntries(result.projects.map(item => [item.id, Boolean(item.has_active_run)])));
      } catch {
        // Preserve the last known activity on a transient refresh failure.
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2500);
    };
    timer = setTimeout(poll, 2500);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [loading, projectId, location.pathname]);
  const refresh = useCallback(async (id: string, signal?: AbortSignal) => {
    const revision = detailRevision.current;
    const result = await get<ChatDetail>(endpoint(id), signal);
    if (currentChat.current === id && route.current === "/projects" && revision === detailRevision.current && !signal?.aborted) setDetail(normalizeDetail(result));
    return result;
  }, []);
  const refreshSources = useCallback(async () => {
    const id = currentChat.current;
    try {
      const result = await get<{ sources: ChatSource[] }>(`${endpoint(id)}/sources`);
      if (currentChat.current === id) { setSources(result.sources); setPanelErrors(previous => ({ ...previous, sources: "" })); }
    } catch (reason) { if (currentChat.current === id) setPanelErrors(previous => ({ ...previous, sources: friendlyRunError(reason) })); throw reason; }
    finally { if (currentChat.current === id) setPanelLoading(previous => ({ ...previous, sources: false })); }
  }, []);
  const refreshChanges = useCallback(async () => {
    const id = currentChat.current;
    try {
      const result = await get<{ changes: Change[] }>(`${endpoint(id)}/changes`);
      if (currentChat.current === id) { setChanges(result.changes); setPanelErrors(previous => ({ ...previous, changes: "" })); }
    } catch (reason) { if (currentChat.current === id) setPanelErrors(previous => ({ ...previous, changes: friendlyRunError(reason) })); throw reason; }
    finally { if (currentChat.current === id) setPanelLoading(previous => ({ ...previous, changes: false })); }
  }, []);
  const openDependencySelection = useCallback(async (id: string, signal?: AbortSignal) => {
    setFileDrawer(undefined); setPackageSelection([]); setPanel("dependencies"); setDependencyLoading(true); setDependencyError("");
    try {
      const result = await get<{ packages: Dependency[] }>(`${endpoint(id)}/dependencies`, signal);
      if (!signal?.aborted && currentChat.current === id) setDependencies(result.packages);
    } catch (reason) {
      if (!signal?.aborted && currentChat.current === id) setDependencyError(friendlyRunError(reason));
    } finally {
      if (!signal?.aborted && currentChat.current === id) setDependencyLoading(false);
    }
  }, []);
  useEffect(() => {
    setDetail(null); setChanges([]); setSources([]); setPanel(null); setFileDrawer(undefined); setProposal(undefined); setPackageSelection(null); setDependencies([]); setScanProgress(null); setResumeQuery(null); setError(""); setNotice("");
    setPanelErrors({ changes: "", sources: "" }); setPanelLoading({ changes: true, sources: true });
    if (!chatId || location.pathname !== "/projects") return;
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>; let tick = 0; let sequence = 0;
    const isCurrent = () => !controller.signal.aborted && currentChat.current === chatId && currentProject.current === projectId && route.current === "/projects";
    const url = endpoint(chatId);
    let liveDetail: ChatDetail | null = null;
    const stream = typeof EventSource === "undefined" ? null : new EventSource(`${url}/stream`);
    stream?.addEventListener("snapshot", event => {
      if (!isCurrent()) return;
      let snapshot: ChatDetail;
      try {
        snapshot = JSON.parse((event as MessageEvent<string>).data);
      } catch {
        liveDetail = null;
        return;
      }
      if (snapshot?.chat?.id !== chatId) return;
      liveDetail = normalizeDetail(snapshot);
      // Ignore detail requests that started before this streamed snapshot arrived.
      detailRevision.current += 1;
      setDetail(liveDetail);
    });
    stream?.addEventListener("error", () => { liveDetail = null; });
    if (pendingDependencyChat.current === chatId) {
      pendingDependencyChat.current = "";
      void openDependencySelection(chatId, controller.signal);
    }
    const poll = async () => {
      try {
        const snapshot = liveDetail ?? await refresh(chatId, controller.signal).catch(reason => {
          if (liveDetail) return liveDetail;
          throw reason;
        });
        if (!isCurrent()) return;
        if (!(liveDetail ?? snapshot).chat.archived) {
          const activity = await get<{ events: { sequence: number; type: string; run_id?: string; payload: ScanProgressEvent }[] }>(`${url}/events?after=${sequence}`, controller.signal);
          if (!isCurrent()) return;
          const liveRun = activeRun((liveDetail ?? snapshot).runs);
          let update: ScanProgressSnapshot | undefined;
          for (const event of activity.events) {
            sequence = Math.max(sequence, event.sequence);
            const runId = event.run_id ?? (liveRun?.kind === "scan" ? liveRun.id : undefined);
            if (event.type === "scan.progress" && runId) update = { runId, payload: event.payload };
          }
          if (update) setScanProgress(update);
          if (tick++ % 3 === 0) {
            const [c, s] = await Promise.allSettled([get<{ changes: Change[] }>(`${url}/changes`, controller.signal), get<{ sources: ChatSource[] }>(`${url}/sources`, controller.signal)]);
            if (isCurrent()) {
              if (c.status === "fulfilled") setChanges(c.value.changes);
              if (s.status === "fulfilled") setSources(s.value.sources);
              setPanelErrors({ changes: c.status === "rejected" ? friendlyRunError(c.reason) : "", sources: s.status === "rejected" ? friendlyRunError(s.reason) : "" });
              setPanelLoading({ changes: false, sources: false });
            }
          }
        }
      } catch (reason) { if (isCurrent()) setError(friendlyRunError(reason)); }
      if (isCurrent()) timer = setTimeout(poll, 1800);
    };
    void poll();
    return () => { controller.abort(); stream?.close(); clearTimeout(timer); detailRevision.current += 1; };
  }, [projectId, chatId, location.pathname, refresh, openDependencySelection]);
  const act = async (work: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await work(); } catch (reason) { setError(friendlyRunError(reason)); } finally { setBusy(false); }
  };
  const createChat = async (request: FirstRequest) => {
    const model = draftModel;
    if (!model) throw new Error("Choose an available model before starting a conversation.");
    const created = readProvisionalChat(projectId);
    await new Promise<void>((resolve, reject) => setLoadTarget({
      kind: "create", projectId, draftKey, model, request, key: created?.key || requestKey(), resolve, reject,
      created, reopened: Boolean(created),
    }));
  };
  const selectModel = (model: Model) => {
    if (!chatId) {
      if (provisionalChat || isCloud(model) || model.tool_protocol === "text") setDraftModelOverride({ projectId, model });
      else { setDraftModelOverride(undefined); setModelId(model.id); }
      return;
    }
    if (modelSwitchDisabled || submissionLock.current || (isSameModel(detail?.chat.model, model) && !isCloud(model))) return;
    setNotice("");
    setLoadTarget({ kind: "switch", chatId, previousModelId: detail?.chat.model?.id, model });
  };
  const changeModel = (id: string) => {
    const model = agentModels.find(item => item.id === id && runnable(item));
    if (!model) { setError("The selected model is no longer available. Refresh the installed model list."); return; }
    if (isCloud(model)) {
      setCloudConsent({ model, projectId, chatId });
      return;
    }
    selectModel(model);
  };
  const refreshSubmitted = async (id: string) => {
    try { await refresh(id); }
    catch (reason) { if (currentChat.current === id) setError(`Your request was submitted, but the conversation could not refresh. ${friendlyRunError(reason)}`); }
  };
  const warmRuntime = async (id: string, model: Model, report?: (status: PreparationProgress) => void, signal?: AbortSignal) => {
    if (model.tool_protocol === "text") return;
    report?.({ phase: "starting_runtime", message: "Preparing this chat's coding agent and tools...", cancellable: true });
    if (currentChat.current === id && !signal?.aborted) setRuntimeWarm({ chat_id: id, model_id: model.id, ready: false, state: "loading", message: "Preparing this chat's coding agent and tools..." });
    try {
      await prepareChatRuntime(id, model.id, status => {
        report?.({ ...status, cancellable: true });
        if (currentChat.current === id && !signal?.aborted) setRuntimeWarm(status);
      }, signal);
      if (currentChat.current === id && !signal?.aborted) setRuntimeWarm(null);
    } catch (reason) {
      if (currentChat.current === id && !signal?.aborted) setRuntimeWarm({ chat_id: id, model_id: model.id, ready: false, state: "failed", message: friendlyRunError(reason) });
      throw reason;
    }
  };
  const modelReady = async (preparation: Preparation, report: (status: PreparationProgress) => void, signal: AbortSignal) => {
    const target = loadTarget; if (!target) return;
    if (target.kind === "create") {
      const onDraft = () => currentProject.current === target.projectId && currentDraft.current === target.draftKey && !currentChat.current && route.current === "/projects";
      if (!onDraft()) {
        target.reject(new Error("The active project changed. Your request was not sent."));
        setLoadTarget(null);
        return;
      }
      report({ phase: "creating_chat", message: "Creating the conversation...", cancellable: false });
      let created = target.created;
      if (!created) {
        const result = await write<Chat | { chat: Chat }>(`/api/ide/projects/${encodeURIComponent(target.projectId)}/chats`, { title: "New chat", model: preparation.model, preparation_id: preparation.id });
        const chat = "chat" in result ? result.chat as Chat : result;
        created = { id: chat.id, model: chat.model || preparation.model };
      } else if (created.model.id !== preparation.model.id) {
        const chat = await write<Chat>(`${endpoint(created.id)}/model`, { model: preparation.model, preparation_id: preparation.id, expected_model_id: created.model.id });
        created = { id: chat.id, model: chat.model || preparation.model };
      }
      localStorage.setItem(provisionalKey(target.projectId), JSON.stringify({ ...created, key: target.key }));
      if (signal.aborted) return;
      setLoadTarget(previous => previous?.kind === "create" ? { ...previous, created } : previous);

      if (target.request.kind === "turn") await warmRuntime(created.id, created.model, report, signal);
      if (signal.aborted) return;
      if (!onDraft()) {
        target.reject(new Error("The active project changed. Your request was not sent."));
        setLoadTarget(null);
        return;
      }
      const { request } = target;
      report({ phase: "submitting_prompt", message: "Submitting your request...", cancellable: false });
      if (request.kind === "turn") await write(`${endpoint(created.id)}/turns`, { request_key: target.key, text: request.text, mode: request.mode });
      else if (request.command.dependency_mode === "source") pendingDependencyChat.current = created.id;
      else await submitScan(created.id, request.command, target.key);
      localStorage.removeItem(provisionalKey(target.projectId));
      setDraftModelOverride(undefined);
      const newChat = { ...created, title: "New chat" };
      invalidateChatList(target.projectId);
      setChats(previous => ({ ...previous, [target.projectId]: [newChat, ...(previous[target.projectId] || []).filter(chat => chat.id !== newChat.id)] }));
      clearWorkspaceValue(`agent.draft.${target.draftKey}`);
      target.resolve();
      if (onDraft()) selectChat(target.projectId, created.id, true);
      setLoadTarget(null);
    } else if (target.kind === "switch") {
      const changed = target.applied || await write<Chat>(`${endpoint(target.chatId)}/model`, { model: preparation.model, preparation_id: preparation.id, ...(target.previousModelId ? { expected_model_id: target.previousModelId } : {}) });
      setLoadTarget(previous => previous?.kind === "switch" ? { ...previous, applied: changed } : previous);
      detailRevision.current += 1;
      setChats(previous => Object.fromEntries(Object.entries(previous).map(([id, rows]) => [id, rows.map(chat => chat.id === changed.id ? { ...chat, ...changed } : chat)])));
      if (currentChat.current === target.chatId) setDetail(previous => previous ? { ...previous, chat: { ...previous.chat, ...changed } } : previous);
      await warmRuntime(target.chatId, preparation.model, report, signal);
      if (currentChat.current === target.chatId && !signal.aborted) setNotice(`Model changed to ${changed.model?.name || preparation.model.name}.`);
      setLoadTarget(null);
    } else {
      try {
        if (currentChat.current !== target.chatId || route.current !== "/projects") throw new Error("The active chat changed. Your message was not sent.");
        await warmRuntime(target.chatId, preparation.model, report, signal);
        if (signal.aborted || currentChat.current !== target.chatId || route.current !== "/projects") throw new Error("The active chat changed. Your message was not sent.");
        report({ phase: "submitting_prompt", message: "Sending your message...", cancellable: false });
        if (target.kind === "resume") await write(`${endpoint(target.chatId)}/runs/${target.runId}/resume`);
        else await write(`${endpoint(target.chatId)}/turns`, { request_key: requestKey(), text: target.text, mode: target.mode });
        await refreshSubmitted(target.chatId); target.resolve();
      }
      catch (reason) { target.reject(new Error(friendlyRunError(reason))); }
      finally { setLoadTarget(null); }
    }
  };
  const send = async (text: string, save: boolean, mode: TurnMode) => {
    const id = chatId;
    if (!text.trim() || submissionLock.current || (id && sendingFor.current.has(id))) return;
    const selectedMode = (id ? detail?.chat.model : draftModel)?.tool_protocol === "text" ? "plan" : mode;
    if (!id) {
      submissionLock.current = true; setBusy(true);
      try { await createChat({ kind: "turn", text, mode: selectedMode }); }
      finally { submissionLock.current = false; setBusy(false); }
      return;
    }
    // The lock and the busy state belong to this conversation, so another conversation stays usable.
    const controller = new AbortController();
    sendingFor.current.set(id, controller);
    setSending(previous => new Set(previous).add(id));
    try {
      if (save) {
        await saveFileDrafts(id, (path, body, version) => write(`${endpoint(id)}/file`, { path, text: body, version }, "PUT"));
        if (currentChat.current === id) void refreshChanges().catch(() => { /* ignored: the changes panel retries on its own */ });
      }
      const model = detail?.chat.model || models.find(item => item.id === modelId);
      if (!model || model.kind === "scan_only") throw new Error("Choose a compatible chat model before sending a message.");
      const status = await get<{ ready: boolean; model_id?: string; phase?: string; message?: string }>(`${endpoint(id)}/model-status`);
      if (currentChat.current !== id || route.current !== "/projects") throw new Error("The active chat changed. Your message was not sent.");
      if (status.phase === "unavailable") throw new Error(status.message || "Select an available model for this conversation.");
      if (status.ready && status.model_id === model.id) {
        await warmRuntime(id, model, undefined, controller.signal);
        if (currentChat.current !== id || route.current !== "/projects") throw new Error("The active chat changed. Your message was not sent.");
        await write(`${endpoint(id)}/turns`, { request_key: requestKey(), text, mode: selectedMode }); await refreshSubmitted(id);
      } else await new Promise<void>((resolve, reject) => setLoadTarget({ kind: "send", chatId: id, text, mode: selectedMode, model, resolve, reject }));
    } catch (reason) {
      if (currentChat.current !== id) {
        const title = (projectChats || []).find(chat => chat.id === id)?.title;
        setNotice(`Your message${title ? ` to "${title}"` : ""} was not sent because you switched conversations. It is still in that conversation's composer.`);
      }
      throw reason;
    } finally {
      if (sendingFor.current.get(id) === controller) sendingFor.current.delete(id);
      setSending(previous => { const next = new Set(previous); next.delete(id); return next; });
    }
  };
  useEffect(() => {
    for (const [id, controller] of sendingFor.current) if (id !== chatId) controller.abort();
  }, [chatId]);
  const resumeSaved = async () => {
    const id = chatId, model = detail?.chat.model, runId = detail?.recovery?.run_id;
    if (!id || !model || !runId || detail?.recovery?.question || submissionLock.current || sendingFor.current.has(id)) return;
    submissionLock.current = true;
    try {
      const status = await get<{ ready: boolean; model_id?: string }>(`${endpoint(id)}/model-status`);
      if (currentChat.current !== id || route.current !== "/projects") return;
      if (!status.ready || status.model_id !== model.id) {
        await new Promise<void>((resolve, reject) => setLoadTarget({ kind: "resume", chatId: id, model, runId, resolve, reject }));
      } else {
        await write(`${endpoint(id)}/runs/${runId}/resume`);
        await refreshSubmitted(id);
      }
    } finally { submissionLock.current = false; }
  };
  const recovery = detail?.recovery;
  useEffect(() => {
    if (location.pathname !== "/projects" || busy || active || !recovery || recovery.question || recovery.uncertain.length || recovery.attempts
        || automaticResume.current.has(recovery.run_id)) return;
    automaticResume.current.add(recovery.run_id);
    void act(resumeSaved);
  }, [location.pathname, chatId, recovery?.run_id, Boolean(recovery?.question), busy, active?.id]);
  const submitScan = (id: string, command: ScanCommand, key: string, selected?: string[]) => write(`${endpoint(id)}/scans`, {
    request_key: key, scope: command.scope,
    ...(command.dependency_mode ? { dependency_mode: command.dependency_mode } : {}),
    ...(selected ? { selected_package_ids: selected } : {}),
  });
  const startScan = async (command: ScanCommand, selected?: string[]) => {
    if (command.dependency_mode === "source" && (detail?.chat.model || draftModel)?.tool_protocol === "text")
      throw new Error("Choose a model with project tools before reviewing dependency source.");
    if (chatId && command.dependency_mode === "source" && !selected) {
      await openDependencySelection(chatId);
      return;
    }
    if (submissionLock.current) return;
    submissionLock.current = true; setBusy(true);
    setStartingScan(command);
    try {
      if (!chatId && (command.scope === "project_code" || command.dependency_mode === "versions")) {
        const created = await write<Chat>("/api/ide/projects/" + encodeURIComponent(projectId) + "/scan-chat");
        if (currentProject.current !== projectId || currentChat.current || route.current !== "/projects") return;
        await submitScan(created.id, command, requestKey());
        if (currentProject.current !== projectId || currentChat.current || route.current !== "/projects") return;
        invalidateChatList(projectId);
        setChats(previous => ({ ...previous, [projectId]: [created, ...(previous[projectId] || []).filter(chat => chat.id !== created.id)] }));
        carryWorkspaceDraft(`agent.draft.${draftKey}`, `agent.draft.${created.id}`);
        selectChat(projectId, created.id, true);
        return;
      }
      if (!chatId) { await createChat({ kind: "scan", command }); return; }
      await submitScan(chatId, command, requestKey(), selected); setPackageSelection(null); setPanel(null); await refreshSubmitted(chatId);
    }
    finally { submissionLock.current = false; setBusy(false); setStartingScan(null); }
  };

  const openingModel = detail?.chat.model;
  useEffect(() => {
    setRuntimeWarm(null);
    if (!chatId || !openingModel || openingModel.kind === "scan_only" || openingModel.tool_protocol === "text" || detail?.chat.archived || loadTarget || active || route.current !== "/projects") return;
    const controller = new AbortController();
    const open = async () => {
      try {
        const status = await get<RuntimePreparation>(`${endpoint(chatId)}/runtime-status`, controller.signal);
        if (controller.signal.aborted || (status.ready && status.model_id === openingModel.id)) return;
        if (status.state === "failed") { setRuntimeWarm(status); return; }
        await warmRuntime(chatId, openingModel, undefined, controller.signal);
      } catch (reason) {
        if (!controller.signal.aborted && currentChat.current === chatId) setRuntimeWarm({ chat_id: chatId, model_id: openingModel.id, ready: false, state: "failed", message: friendlyRunError(reason) });
      }
    };
    void open();
    return () => controller.abort();
  }, [chatId, openingModel?.id, detail?.chat.archived, !!loadTarget, active?.id]);

  const runScanAction = (command: ScanCommand) => {
    if (scanDisabled || submissionLock.current) return;
    if (changesOpen) showChanges(null, false);
    setError("");
    void startScan(command).catch(reason => setError(friendlyRunError(reason)));
  };
  const commandEntries: PaletteEntry[] = project && !scanDisabled ? projectActions.flatMap(item => {
      const command = parseCommand(item.command);
    return command.kind === "scan" ? [{ id: item.command, title: item.label, detail: item.description, run: () => runScanAction(command) }] : [];
  }) : [];
  const sourceReviewModel = detail?.chat.model ?? draftModel;
  const workspaceActions = [
    ...destinations.map(item => ({ id: item.id, label: item.label, icon: <item.icon size={16} aria-hidden />,
      disabled: !detail || detail.chat.archived, run: () => { if (changesOpen) showChanges(null, false); openPanel(item.id); } })),
    { id: "dependency-source", label: "Review dependency source", icon: <PackageSearch size={16} aria-hidden />,
      disabled: scanDisabled || !sourceReviewModel || sourceReviewModel.kind === "scan_only" || sourceReviewModel.tool_protocol === "text",
      run: () => runScanAction({ kind: "scan", scope: "dependencies", dependency_mode: "source" }) },
  ];
  const updateConversation = async (id: string, fields: { title?: string; pinned?: boolean }) => {
    const updated = await write<Chat>(endpoint(id), fields, "PATCH");
    invalidateChatList(projectId);
    if (currentChat.current === id) detailRevision.current += 1;
    const patch = { title: updated.title, pinned: updated.pinned };
    setChats(previous => ({ ...previous, [projectId]: (previous[projectId] || []).map(chat => chat.id === id ? { ...chat, ...patch } : chat) }));
    setDetail(previous => previous?.chat.id === id ? { ...previous, chat: { ...previous.chat, ...patch } } : previous);
  };
  const deleteConversation = async (id: string) => {
    await write(endpoint(id), undefined, "DELETE");
    invalidateChatList(projectId);
    if (localStorage.getItem(`project-last-chat.${projectId}`) === id) localStorage.removeItem(`project-last-chat.${projectId}`);
    clearWorkspaceValue(`agent.draft.${id}`);
    fileDraftsFor(id).clear();
    if (currentProject.current === projectId && currentChat.current === id) {
      setResumeQuery(null);
      focusComposer.current = `project.${projectId}.new`;
      navigate(projectId, "", true, true);
    }
    setChats(previous => ({ ...previous, [projectId]: (previous[projectId] || []).filter(chat => chat.id !== id) }));
  };
  const title = detail?.chat.title || projectChats?.find(chat => chat.id === chatId)?.title;
  return <div className="project-ide project-chat" ref={surface}>
    <header className="ide-header"><button className="ide-icon" aria-label={navigation ? "Hide project navigation" : "Show project navigation"} aria-expanded={navigation} onClick={() => setNavigation(value => !value)}>{navigation ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />}</button>
      <div className="ide-project-identity chat-identity"><Folder size={16} aria-hidden /><span title={project?.name}>{project?.name || "Projects"}</span>{title && <><ChevronRight size={13} aria-hidden /><h2 title={title}>{title}</h2></>}</div>
      <div className="chat-header-actions">
        {project && <>
          <button type="button" className="ide-icon" aria-label="New conversation" title="New conversation" onClick={newConversation}><Plus size={17} aria-hidden /></button>
          <button type="button" className="ide-icon" aria-label="Resume conversation" title="Resume conversation" disabled={!detail && !showDraft} onClick={() => {
            if (changesOpen) showChanges(null, false);
            openHistory();
          }}><History size={16} aria-hidden /></button>
          <button ref={settingsButton} type="button" className="ide-icon" aria-label="Project settings" title="Project settings" onClick={() => setSettings(true)}><Settings2 size={16} aria-hidden /></button>
          {detail && !changesOpen && <button ref={filesButton} type="button" className="ide-icon" aria-label="Workspace files" title="Workspace files" aria-expanded={!!visibleFiles} onClick={event => visibleFiles ? closeFiles() : openFiles(undefined, event.currentTarget)}><FolderOpen size={17} aria-hidden /></button>}
        </>}
        <button ref={commandButton} type="button" className="chat-command-button" aria-label="Workspace commands" aria-haspopup="dialog" aria-keyshortcuts="Control+K Meta+K" onClick={event => openPalette(event.currentTarget)}><span>Commands</span><kbd>Ctrl+K</kbd></button>
        {project && <WorkspacePanel key={project.id} projectId={project.id} actions={workspaceActions} trigger={workspaceButton} onChanges={() => showChanges("changes")} />}
      </div>
    </header>
    {error && <div className="ide-banner ide-error" role="alert"><span>{error}</span><button className="ide-icon" aria-label="Dismiss error" onClick={() => setError("")}><X size={15} /></button></div>}
    {notice && <div className="ide-banner" role="status">{notice}<button className="ide-icon" aria-label="Dismiss notice" onClick={() => setNotice("")}><X size={15} /></button></div>}
    <div className="ide-body">
      {navigation && <ProjectNavigation projects={projects.map(item => ({ ...item, has_active_run: projectActivity[item.id] ?? item.has_active_run }))} projectId={projectId} onAdd={() => setAddProject(true)} onProject={id => { if (id !== projectId) selectChat(id); }} />}
      <div className="chat-main">
        {!project ? <div className="chat-welcome">{loading ? <p role="status">Loading projects...</p> : <><Folder size={30} /><h2>Create your first project</h2><p>Choose a source folder to start working with the agent.</p><button className="ide-primary" onClick={() => setAddProject(true)}>Create project</button></>}</div> : <>
          <section className="chat-stage" aria-label="Project chat" hidden={changesOpen}>
            {chatErrors[projectId] && <div className="ide-banner" role="alert">{chatErrors[projectId]}<button onClick={() => void loadChats(projectId)}>Retry conversations</button></div>}
            {runtimeWarm?.chat_id === chatId && !runtimeWarm.ready && !loadTarget && <div className="chat-runtime-readiness" role="status"><span>{runtimeWarm.message || "Preparing the coding agent..."}</span>{runtimeWarm.elapsed_seconds != null && <time>{clockSpan(Math.floor(runtimeWarm.elapsed_seconds))}</time>}{runtimeWarm.state === "failed" && <button onClick={() => { if (detail?.chat.model) void warmRuntime(chatId, detail.chat.model).catch(() => { /* ignored: warmRuntime renders the failed preparation state. */ }); }}>Retry agent preparation</button>}</div>}
            {!detail && !showDraft && !chatErrors[projectId] && <div className="chat-panel-empty" role="status">Loading conversation...</div>}
            {(detail || showDraft) && <Conversation key={draftKey} draftKey={draftKey} detail={detail} models={composerModels} onRefreshModels={reloadModels} modelId={chatId ? modelId : draftModel?.id || ""} onModel={changeModel} dirtyCount={chatId ? unsavedFiles : 0} busy={working} onSend={send} onScan={startScan} onResumeSaved={() => void act(resumeSaved)} onCancel={() => void act(async () => { if (active) await write(`${endpoint(chatId)}/runs/${active.id}/cancel`); await refresh(chatId); })} onPermission={(id, reply) => act(async () => { await write(`${endpoint(chatId)}/permissions/${id}`, { reply }); await refresh(chatId); })} onAnswer={async (id, answers) => { await write(`${endpoint(chatId)}/questions/${id}`, { answers }); await refreshSubmitted(chatId); }}
              history={{ chats: projectChats || [], query: resumeQuery, onOpen: openHistory, onClose: () => setResumeQuery(null),
                onUpdate: updateConversation, onDelete: deleteConversation,
                onPick: id => { setResumeQuery(null); focusComposer.current = id; selectChat(projectId, id); } }}
              onNew={newConversation} scanProgress={scanProgress} startingScan={startingScan} onOpenReference={detail ? openReference : undefined}
              onDiff={() => showChanges("changes")} onPropose={findingId => { setProposal({ chatId, findingId }); showChanges("agent-changes"); }}
              scanBusy={busy || !!loadTarget}
              sources={chatId ? sources : []} onAddSource={chatId && !detail?.chat.archived ? () => openPanel("sources", true) : undefined} onRemoveSource={!detail?.chat.archived ? id => void act(async () => { await write(`${endpoint(chatId)}/sources/${encodeURIComponent(id)}`, undefined, "DELETE"); await refreshSources(); }) : undefined} />}
          </section>
          {changesOpen && <section className="changes-workspace" aria-label="Changes workspace">
            <header>
              <button type="button" aria-label="Close changes" onClick={() => showChanges(null)}><ArrowLeft size={16} aria-hidden />Conversation</button>
              <h2 ref={changesHeading} tabIndex={-1}>Changes</h2>
              <div className="changes-views" role="group" aria-label="Change source">
                <button type="button" aria-pressed={!agentChanges} onClick={() => showChanges("changes")}>Working tree</button>
                <button type="button" aria-pressed={agentChanges} onClick={() => showChanges("agent-changes")}>Agent workspace{changes.length ? ` (${changes.length})` : ""}</button>
              </div>
            </header>
            <div className="changes-body">
              {!agentChanges ? <CheckoutChanges key={projectId} projectId={projectId} /> : chatId ? <>
                <PatchReview key={chatId} projectId={projectId} chatId={chatId} findingId={proposal?.chatId === chatId ? proposal.findingId : undefined}
                  busy={busy || !!active || !!detail?.chat.archived} onClearFinding={() => setProposal(undefined)}
                  onStaged={() => { setNotice("Patch staged for review."); void refreshChanges().catch(() => { /* ignored: refreshChanges shows the failure in the changes panel */ }); }} />
                {panelLoading.changes ? <p role="status">Loading workspace changes...</p> : panelErrors.changes ? <div role="alert"><p className="ide-error">{panelErrors.changes}</p>
                  <button onClick={() => void refreshChanges().catch(() => { /* ignored: refreshChanges shows the failure in the changes panel */ })}>Retry</button></div> :
                  <ChangesPanel key={chatId} chatId={chatId} changes={changes} busy={working || !!active || !!detail?.chat.archived} onIntegrated={() => {
                    setNotice("Reviewed files applied to the project."); void refreshChanges().catch(reason => setError(friendlyRunError(reason)));
                  }} />}
              </> : <p className="checkout-empty">Start a conversation to see agent workspace changes.</p>}
            </div>
          </section>}
          {visibleFiles && <WorkspaceFiles key={chatId} chatId={chatId} reference={visibleFiles.reference} drafts={visibleFiles.drafts}
            readOnly={working || !!active || !!detail?.chat.archived} onClose={closeFiles} onSaved={() => { if (currentChat.current === chatId) void refreshChanges().catch(() => { /* ignored: refreshChanges shows the failure in the changes panel */ }); }} />}
          {!changesOpen && panel && chatId && <DetailPanel key={`${chatId}-${panel}`} name={panelNames[panel]} onClose={closePanel}>
            {chatId && panel !== "dependencies" && <div className="chat-panel-tabs" aria-label="Switch chat panel">{destinations.map(item => <button key={item.id} className={panel === item.id ? "is-selected" : ""} aria-label={`Show ${item.label.toLowerCase()}`} title={item.label} onClick={() => openPanel(item.id)}><item.icon size={16} /></button>)}</div>}
            {panel === "sources" && (detailLoading ? <p role="status">Loading sources...</p> : detailError ? <div role="alert"><p className="ide-error">{detailError}</p><button onClick={() => void refreshSources().catch(() => { /* ignored: refreshSources shows the failure in the sources panel */ })}>Retry</button></div> : <SourcesPanel chatId={chatId} sources={sources} autoAdd={addingSource} onChange={refreshSources} />)}
            {(panel === "plan" || panel === "subagents") && <ObservedPanel chatId={chatId} kind={panel} />}
            {panel === "dependencies" && <div className="ide-dependencies"><h3>Select packages for source review</h3><p>Choose the exact dependencies to investigate. No source review starts until you confirm.</p>{dependencyLoading ? <p role="status">Loading dependency inventory...</p> : dependencyError ? <p className="ide-error" role="alert">{dependencyError}</p> : dependencies.length ? <Dependencies packages={dependencies} selection={packageSelection} onSelection={setPackageSelection} /> : <p>No resolvable dependencies found.</p>}<div className="ide-toolbar"><button className="ide-primary" disabled={working || !packageSelection?.length} onClick={() => void act(() => startScan({ kind: "scan", scope: "dependencies", dependency_mode: "source" }, packageSelection || []))}>Review {packageSelection?.length || "selected"} packages</button><button onClick={closePanel}>Cancel selection</button></div></div>}
          </DetailPanel>}
        </>}
      </div>
    </div>
    {addProject && <CreateProjectDialog onClose={() => setAddProject(false)} onCreated={created => { setProjects(items => [...items, created]); setChats(items => ({ ...items, [created.id]: [] })); navigate(created.id); setAddProject(false); }} />}
    {loadTarget && <ModelLoadingDialog model={loadTarget.model} purpose={loadTarget.kind === "switch" ? "switch" : loadTarget.kind === "create" ? "start" : "send"} allowShorter={loadTarget.kind === "create" ? !loadTarget.created || loadTarget.reopened : loadTarget.kind === "switch" ? !loadTarget.applied : false} onClose={() => { if (loadTarget.kind !== "switch") loadTarget.reject(new Error("Model loading cancelled.")); setLoadTarget(null); }} onReady={modelReady} />}
    {palette && <WorkbenchPalette title="Commands" entries={commandEntries} onClose={closePalette} />}
    {cloudConsent && <Dialog open onOpenChange={open => { if (!open) setCloudConsent(undefined); }} title={cloudConsent.model.kind === "hosted" ? "Allow hosted model?" : "Allow Ollama Cloud?"} description={cloudConsent.model.name + " runs remotely. Prompts and relevant source excerpts from this conversation may be sent to the model provider."}>
      <div className="flex flex-wrap gap-2"><button className="border border-line px-3 py-2" onClick={() => setCloudConsent(undefined)}>Cancel</button>
        <button className="border border-brand bg-brand px-3 py-2 text-on-brand" onClick={() => {
          const consent = cloudConsent;
          setCloudConsent(undefined);
          if (currentProject.current === consent.projectId && currentChat.current === consent.chatId) selectModel({ ...consent.model, allow_remote: true });
        }}>Allow for this conversation</button></div>
    </Dialog>}
    {settings && project && <ProjectSettingsDialog key={project.id} project={project}
      contextUsage={detail?.context_usage} savedMessages={detail?.messages.length}
      turnActive={!!active || (projectActivity[project.id] ?? !!project.has_active_run)}
      onClose={() => setSettings(false)} returnFocus={settingsButton}
      onSaved={updated => setProjects(items => items.map(item => item.id === updated.id ? updated : item))} />}
  </div>;
}
