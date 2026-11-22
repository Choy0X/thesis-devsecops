import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ToastProvider, useToast } from "../Toast";
import { KeyValue, Stat } from ".";

describe("Stat and KeyValue", () => {
  it("shows a number under its name, with a unit and a hint", () => {
    render(<Stat label="Jobs" value={12} unit="running" hint="of 16" />);
    expect(screen.getByText("Jobs")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("of 16")).toBeInTheDocument();
  });

  it("marks a critical figure with text colour and not with a fill", () => {
    render(<Stat label="Failed" value={3} tone="crit" />);
    const cls = screen.getByText("3").className;
    expect(cls).toContain("text-crit-soft");
    expect(cls).not.toContain("bg-crit");
  });

  it("is a description list, so a screen reader reads each pair", () => {
    const { container } = render(<KeyValue items={[{ label: "Model", value: "qwen" }, { label: "Device", value: "GPU" }]} />);
    expect(container.querySelectorAll("dt")).toHaveLength(2);
    expect(container.querySelectorAll("dd")).toHaveLength(2);
    expect(screen.getByText("Device").tagName).toBe("DT");
  });
});

function Pusher() {
  const toast = useToast();
  return <><button onClick={() => toast.success("Saved.")}>ok</button><button onClick={() => toast.error("Could not save.")}>bad</button></>;
}

describe("Toast", () => {
  it("says what kind it is in a word and an icon, not by colour alone, and can be dismissed", async () => {
    const user = userEvent.setup();
    render(<ToastProvider><Pusher /></ToastProvider>);
    await user.click(screen.getByText("bad"));
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Problem");
    expect(status).toHaveTextContent("Could not save.");
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).toBeNull();
    await user.click(screen.getByText("ok"));
    expect(await screen.findByRole("status")).toHaveTextContent("Done");
  });
});
