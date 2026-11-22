import { Check, Copy } from "lucide-react";
import { createContext, memo, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { IconButton } from "../kit";
import "./highlight.css";

// An answer of the model as markdown. Nothing in it is trusted: raw HTML is never rendered (react-markdown shows it as text), a link opens only when it is http, https or mailto, an image is
// never loaded (an address in an image is a way to send what the model read to another computer the moment the answer is drawn), and a heading is drawn as a bold line, not as a heading of
// the page (the page's own headings must go down one level at a time). Code is highlighted by the language the model named, with that language shown and a Copy button.

const SAFE_LINK = /^(https?:|mailto:)/i;
type HastNode = { type?: string; tagName?: string; value?: string; properties?: { className?: string[] }; children?: HastNode[] };
export type WorkspaceReference = { path: string; line?: number; directory?: boolean };
export type OpenWorkspaceReference = (reference: WorkspaceReference, trigger: HTMLElement) => void;
const WorkspaceReferences = createContext<OpenWorkspaceReference | undefined>(undefined);

function workspaceReference(value: string, explicit: boolean): WorkspaceReference | undefined {
  if (explicit) {
    try { value = decodeURIComponent(value); } catch { return; }
  }
  const citation = value.trim().replace(/\\/g, "/").replace(/^\.\//, "");
  const match = /^([^:#]+?)(?:(?::|#L)([1-9]\d*)(?:[-:](?:L)?\d+)?)?$/.exec(citation);
  if (!match) return;
  const directory = match[1].endsWith("/");
  const path = match[1].replace(/\/$/, "");
  if (!path.split("/").every(segment => /^[\p{L}\p{N}._@+() -]+$/u.test(segment) && !/^\.{1,2}$/.test(segment))) return;
  if (!explicit && !directory && !path.includes("/") && !/^(?:[^ ]+\.[\w-]+|Dockerfile|Containerfile|Makefile|GNUmakefile|LICENSE|README|Gemfile|Rakefile|Jenkinsfile)$/i.test(path)) return;
  const line = Number(match[2]);
  return { path, directory, line: Number.isSafeInteger(line) ? line : undefined };
}

function InlineCode({ className, children }: { className?: string; children?: ReactNode }) {
  const open = useContext(WorkspaceReferences);
  const reference = open && typeof children === "string" ? workspaceReference(children, false) : undefined;
  const style = className || "bg-surface-3 px-1 py-px font-mono text-[0.78rem]";
  return open && reference ? <button type="button" className="chat-file-reference" title={`Open ${reference.path} in workspace`} onClick={event => open(reference, event.currentTarget)}>
    <code className={style}>{children}</code>
  </button> : <code className={style}>{children}</code>;
}

function MarkdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  const open = useContext(WorkspaceReferences);
  const external = href && SAFE_LINK.test(href);
  const reference = open && href && !external ? workspaceReference(href, true) : undefined;
  return <WorkspaceReferences.Provider value={undefined}>{external
    ? <a href={href} target="_blank" rel="noopener noreferrer" className="break-all text-brand-strong underline hover:text-fg">{children}</a>
    : open && reference ? <button type="button" className="chat-file-reference" title={`Open ${reference.path} in workspace`} onClick={event => open(reference, event.currentTarget)}>{children}</button>
      : <span className="break-all">{children}</span>}
  </WorkspaceReferences.Provider>;
}

/** The plain text of a node of the syntax tree: what Copy puts on the clipboard (the highlighting spans are not in it). */
export function textOf(node: HastNode | undefined): string {
  if (!node) return "";
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(textOf).join("");
}

/** What a table is called to a screen reader: its column headings ("Table: file, line"), since an answer's table has no caption of its own. */
export function tableName(node: HastNode | undefined): string {
  const row = (function find(n: HastNode | undefined): HastNode | undefined { return !n ? undefined : n.tagName === "tr" ? n : (n.children ?? []).map(find).find(Boolean); })(node);
  const heads = (row?.children ?? []).filter((c) => c.tagName === "th" || c.tagName === "td").map((c) => textOf(c).trim()).filter(Boolean);
  return heads.length ? `Table: ${heads.join(", ")}` : "A table in the answer";
}

function CodeBlock({ node, children }: { node?: HastNode; children?: ReactNode }) {
  const code = node?.children?.find((c) => c.tagName === "code");
  const language = (code?.properties?.className ?? []).find((c) => c.startsWith("language-"))?.slice("language-".length) ?? "";
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => () => { if (timer.current != null) window.clearTimeout(timer.current); }, []);
  const copy = async () => {
    try { await navigator.clipboard.writeText(textOf(code ?? node).replace(/\n$/, "")); }
    catch { return; }                                                          // ignored: a window that refuses the clipboard shows no "Copied", so nobody is told something false
    setCopied(true);
    if (timer.current != null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1800);
  };
  return (
    <div className="my-2 border border-line bg-terminal">
      <div className="flex items-center justify-between border-b border-line bg-surface-2 px-3 py-1">
        <span className="font-mono text-[0.7rem] text-muted">{language || "text"}</span>
        <IconButton label={copied ? "Copied" : "Copy the code"} size="sm" onClick={copy}>{copied ? <Check size={13} aria-hidden /> : <Copy size={13} aria-hidden />}</IconButton>
        <span role="status" className="sr-only">{copied ? "Copied to the clipboard" : ""}</span>
      </div>
      <WorkspaceReferences.Provider value={undefined}><pre className="overflow-x-auto px-3 py-2 font-mono text-[0.76rem] leading-relaxed [&_code]:bg-transparent [&_code]:p-0">{children}</pre></WorkspaceReferences.Provider>
    </div>
  );
}

const heading = ({ children }: { children?: ReactNode }) => <p className="mt-3 text-[0.9rem] font-semibold first:mt-0">{children}</p>;

const components: Components = {
  pre: CodeBlock,
  code: InlineCode,
  a: MarkdownLink,
  img: ({ alt }) => <span className="text-faint">[image{alt ? `: ${alt}` : ""}, not loaded]</span>,
  h1: heading, h2: heading, h3: heading, h4: heading, h5: heading, h6: heading,
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>,
  blockquote: ({ children }) => <blockquote className="my-2 border-l-2 border-line-strong pl-3 text-muted">{children}</blockquote>,
  hr: () => <hr className="my-3 border-line" />,
  table: ({ node, children }) => <div className="my-2 overflow-x-auto"><table aria-label={tableName(node as HastNode | undefined)} className="w-full border-collapse text-[0.8rem]">{children}</table></div>,
  th: ({ children }) => <th className="border border-line bg-surface-2 px-2 py-1 text-left font-semibold">{children}</th>,
  td: ({ children }) => <td className="border border-line px-2 py-1 align-top">{children}</td>,
};

/** The markdown of an answer. Re-drawn only when the text changes, because an answer that is being written is drawn again for every few characters. */
export const Markdown = memo(function Markdown({ text, onOpenReference }: { text: string; onOpenReference?: OpenWorkspaceReference }) {
  return (
    <div className="chat-md break-words text-[0.86rem] leading-relaxed">
      <WorkspaceReferences.Provider value={onOpenReference}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]} components={components}>{text}</ReactMarkdown>
      </WorkspaceReferences.Provider>
    </div>
  );
});
