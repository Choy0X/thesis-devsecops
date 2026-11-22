import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Button } from "./ui";

describe("Button", () => {
  it("tells assistive technology that it is working while it loads, and not otherwise", () => {
    const { rerender } = render(<Button loading>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    rerender(<Button>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).not.toHaveAttribute("aria-busy");
  });
});
