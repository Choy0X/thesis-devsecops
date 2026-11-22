import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { X } from "lucide-react";
import { describe, expect, it, vi } from "vitest";
import { Button, IconButton } from ".";

describe("Button", () => {
  it("is a real button that does not submit a form unless it says so", async () => {
    const onSubmit = vi.fn((e: { preventDefault: () => void }) => e.preventDefault());
    render(<form onSubmit={onSubmit}><Button>Plain</Button><Button type="submit">Send</Button></form>);
    expect(screen.getByRole("button", { name: "Plain" })).toHaveAttribute("type", "button");
    await userEvent.click(screen.getByRole("button", { name: "Plain" }));
    expect(onSubmit).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("is busy and cannot be clicked again while it loads", async () => {
    const onClick = vi.fn();
    render(<Button loading onClick={onClick}>Save</Button>);
    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("forwards its ref and gives a filled variant a foreground focus ring", () => {
    const ref = createRef<HTMLButtonElement>();
    render(<Button ref={ref} variant="primary">Go</Button>);
    expect(ref.current).toBe(screen.getByRole("button", { name: "Go" }));
    expect(ref.current!.className).toContain("focus-visible:outline-fg");      // a brand-red ring on a brand-red fill could not be seen
    expect(ref.current!.className).toContain("chamfer-sm");
  });

  it("has a danger variant that turns solid critical red under the pointer", () => {
    render(<Button variant="danger">Delete</Button>);
    expect(screen.getByRole("button", { name: "Delete" }).className).toContain("hover:bg-crit");
  });
});

describe("IconButton", () => {
  it("is named by its label and runs its handler", async () => {
    const onClick = vi.fn();
    render(<IconButton label="Close" onClick={onClick}><X aria-hidden size={14} /></IconButton>);
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("shows the same words as a tooltip when it gets keyboard focus", async () => {
    render(<IconButton label="Close"><X aria-hidden size={14} /></IconButton>);
    await userEvent.tab();
    expect((await screen.findAllByText("Close")).length).toBeGreaterThan(0);
  });

  it("can drop the tooltip", () => {
    render(<IconButton label="Quiet" tip={false}><X aria-hidden size={14} /></IconButton>);
    expect(screen.getByRole("button", { name: "Quiet" })).toBeInTheDocument();
  });

  it("is red on hover, on keyboard focus and while pressed in the danger tone", () => {
    render(<IconButton label="Remove" tone="danger"><X aria-hidden size={14} /></IconButton>);
    const cls = screen.getByRole("button", { name: "Remove" }).className;
    for (const needle of ["hover:bg-crit", "focus-visible:bg-crit", "active:bg-crit", "hover:text-on-brand"]) expect(cls).toContain(needle);
  });

  it("does not run its handler when disabled", async () => {
    const onClick = vi.fn();
    render(<IconButton label="Off" disabled onClick={onClick}><X aria-hidden size={14} /></IconButton>);
    await userEvent.click(screen.getByRole("button", { name: "Off" }));
    expect(onClick).not.toHaveBeenCalled();
  });
});
