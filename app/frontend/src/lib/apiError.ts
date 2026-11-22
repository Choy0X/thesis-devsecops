// One error type for every failed request. The backend answers errors with {ok: false, error, detail, hint, code}
// (app/backend/errors.py); older or foreign bodies are read leniently so a toast never shows raw JSON.

const TITLES: Record<number, string> = {
  0: "Cannot reach the app",
  400: "The request was not accepted",
  401: "Sign-in is needed",
  403: "Not allowed",
  404: "Not found",
  405: "That action is not available here",
  409: "That cannot run right now",
  413: "The request is too large",
  422: "The request is not valid",
};

const CODES: Record<number, string> = {
  0: "unreachable", 400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
  405: "method_not_allowed", 409: "conflict", 413: "too_large", 422: "invalid_request",
};

export const statusTitle = (status: number) =>
  TITLES[status] ?? (status >= 500 ? "The app hit an error" : "The request failed");

const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

/** The first problem of a FastAPI validation list ({detail: [{loc, msg}]}) as "field: message". */
function firstProblem(list: unknown[]): string | null {
  const item = list[0] as { loc?: unknown[]; msg?: unknown } | undefined;
  if (!item || typeof item !== "object") return null;
  const where = (item.loc ?? []).filter((p) => p !== "body" && p !== "query" && p !== "path").join(".");
  const msg = text(item.msg);
  return msg ? (where ? `${where}: ${msg}` : msg) : null;
}

/** One reason of a refusal: a stable code, the sentence to show, and the numbers behind it. */
export type ApiReason = { code: string; text: string; numbers?: Record<string, unknown> };

export class ApiError extends Error {
  readonly status: number;
  readonly title: string;
  readonly detail: string;
  readonly hint: string | null;
  readonly code: string;
  /** Status, URL and the raw body: what Developer mode shows under "Technical details". */
  readonly technical: string;
  /** The reasons a refusal came with (a campaign that cannot start says what is short, with numbers); empty for any other error. */
  readonly reasons: ApiReason[];

  constructor(init: { status: number; title: string; detail?: string; hint?: string | null; code?: string; technical?: string; reasons?: ApiReason[] }) {
    super(init.title);
    this.name = "ApiError";
    this.status = init.status;
    this.title = init.title;
    this.detail = init.detail ?? init.title;
    this.hint = init.hint ?? null;
    this.code = init.code ?? CODES[init.status] ?? (init.status >= 500 ? "server_error" : "error");
    this.technical = init.technical ?? `${init.status} ${init.title}`;
    this.reasons = init.reasons ?? [];
  }

  toString() { return this.title; }

  /** Build an error from a response status and its body text (JSON of any known shape, plain text or HTML). */
  static fromBody(status: number, body: string, url = ""): ApiError {
    const technical = `${status} ${url}\n${body.slice(0, 2000)}`.trim();
    let parsed: unknown = null;
    try { parsed = JSON.parse(body); } catch { /* ignored: not JSON; the text is used as it is below */ }
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      const b = parsed as Record<string, unknown>;
      const list = Array.isArray(b.detail) ? firstProblem(b.detail) : null;
      const title = text(b.error) ?? (typeof b.detail === "string" ? text(b.detail) : null) ?? (list ? statusTitle(status) : null) ?? text(b.message) ?? statusTitle(status);
      const detail = (typeof b.detail === "string" ? text(b.detail) : null) ?? list ?? text(b.reason) ?? title;
      const reasons = Array.isArray(b.reasons) ? b.reasons.filter((r): r is ApiReason => !!r && typeof r === "object" && typeof (r as ApiReason).text === "string") : [];
      return new ApiError({ status, title, detail, hint: text(b.hint), code: text(b.code) ?? undefined, technical, reasons });
    }
    const plain = body.trim();
    const readable = plain && plain.length <= 300 && !plain.startsWith("<") ? plain : null;
    return new ApiError({ status, title: statusTitle(status), detail: readable ?? undefined, technical });
  }

  /** The request never got an answer (the backend stopped, the network failed). */
  static unreachable(url: string, cause: unknown): ApiError {
    return new ApiError({
      status: 0, title: statusTitle(0), detail: "The request did not get an answer.",
      hint: "The backend may have stopped. Restart the app and try again.",
      technical: `0 ${url}\n${cause instanceof Error ? cause.message : String(cause)}`,
    });
  }

  /** The request was sent but no answer came within the time allowed. */
  static timeout(url: string, ms: number): ApiError {
    return new ApiError({
      status: 0, title: "The app took too long to answer", detail: "The request did not finish in time.",
      hint: "The backend may be busy with a long job. Wait a moment and try again.", code: "timeout",
      technical: `0 ${url}\nno answer within ${ms} ms`,
    });
  }
}

/** The text to show for any thrown value. */
export const errorText = (e: unknown): string =>
  e instanceof ApiError ? e.title : e instanceof Error ? e.message : String(e);
