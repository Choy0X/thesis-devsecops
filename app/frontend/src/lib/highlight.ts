// A small, offline syntax colouring for one line of code: comments, strings, numbers and keywords. It is a reading aid for diffs, not a parser: a block
// comment or a multi-line string that spans lines is coloured line by line as plain code. No library, no network, and no cost beyond one regular
// expression per line.

export type TokenKind = "plain" | "comment" | "string" | "number" | "keyword";
export type Token = { text: string; kind: TokenKind };

const WORDS: Record<string, string> = {
  python: "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield self",
  javascript: "async await break case catch class const continue debugger default delete do else export extends finally for from function if import in instanceof let new of return static super switch this throw try typeof var void while with yield null undefined true false",
  go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var nil true false",
  java: "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long new package private protected public return short static super switch synchronized this throw throws try void volatile while null true false",
  c: "auto break case char const continue default do double else enum extern float for goto if inline int long register return short signed sizeof static struct switch typedef union unsigned void volatile while NULL true false",
  rust: "as async await break const continue crate dyn else enum extern fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait type unsafe use where while true false",
  php: "abstract and as break callable case catch class clone const continue declare default do echo else elseif extends final finally for foreach function global if implements include instanceof interface new namespace or private protected public require return static switch throw trait try use var while null true false",
  ruby: "alias and begin break case class def defined do else elsif end ensure false for if in module next nil not or redo rescue retry return self super then true undef unless until when while yield",
};
const FAMILY: Record<string, string> = {
  python: "python", py: "python", javascript: "javascript", js: "javascript", typescript: "javascript", ts: "javascript", tsx: "javascript", jsx: "javascript",
  go: "go", java: "java", kotlin: "java", csharp: "java", "c#": "java", c: "c", cpp: "c", "c++": "c", rust: "rust", php: "php", ruby: "ruby", rb: "ruby",
};
const HASH_COMMENTS = new Set(["python", "ruby", "php"]);
const KEYWORDS: Record<string, Set<string>> = Object.fromEntries(Object.entries(WORDS).map(([k, v]) => [k, new Set(v.split(" "))]));

const SCAN = /("(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?|`(?:\\.|[^`\\])*`?)|(\b0[xX][0-9a-fA-F_]+\b|\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|([A-Za-z_$][A-Za-z0-9_$]*)/g;

/** The tokens of one line, which together spell the line exactly. An unknown language gets comment, string and number colouring and no keywords. */
export function tokenize(line: string, language: string | null | undefined): Token[] {
  const family = FAMILY[(language ?? "").toLowerCase()] ?? "";
  const words = KEYWORDS[family];
  const out: Token[] = [];
  const push = (text: string, kind: TokenKind) => { if (text) out.push({ text, kind }); };

  // a line comment ends the code part of the line
  let code = line, comment = "";
  const hash = HASH_COMMENTS.has(family) || family === "";
  const slash = family !== "python" && family !== "ruby";
  let cut = -1, quote = "";
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quote) { if (c === "\\") i++; else if (c === quote) quote = ""; continue; }
    if (c === '"' || c === "'" || c === "`") { quote = c; continue; }
    if ((slash && c === "/" && line[i + 1] === "/") || (hash && c === "#" && (family !== "" || i === 0 || /\s/.test(line[i - 1])))) { cut = i; break; }
  }
  if (cut >= 0) { code = line.slice(0, cut); comment = line.slice(cut); }

  let last = 0;
  for (const m of code.matchAll(SCAN)) {
    push(code.slice(last, m.index), "plain");
    if (m[1]) push(m[0], "string");
    else if (m[2]) push(m[0], "number");
    else push(m[0], words?.has(m[0]) ? "keyword" : "plain");
    last = m.index! + m[0].length;
  }
  push(code.slice(last), "plain");
  push(comment, "comment");
  return out.reduce<Token[]>((merged, t) => {                      // neighbouring plain pieces become one
    const prev = merged[merged.length - 1];
    if (prev && prev.kind === "plain" && t.kind === "plain") prev.text += t.text; else merged.push({ ...t });
    return merged;
  }, []);
}

export const KIND_CLASS: Record<TokenKind, string> = {
  plain: "", comment: "text-faint italic", string: "text-chart-4", number: "text-chart-5", keyword: "text-chart-3",
};
