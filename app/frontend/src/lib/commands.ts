// What "/" can mean in the chat composer, modeled on Claude Code and Codex. A leading "/word" is a command only when the word matches one of these names as a whole token; anything
// else, including an unrecognized "/something", is sent unchanged as an ordinary question (so a question that happens to start with a path, for instance, is never swallowed).
// Three of these (batch, agents, schedule) are recognized here so the composer can list and autocomplete them, but have no real implementation yet: running one just says so.

export type CommandArgKind = "none" | "optional-text" | "required-text";

export type CommandSpec = { name: string; args: CommandArgKind; stub?: boolean; hint: string };

export const COMMANDS: CommandSpec[] = [
  { name: "resume", args: "optional-text", hint: "Switch to a conversation by name, or open the list." },
  { name: "compact", args: "none", hint: "Summarize the oldest turns now, to save tokens." },
  { name: "clear", args: "none", hint: "Empty this conversation." },
  { name: "autocompact", args: "optional-text", hint: "Turn automatic compaction on, off, or toggle it." },
  { name: "context", args: "none", hint: "How much of the model's context this conversation uses." },
  { name: "deep-research", args: "required-text", hint: "Ask with more tool rounds and more retrieved excerpts." },
  { name: "plan", args: "required-text", hint: "Ask for a plan instead of a direct answer." },
  { name: "security-review", args: "none", hint: "Run a security review of this project." },
  { name: "status", args: "none", hint: "The model and GPU status." },
  { name: "usage", args: "none", hint: "Tokens used so far in this conversation." },
  { name: "skill", args: "required-text", hint: "Apply a prepared skill's instructions to this conversation." },
  { name: "batch", args: "none", stub: true, hint: "Not available yet." },
  { name: "agents", args: "none", stub: true, hint: "Not available yet." },
  { name: "schedule", args: "none", stub: true, hint: "Not available yet." },
];

const BY_NAME = new Map(COMMANDS.map((c) => [c.name, c]));

/** The command a line of the composer names, or null when it is not one (an ordinary question, or an unrecognized leading "/"). */
export function parseCommand(text: string): { name: string; arg: string } | null {
  const m = /^\/(\S+)(?:[ \t]+([\s\S]*))?$/.exec(text.trim());
  if (!m) return null;
  const name = m[1].toLowerCase();
  if (!BY_NAME.has(name)) return null;
  return { name, arg: (m[2] ?? "").trim() };
}

/** Commands whose name starts with ``prefix`` (the word typed so far, without its leading "/"), for the composer's menu. Everything when ``prefix`` is empty. */
export function matchCommands(prefix: string): CommandSpec[] {
  const p = prefix.toLowerCase();
  return COMMANDS.filter((c) => c.name.startsWith(p));
}
