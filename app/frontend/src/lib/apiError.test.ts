import { describe, expect, it } from "vitest";
import { ApiError, errorText, statusTitle } from "./apiError";

const make = (status: number, body: unknown) => ApiError.fromBody(status, typeof body === "string" ? body : JSON.stringify(body), "/api/x");

describe("ApiError.fromBody", () => {
  it("reads the unified body", () => {
    const e = make(409, { ok: false, error: "a campaign is already running", detail: "more words", hint: "Pause it first.", code: "conflict" });
    expect([e.status, e.title, e.detail, e.hint, e.code]).toEqual([409, "a campaign is already running", "more words", "Pause it first.", "conflict"]);
    expect(e.message).toBe("a campaign is already running");
    expect(String(e)).toBe("a campaign is already running");
  });

  it("reads each older shape", () => {
    expect(make(404, { error: "not found" }).title).toBe("not found");
    expect(make(400, { error: "bad tag", detail: "bad tag" }).detail).toBe("bad tag");
    expect(make(400, { ok: false, error: "x", detail: "y" }).detail).toBe("y");
    expect(make(400, { ok: false, detail: "unknown target: nope" }).title).toBe("unknown target: nope");
    expect(make(403, { error: "forbidden: reload" }).code).toBe("forbidden");
    expect(make(500, { error: "boom" }).code).toBe("server_error");
  });

  it("reads a FastAPI validation list", () => {
    const e = make(422, { detail: [{ loc: ["body", "stage"], msg: "Field required", type: "missing" }] });
    expect(e.title).toBe("The request is not valid");
    expect(e.detail).toBe("stage: Field required");
  });

  it("never turns raw JSON or HTML into the title", () => {
    for (const body of ['{"weird": true}', "<html><body>502 Bad Gateway</body></html>", "", "[1,2]"]) {
      const e = make(502, body);
      expect(e.title).toBe(statusTitle(502));
      expect(e.title).not.toContain("{");
      expect(e.title).not.toContain("<");
    }
  });

  it("keeps short plain text as the detail", () => {
    const e = make(400, "Bad Request");
    expect([e.title, e.detail]).toEqual(["The request was not accepted", "Bad Request"]);
  });

  it("keeps the raw body for Technical details", () => {
    const e = make(409, { error: "x" });
    expect(e.technical).toContain("409 /api/x");
    expect(e.technical).toContain('"error":"x"');
  });
});

describe("ApiError.unreachable and errorText", () => {
  it("describes a request that got no answer", () => {
    const e = ApiError.unreachable("/api/jobs", new TypeError("Failed to fetch"));
    expect([e.status, e.title, e.code]).toEqual([0, "Cannot reach the app", "unreachable"]);
    expect(e.hint).toMatch(/Restart/);
    expect(e.technical).toContain("Failed to fetch");
  });

  it("shows the title for api errors and the message for others", () => {
    expect(errorText(make(404, { error: "not found" }))).toBe("not found");
    expect(errorText(new Error("plain"))).toBe("plain");
    expect(errorText("text")).toBe("text");
  });
});

describe("the reasons of a refusal", () => {
  it("are kept with the error, so a screen can list them", () => {
    const body = JSON.stringify({ ok: false, error: "Not enough room to start this campaign", hint: "Wait.", code: "no_room",
      reasons: [{ code: "cpu", text: "This campaign needs 2 cores.", numbers: { needs: 2 } }, { nonsense: true }, "text"] });
    const e = ApiError.fromBody(409, body, "/api/campaigns");
    expect(e.title).toBe("Not enough room to start this campaign");
    expect(e.reasons).toEqual([{ code: "cpu", text: "This campaign needs 2 cores.", numbers: { needs: 2 } }]);
    expect(ApiError.fromBody(409, JSON.stringify({ error: "x" })).reasons).toEqual([]);
    expect(ApiError.unreachable("/x", new Error("down")).reasons).toEqual([]);
  });
});
