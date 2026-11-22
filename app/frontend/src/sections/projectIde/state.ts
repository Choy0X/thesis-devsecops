export type Command =
  | { kind: "text" }
  | { kind: "menu"; choices: { command: string; label: string }[] }
  | { kind: "invalid"; usage: string }
  | { kind: "scan"; scope: "dependencies" | "project_code" | "model_review"; dependency_mode?: "versions" | "source" }
  | { kind: "resume"; query: string }
  | { kind: "plan"; goal: string }
  | { kind: "diff" }
  | { kind: "new" }
  | { kind: "help" };

export type ScanCommand = Extract<Command, { kind: "scan" }>;
export const projectActions = [
  { command: "/scan dependencies versions", label: "Check dependencies", description: "Check dependency versions against public advisories." },
  { command: "/scan code", label: "Scan code", description: "Inspect the workspace with static rules." },
] as const;
export const commandUsage = "Commands: /plan [goal], /resume [title], /new, /scan code, /scan model, /scan dependencies versions, /scan dependencies source, /diff, /help.";
const scanUsage = "Use /scan code, /scan model, /scan dependencies versions, or /scan dependencies source.";
const terminalChoices = [
  { command: "/plan", label: "Create a read-only implementation plan" },
  { command: "/resume", label: "Resume a conversation" },
  { command: "/new", label: "Start a new conversation" },
  { command: "/scan", label: "Review project code or dependencies" },
  { command: "/diff", label: "Review changes" },
  { command: "/help", label: "Show commands" },
];
const scanChoices = [
  { command: "/scan code", label: "Review project code" },
  { command: "/scan model", label: "Review project code with the fine-tuned model" },
  { command: "/scan dependencies", label: "Review dependencies" },
];
const dependencyChoices = [
  { command: "/scan dependencies versions", label: "Check versions and known advisories" },
  { command: "/scan dependencies source", label: "Select packages for source review" },
];
const commandAliases = [
  { command: "/check dependencies", label: "Check versions and known advisories" },
  { command: "/patch review", label: "Review changes" },
];

export function parseCommand(input: string): Command {
  const text = input.trim();
  if (!text.startsWith("/")) return { kind: "text" };
  const words = text.toLowerCase().split(/\s+/);
  const normalized = words.join(" ");
  if (normalized === "/") return { kind: "menu", choices: terminalChoices };
  if (words[0] === "/plan") return { kind: "plan", goal: text.slice("/plan".length).trim() };
  if (words[0] === "/resume") return { kind: "resume", query: text.slice("/resume".length).trim() };
  if (normalized === "/new") return { kind: "new" };
  if (normalized === "/help") return { kind: "help" };
  if (normalized === "/diff" || normalized === "/patch review") return { kind: "diff" };
  if (normalized === "/check dependencies") return { kind: "scan", scope: "dependencies", dependency_mode: "versions" };
  if (normalized === "/scan") return { kind: "menu", choices: scanChoices };
  if (normalized === "/scan dependencies") return { kind: "menu", choices: dependencyChoices };
  if (normalized === "/scan code") return { kind: "scan", scope: "project_code" };
  if (normalized === "/scan model") return { kind: "scan", scope: "model_review" };
  const isScan = words[0] === "/scan";
  if (isScan && words.length === 3 && words[1] === "dependencies" && (words[2] === "versions" || words[2] === "source")) {
    return { kind: "scan", scope: "dependencies", dependency_mode: words[2] };
  }
  const candidates = isScan ? (words[1] === "dependencies" ? dependencyChoices : scanChoices) : [...terminalChoices, ...commandAliases];
  const choices = candidates.filter(choice => choice.command.startsWith(normalized));
  return choices.length ? { kind: "menu", choices } : { kind: "invalid", usage: isScan ? scanUsage : commandUsage };
}

export type FileBuffer = { path: string; text: string; saved: string; version: string; conflict?: boolean; remoteVersion?: string };
export const isDirty = (buffer: FileBuffer) => buffer.text !== buffer.saved;
/** Reconcile saved workspace changes without destroying an unsaved editor buffer. */
export function reconcileBuffer(buffer: FileBuffer, remote: { text: string; version: string }): FileBuffer {
  if (remote.version === buffer.version) return buffer;
  if (isDirty(buffer)) return { ...buffer, conflict: true, remoteVersion: remote.version };
  return { path: buffer.path, text: remote.text, saved: remote.text, version: remote.version };
}
/** A save response belongs to the submitted text, even if typing continued while saving. */
export function acceptSave(buffer: FileBuffer, submitted: string, version: string): FileBuffer {
  return { ...buffer, saved: submitted, version, conflict: false, remoteVersion: undefined };
}

export type PanelPreferences = { navigation: boolean; explorer: boolean; chatPercent: number; trayHeight: number; tray: string; focus: "editor" | "chat" };
export function fitChatPercent(percent: number, areaWidth: number, explorerWidth: number, editorMinimum = 260, separatorWidth = 4): number {
  if (areaWidth <= 0) return percent;
  const maximum = Math.min(65, 100 * (areaWidth - explorerWidth - editorMinimum - separatorWidth) / areaWidth);
  const minimum = Math.max(25, 30000 / areaWidth);
  if (maximum < minimum) return percent; // The compact layout switches between editor and agent.
  return Math.max(minimum, Math.min(maximum, percent));
}
export const defaultPanels: PanelPreferences = { navigation: true, explorer: true, chatPercent: 40, trayHeight: 220, tray: "changes", focus: "editor" };
export function readPanels(): PanelPreferences {
  try {
    const value = JSON.parse(localStorage.getItem("projects-ide-panels-v1") || "{}");
    return { ...defaultPanels, ...value, chatPercent: Math.max(25, Math.min(65, Number(value.chatPercent) || 40)), trayHeight: Math.max(120, Math.min(550, Number(value.trayHeight) || 220)) };
  } catch { return defaultPanels; }
}
