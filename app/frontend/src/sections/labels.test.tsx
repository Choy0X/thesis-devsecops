import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { tipOf } from "../test/hint";
import { FixCheckChip } from "./RecordPairs";

describe("the fix-check chip says what the check does", () => {
  it("never says 'verified'", () => {
    for (const check of ["verified", "suspect", "unknown"]) {
      const { unmount } = render(<FixCheckChip check={check} />);
      expect(screen.getByText(/release-date|not checked/)).toBeInTheDocument();
      expect(screen.queryByText(/verified/i)).toBeNull();
      unmount();
    }
  });

  it("explains itself in the tooltip", async () => {
    render(<FixCheckChip check="verified" />);
    expect(await tipOf(screen.getByText("release-date plausible"))).toContain("does not prove");
  });

  it("renders nothing without a check", () => {
    const { container } = render(<FixCheckChip check={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
