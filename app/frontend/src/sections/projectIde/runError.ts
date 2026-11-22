// Older runs persisted nested OpenCode/API errors. Keep their actionable message readable.
export function friendlyRunError(value: unknown, depth = 0): string {
  if (depth > 7) return "The request failed. Check the selected model and try again.";
  if (value instanceof Error) {
    const api = value as Error & { detail?: string };
    return friendlyRunError(api.detail || api.message, depth + 1);
  }
  if (value && typeof value === "object") {
    const item = value as Record<string, unknown>;
    for (const key of ["error", "data", "message", "detail"]) if (item[key]) return friendlyRunError(item[key], depth + 1);
  }
  if (typeof value === "string") {
    try { return friendlyRunError(JSON.parse(value), depth + 1); } catch { /* ignored: Plain text or a prefixed native error is handled below. */ }
    const start = value.indexOf("{\"name\":");
    const spaced = value.indexOf('{"name": ');
    const offset = start >= 0 ? start : spaced;
    if (offset >= 0) { try { return friendlyRunError(JSON.parse(value.slice(offset)), depth + 1); } catch { /* ignored: Keep the original text if it is not valid JSON. */ } }
    return value;
  }
  return "The request failed. Check the selected model and try again.";
}
