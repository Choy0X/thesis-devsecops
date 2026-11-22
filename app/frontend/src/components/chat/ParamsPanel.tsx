import { useState, type ReactNode } from "react";
import type { ChatEndpoint, ChatParams } from "../../lib/api";
import { num } from "../../lib/format";
import { Dialog } from "../hud/Dialog";
import { Button, Hint, NumberField, Switch, Textarea } from "../kit";
import { TextPreviewField } from "./TextPreviewField";

// What the person can set for one conversation, and what it runs on: the model and where it runs (a model that is not on this computer is sent the lines of the files it reads), the
// system instructions, the extra instructions, the temperature, top p and the longest answer, and how much of the model's context the conversation has used. A number is saved when
// its field is left; empty means the model's own. The two instructions fields are long text, so they are collapsed to a preview: hovering (or focusing) reveals an edit button that
// opens a dialog with the full text and an explicit Save. The server checks every value and says what is wrong, and the panel shows that sentence.

const PROMPT_TOKENS = 700;                  // the instructions and the tool descriptions that go with every question

/** An estimate: four characters to a token, plus the instructions that go with every question. */
export const approxTokens = (chars: number) => PROMPT_TOKENS + Math.ceil(chars / 4);

/** The context meter in words; ``context`` is null when the model's size is not known. */
export function contextWords(used: number, context: number | null): string {
  return context ? `about ${num(used)} of ${num(context)} tokens of context` : `about ${num(used)} tokens so far; the model's context size is not known`;
}

// Starting numbers for a code/security assistant: low temperature for precise, low-hallucination answers, and a longest answer that leaves headroom in the default context after the
// system prompt and the tool descriptions. Shown whenever a value is not set yet (a new conversation, or an older one saved before these existed), so the fields are never blank; a
// person who clears a field on purpose still saves the model's own (null), the field just does not start empty.
export const DEFAULT_NUMBER_PARAMS: { temperature: number; top_p: number; max_tokens: number } = { temperature: 0.2, top_p: 0.9, max_tokens: 1024 };

const PROVIDER_WORDS: Record<string, string> = { brave: "Brave Search", google: "Google Custom Search", duckduckgo: "DuckDuckGo" };

type Draft = { temperature: string; top_p: string; max_tokens: string };
const toDraft = (p: ChatParams): Draft => ({
  temperature: String(p.temperature ?? DEFAULT_NUMBER_PARAMS.temperature),
  top_p: String(p.top_p ?? DEFAULT_NUMBER_PARAMS.top_p),
  max_tokens: String(p.max_tokens ?? DEFAULT_NUMBER_PARAMS.max_tokens),
});

/** A dialog that edits one long piece of text: an optional note shown above the box, an optional Revert next to Cancel and Save. */
function EditDialog({ title, value, maxLength, note, revertLabel, onRevert, onCancel, onSave }: {
  title: string;
  value: string;
  maxLength: number;
  note?: ReactNode;
  revertLabel?: string;
  onRevert?: () => Promise<void>;
  onCancel: () => void;
  onSave: (text: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(value);
  const [busy, setBusy] = useState(false);
  const dirty = draft.trim() !== value.trim();
  const save = async () => {
    if (!dirty || busy) return;
    setBusy(true);
    try { await onSave(draft.trim()); onCancel(); } finally { setBusy(false); }
  };
  const revert = async () => {
    if (!onRevert || busy) return;
    setBusy(true);
    try { await onRevert(); onCancel(); } finally { setBusy(false); }
  };
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onCancel(); }} title={title}>
      <div className="space-y-3">
        {note}
        <Textarea aria-label={title} rows={10} maxLength={maxLength} autoFocus disabled={busy} value={draft} onChange={(e) => setDraft(e.target.value)} />
        <div className="flex items-center justify-between gap-2">
          <div>{onRevert && <Button variant="ghost" size="sm" disabled={busy} onClick={() => void revert()}>{revertLabel}</Button>}</div>
          <div className="flex gap-2">
            <Button variant="subtle" disabled={busy} onClick={onCancel}>Cancel</Button>
            <Button variant="primary" disabled={!dirty || busy} onClick={() => void save()}>Save changes</Button>
          </div>
        </div>
      </div>
    </Dialog>
  );
}

export function ParamsPanel({ params, endpoint, context, used, disabled, error, onChange, onChangeInstructions, everTrimmed, everCompacted, extra, layout = "stacked", agent = false }: {
  params: ChatParams;
  endpoint: ChatEndpoint | null;
  context: number | null;
  /** Estimated tokens the conversation uses so far (see ``approxTokens``). */
  used: number;
  disabled?: boolean;
  /** The sentence the server gave when it refused a value. */
  error?: string;
  /** Save what changed: one key, or more. A number field that is empty is null. */
  onChange: (patch: Partial<ChatParams>) => void;
  /** Save the project's system instructions ("" reverts to the built-in default). */
  onChangeInstructions: (text: string) => Promise<void>;
  /** The oldest turns of this conversation were ever left out of what the model is sent (plain drop; nothing was summarized). */
  everTrimmed?: boolean;
  /** The oldest turns of this conversation were ever folded into a summary to save tokens, instead of being dropped. */
  everCompacted?: boolean;
  /** More settings below the ones every conversation has -- an agent session's own mode info and its auto-approve switch. */
  extra?: ReactNode;
  /** "stacked" (default): one narrow column, for the sidebar a conversation's settings have always used. "grid": the switches share a row and the three number fields share a
   *  row, for a dialog wide enough to take it -- the same fields, reflowed instead of stacked, so a dialog is not a long scroll of single-column rows. */
  layout?: "stacked" | "grid";
  /** An agent session: the web switch gives it the tools to search and to read a page, instead of searching for the question itself. */
  agent?: boolean;
}) {
  const [draft, setDraft] = useState<Draft>(() => toDraft(params));
  const [editingSystem, setEditingSystem] = useState(false);
  const [editingConvo, setEditingConvo] = useState(false);
  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));
  const same = (key: keyof Draft) => draft[key] === toDraft(params)[key];
  const save = (key: "temperature" | "top_p" | "max_tokens") => {
    if (same(key)) return;
    const text = draft[key].trim();
    onChange({ [key]: text === "" ? null : Number(text) });
  };
  const high = context !== null && used / context > 0.8;

  return (
    <section aria-label="This conversation's settings" className="space-y-4 text-[0.82rem]">
      <div className="space-y-1">
        <h2 className="text-[0.7rem] font-semibold uppercase tracking-wider text-faint">Model</h2>
        {!endpoint && <p className="text-muted">Looking for the model...</p>}
        {endpoint && !endpoint.configured && <p role="status" className="text-muted">No language model is set up. Start a local one under Model Hub, Serving, or give the address of a hosted one in Settings.</p>}
        {endpoint?.configured && (
          <>
            <p className="break-all font-mono text-[0.78rem]">{endpoint.model}</p>
            <p className="text-muted">{endpoint.local ? "Runs on this computer." : <span className="text-warn">Runs on {endpoint.host || "another computer"}, not this computer: the lines of the files it reads are sent there.</span>}</p>
            <Hint label="An estimate: four characters to a token, plus the instructions that go with every question." className={high ? "text-warn" : "text-muted"}>{contextWords(used, context)}</Hint>
            {everCompacted && <p className="text-muted">Earlier messages were summarized to save tokens; nothing was dropped.</p>}
            {everTrimmed && <p role="status" className="text-warn">Only the most recent messages are sent to the model; earlier ones are kept here but no longer reused.</p>}
          </>)}
      </div>
      <div className={layout === "grid" ? "grid grid-cols-2 gap-x-4" : "space-y-4"}>
        {endpoint?.system_prompt !== undefined && (
          <TextPreviewField label="System instructions" value={endpoint.system_prompt} placeholder="No system instructions." disabled={disabled} onEdit={() => setEditingSystem(true)} />
        )}
        <TextPreviewField label="Instructions" value={params.system} placeholder="Added after the system instructions, for this conversation only." disabled={disabled} onEdit={() => setEditingConvo(true)} />
      </div>
      <div className={layout === "grid" ? "grid grid-cols-2 gap-x-4 gap-y-4" : "space-y-4"}>
        <Switch label="Automatic compaction" description="When a conversation grows long, summarize its oldest turns to save tokens instead of dropping them. Off: they are dropped once the limit is reached."
          checked={params.autocompact} disabled={disabled} onCheckedChange={(v) => onChange({ autocompact: v })} />
        <Switch label="Search the web" disabled={disabled || !endpoint?.web_search_provider} checked={params.web_search} onCheckedChange={(v) => onChange({ web_search: v })}
          description={endpoint?.web_search_provider && agent
            ? `Gives the agent two tools: search the web through ${PROVIDER_WORDS[endpoint.web_search_provider] ?? endpoint.web_search_provider}, and read a page. Only the words of its query go out (never code or keys); it reads a page only from a result or an address you wrote, and asks you for any other. Pages are untrusted data, masked for credentials. Its sources are listed beside the thread.`
            : endpoint?.web_search_provider
            ? `Sends your question to ${PROVIDER_WORDS[endpoint.web_search_provider] ?? endpoint.web_search_provider} to search the web, and fetches matching pages to ground the answer. Pages are treated as untrusted data and masked for credentials, like the project's own files.`
            : "No web search provider is set up in Settings."} />
      </div>
      <div className={layout === "grid" ? "grid grid-cols-3 gap-x-4" : "space-y-4"}>
        <NumberField label="Temperature" hint="0 is steady and repeats itself, 2 is varied. Empty uses the model's own." min={0} max={2} step={0.1} disabled={disabled} value={draft.temperature}
          onChange={(e) => set({ temperature: e.target.value })} onBlur={() => save("temperature")} />
        <NumberField label="Top p" hint="Only the likeliest words that add up to this share are considered: 1 is all of them. Empty uses the model's own." min={0} max={1} step={0.05} disabled={disabled}
          value={draft.top_p} onChange={(e) => set({ top_p: e.target.value })} onBlur={() => save("top_p")} />
        <NumberField label="Longest answer" hint="In tokens. Empty lets the model decide." unit="tokens" min={1} max={65536} step={64} disabled={disabled} value={draft.max_tokens}
          onChange={(e) => set({ max_tokens: e.target.value })} onBlur={() => save("max_tokens")} />
      </div>
      {error && <p role="alert" className="text-crit-soft">{error}</p>}
      {extra}

      {editingSystem && endpoint?.system_prompt !== undefined && (
        <EditDialog title="System instructions" value={endpoint.system_prompt} maxLength={4000} onCancel={() => setEditingSystem(false)} onSave={onChangeInstructions}
          note={<p className="text-[0.8rem] text-muted">Sent before the Instructions below, on every conversation of this project.</p>}
          {...(endpoint.system_prompt_custom ? { revertLabel: "Revert to default", onRevert: () => onChangeInstructions("") } : {})} />
      )}
      {editingConvo && (
        <EditDialog title="Instructions" value={params.system} maxLength={4000} onCancel={() => setEditingConvo(false)} onSave={async (text) => onChange({ system: text })}
          note={<p className="text-[0.8rem] text-muted">Added after the system instructions, for this conversation. It still reads the project with its read-only tools and nothing else.</p>} />
      )}
    </section>
  );
}
