import { expect, it } from "vitest";
import { friendlyRunError } from "./runError";

it("unwraps the historical nested native memory error", () => {
  const message = "The model needs about 4.9 GB. Free memory or choose a shorter context.";
  const native = JSON.stringify({ name: "APIError", data: { message, statusCode: 507 } });
  const result = JSON.stringify({ automatic_retry: false, error: `The selected model/runtime returned an error: ${native}` });
  expect(friendlyRunError(result)).toBe(message);
});
it("preserves ordinary actionable errors", () => { expect(friendlyRunError(new Error("Load the selected model first."))).toBe("Load the selected model first."); });
