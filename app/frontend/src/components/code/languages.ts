import { StreamLanguage } from "@codemirror/language";
import type { Extension } from "@codemirror/state";

// The grammar for a file, found from its name and loaded only when a file of that kind is shown: each language is its own chunk, so the first page and a view of Python carry no Rust.
// A name with no grammar here (or one that fails to load) is shown as plain text, never as an error.

const lowered = (path: string) => (path.split(/[\\/]/).pop() ?? "").toLowerCase();

/** The extension of a file name, without the dot ("" for a name with none, and for a dotfile such as ``.gitignore``, which has no extension, only a name). */
export function extensionOf(path: string): string {
  const name = lowered(path);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1) : "";
}

/** A short name for what the viewer will treat the file as, or null when it is plain text. Used for the label and by the tests. */
export function languageName(path: string): string | null {
  const name = lowered(path);
  const ext = extensionOf(path);
  if (name === "dockerfile" || name.startsWith("dockerfile.")) return "dockerfile";
  if (name === "makefile" || name === "gnumakefile") return null;
  const map: Record<string, string> = {
    ts: "typescript", mts: "typescript", cts: "typescript", tsx: "tsx", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "jsx", py: "python", pyi: "python", json: "json", jsonc: "json",
    webmanifest: "json", css: "css", scss: "css", html: "html", htm: "html", vue: "html", svelte: "html", md: "markdown", markdown: "markdown", sql: "sql", yml: "yaml", yaml: "yaml", rs: "rust",
    go: "go", java: "java", c: "cpp", h: "cpp", cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp", php: "php", sh: "shell", bash: "shell", zsh: "shell", toml: "toml", xml: "xml", svg: "xml",
    ps1: "powershell", rb: "ruby", kt: "kotlin", swift: "swift",
  };
  return map[ext] ?? null;
}

/** The CodeMirror extension that colours the file, or null for plain text. */
export async function languageFor(path: string): Promise<Extension | null> {
  switch (languageName(path)) {
    case "typescript": return (await import("@codemirror/lang-javascript")).javascript({ typescript: true });
    case "tsx": return (await import("@codemirror/lang-javascript")).javascript({ typescript: true, jsx: true });
    case "javascript": return (await import("@codemirror/lang-javascript")).javascript();
    case "jsx": return (await import("@codemirror/lang-javascript")).javascript({ jsx: true });
    case "python": return (await import("@codemirror/lang-python")).python();
    case "json": return (await import("@codemirror/lang-json")).json();
    case "css": return (await import("@codemirror/lang-css")).css();
    case "html": return (await import("@codemirror/lang-html")).html();
    case "markdown": return (await import("@codemirror/lang-markdown")).markdown();
    case "sql": return (await import("@codemirror/lang-sql")).sql();
    case "yaml": return (await import("@codemirror/lang-yaml")).yaml();
    case "rust": return (await import("@codemirror/lang-rust")).rust();
    case "go": return (await import("@codemirror/lang-go")).go();
    case "java": return (await import("@codemirror/lang-java")).java();
    case "cpp": return (await import("@codemirror/lang-cpp")).cpp();
    case "php": return (await import("@codemirror/lang-php")).php();
    case "shell": return StreamLanguage.define((await import("@codemirror/legacy-modes/mode/shell")).shell);
    case "toml": return StreamLanguage.define((await import("@codemirror/legacy-modes/mode/toml")).toml);
    case "dockerfile": return StreamLanguage.define((await import("@codemirror/legacy-modes/mode/dockerfile")).dockerFile);
    case "xml": return StreamLanguage.define((await import("@codemirror/legacy-modes/mode/xml")).xml);
    case "powershell": return StreamLanguage.define((await import("@codemirror/legacy-modes/mode/powershell")).powerShell);
    case "ruby": return StreamLanguage.define((await import("@codemirror/legacy-modes/mode/ruby")).ruby);
    case "kotlin": return StreamLanguage.define((await import("@codemirror/legacy-modes/mode/clike")).kotlin);
    case "swift": return StreamLanguage.define((await import("@codemirror/legacy-modes/mode/swift")).swift);
    default: return null;
  }
}
