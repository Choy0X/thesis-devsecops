import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../lib/apiError";
import { ErrorState } from "./ui";

describe("ErrorState", () => {
  it("shows the title, the detail and the hint of an ApiError", () => {
    const e = new ApiError({ status: 409, title: "a campaign is already running", detail: "Stop it first, then start again.", hint: "Use Pause." });
    render(<ErrorState error={e} />);
    expect(screen.getByRole("alert")).toHaveTextContent("a campaign is already running");
    expect(screen.getByText("Stop it first, then start again.")).toBeInTheDocument();
    expect(screen.getByText("Use Pause.")).toBeInTheDocument();
  });

  it("shows plain errors without raw JSON and offers a retry only when asked", () => {
    const retry = vi.fn();
    const { rerender } = render(<ErrorState error={new Error("boom")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
    expect(screen.queryByRole("button")).toBeNull();
    rerender(<ErrorState error={new Error("boom")} onRetry={retry} />);
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("does not repeat the detail when it equals the title", () => {
    const e = new ApiError({ status: 404, title: "not found" });
    render(<ErrorState error={e} />);
    expect(screen.getAllByText("not found")).toHaveLength(1);
  });
});
