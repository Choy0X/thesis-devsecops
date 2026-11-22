import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RoleChoice } from "./RoleChoice";

// What a model will be set up as (a language model or an embedding model). The app works it out from the model, so the person is told and not asked; the two choices appear only when
// they press Change, or when they had already chosen one.

const show = (over: Partial<React.ComponentProps<typeof RoleChoice>> = {}) => {
  const onChange = vi.fn();
  render(<RoleChoice role="chat" detected onChange={onChange} {...over} />);
  return onChange;
};

describe("RoleChoice", () => {
  it("says what the model will be set up as and where that came from, without asking", () => {
    show();
    expect(screen.getByText("Will be set up as a language model.")).toBeInTheDocument();
    expect(screen.getByText("Found from the name and the files of the model.")).toBeInTheDocument();
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });

  it("says embedding model for an embedder", () => {
    show({ role: "embedding" });
    expect(screen.getByText("Will be set up as an embedding model.")).toBeInTheDocument();
  });

  it("offers the two choices only after Change, with the current one on, and reports a choice", () => {
    const onChange = show();
    fireEvent.click(screen.getByRole("button", { name: "Change what it is used as" }));
    expect(screen.getByRole("radiogroup", { name: "Use it as" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Language model" })).toBeChecked();
    expect(screen.queryByRole("button", { name: "Change what it is used as" })).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "Embedding model" }));
    expect(onChange).toHaveBeenCalledWith("embedding");
  });

  it("shows the choices at once when the person had already chosen, and no longer says it was found", () => {
    show({ role: "embedding", detected: false });
    expect(screen.getByRole("radio", { name: "Embedding model" })).toBeChecked();
    expect(screen.queryByText(/Found from the name/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Change what it is used as" })).toBeNull();
  });

  it("does not claim to have found the role when the plan does not say so: the choices are shown", () => {
    show({ detected: undefined });
    expect(screen.queryByText(/Found from the name/)).toBeNull();
    expect(screen.getByRole("radio", { name: "Language model" })).toBeChecked();
  });

  it("keeps the sentence and offers no change while the install runs", () => {
    show({ disabled: true });
    expect(screen.getByText("Will be set up as a language model.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change what it is used as" })).toBeNull();
    expect(screen.queryByRole("radio")).toBeNull();
  });

  it("keeps the choices, but not usable, while the install runs after a choice", () => {
    show({ role: "embedding", detected: false, disabled: true });
    expect(screen.getByRole("radio", { name: "Language model" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "Embedding model" })).toBeDisabled();
  });

  it("shows nothing for a model that has no role (one that cannot be made ready)", () => {
    const { container } = render(<RoleChoice role={null} detected onChange={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
