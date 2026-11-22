import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { SecretField } from ".";

function Harness({ saved = true, onRemove }: { saved?: boolean; onRemove?: () => void }) {
  const [v, setV] = useState("");
  return <SecretField label="API key" value={v} onChange={setV} saved={saved} savedHint="a1b2" onRemove={onRemove} hint="Stored on this computer only" />;
}

describe("SecretField", () => {
  it("shows and hides what was typed with the Eye, and says which state it is in", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByLabelText("API key");
    expect(input).toHaveAttribute("type", "password");
    await user.type(input, "sk-123");
    await user.click(screen.getByRole("button", { name: "Show API key" }));
    expect(input).toHaveAttribute("type", "text");
    expect(screen.getByRole("button", { name: "Hide API key" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Hide API key" }));
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveValue("sk-123");
  });

  it("never removes the saved value from a click on the Eye, the label, the hint or the saved tag", async () => {
    const onRemove = vi.fn();
    const user = userEvent.setup();
    render(<Harness onRemove={onRemove} />);
    await user.click(screen.getByRole("button", { name: "Show API key" }));
    await user.click(screen.getByText("API key"));
    await user.click(screen.getByText("Stored on this computer only"));
    await user.click(screen.getByTestId("secret-saved"));
    expect(onRemove).not.toHaveBeenCalled();
    expect(document.querySelector("label button")).toBeNull();           // nothing that can be pressed sits inside a label
  });

  it("has a Remove button of its own, only when a value is saved, that asks the caller once", async () => {
    const onRemove = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<Harness onRemove={onRemove} />);
    await user.click(screen.getByRole("button", { name: "Remove saved API key" }));
    expect(onRemove).toHaveBeenCalledTimes(1);
    rerender(<Harness saved={false} onRemove={onRemove} />);
    expect(screen.queryByRole("button", { name: "Remove saved API key" })).toBeNull();
  });

  it("tells that a value is saved and how it ends, without ever receiving it", () => {
    render(<Harness />);
    expect(screen.getByTestId("secret-saved")).toHaveTextContent("a1b2");
    expect(screen.getByLabelText("API key")).toHaveAttribute("placeholder", expect.stringContaining("saved"));
  });

  // A saved key is never sent to the page with the settings, so with nothing typed the field is empty and Show has nothing to show until the page asks for it.
  describe("a saved key", () => {
    function Revealing({ onReveal, saved = true }: { onReveal?: () => Promise<string>; saved?: boolean }) {
      const [v, setV] = useState("");
      return <SecretField label="API key" value={v} onChange={setV} saved={saved} savedHint="a1b2" onReveal={onReveal} />;
    }

    it("is fetched when Show is pressed, shown in the field, and dropped again when it is hidden", async () => {
      const onReveal = vi.fn().mockResolvedValue("sk-saved-a1b2");
      const user = userEvent.setup();
      render(<Revealing onReveal={onReveal} />);
      const input = screen.getByLabelText("API key");
      expect(input).toHaveValue("");
      await user.click(screen.getByRole("button", { name: "Show API key" }));
      await waitFor(() => expect(input).toHaveValue("sk-saved-a1b2"));
      expect(input).toHaveAttribute("type", "text");
      expect(onReveal).toHaveBeenCalledTimes(1);
      await user.click(screen.getByRole("button", { name: "Hide API key" }));
      expect(input).toHaveAttribute("type", "password");
      expect(input).toHaveValue("");                                                    // not kept in the page while hidden
      await user.click(screen.getByRole("button", { name: "Show API key" }));
      await waitFor(() => expect(input).toHaveValue("sk-saved-a1b2"));
      expect(onReveal).toHaveBeenCalledTimes(2);                                         // asked again, never remembered
    });

    it("is edited in place: typing into the shown key makes the whole edited text the new value", async () => {
      const onChange = vi.fn();
      const user = userEvent.setup();
      function Edit() {
        const [v, setV] = useState("");
        return <SecretField label="API key" value={v} onChange={(x) => { onChange(x); setV(x); }} saved savedHint="a1b2" onReveal={async () => "sk-saved"} />;
      }
      render(<Edit />);
      await user.click(screen.getByRole("button", { name: "Show API key" }));
      const input = screen.getByLabelText("API key");
      await waitFor(() => expect(input).toHaveValue("sk-saved"));
      await user.type(input, "X");
      expect(onChange).toHaveBeenLastCalledWith("sk-savedX");
      expect(input).toHaveValue("sk-savedX");
    });

    it("is not asked for when something was typed", async () => {
      const onReveal = vi.fn().mockResolvedValue("sk-saved");
      const user = userEvent.setup();
      render(<Revealing onReveal={onReveal} />);
      await user.type(screen.getByLabelText("API key"), "new-value");
      await user.click(screen.getByRole("button", { name: "Show API key" }));
      expect(screen.getByLabelText("API key")).toHaveValue("new-value");
      expect(screen.getByLabelText("API key")).toHaveAttribute("type", "text");
      expect(onReveal).not.toHaveBeenCalled();
    });

    it("is not asked for when nothing is saved, or when the page has no way to ask", async () => {
      const onReveal = vi.fn().mockResolvedValue("sk-saved");
      const user = userEvent.setup();
      const first = render(<Revealing onReveal={onReveal} saved={false} />);
      await user.click(screen.getByRole("button", { name: "Show API key" }));
      expect(screen.getByLabelText("API key")).toHaveAttribute("type", "text");
      expect(onReveal).not.toHaveBeenCalled();
      first.unmount();
      render(<Revealing saved />);
      await user.click(screen.getByRole("button", { name: "Show API key" }));
      expect(screen.getByLabelText("API key")).toHaveAttribute("type", "text");
      expect(screen.getByLabelText("API key")).toHaveValue("");
    });

    it("says in words that the saved value could not be read, and stays hidden", async () => {
      const user = userEvent.setup();
      render(<Revealing onReveal={() => Promise.reject(new Error("The app did not answer"))} />);
      await user.click(screen.getByRole("button", { name: "Show API key" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("The saved value could not be read: The app did not answer");
      expect(screen.getByLabelText("API key")).toHaveAttribute("type", "password");
      expect(screen.getByRole("button", { name: "Show API key" })).toBeEnabled();        // it can be tried again
    });

    it("shows a busy eye while it is being read, and a second press waits", async () => {
      let release: (v: string) => void = () => undefined;
      const onReveal = vi.fn(() => new Promise<string>((resolve) => { release = resolve; }));
      const user = userEvent.setup();
      render(<Revealing onReveal={onReveal} />);
      await user.click(screen.getByRole("button", { name: "Show API key" }));
      const eye = screen.getByRole("button", { name: "Show API key" });
      expect(eye).toBeDisabled();
      expect(eye).toHaveAttribute("aria-busy", "true");
      release("sk-saved");
      await waitFor(() => expect(screen.getByRole("button", { name: "Hide API key" })).toBeEnabled());
      expect(onReveal).toHaveBeenCalledTimes(1);
    });

    it("is dropped when the saved key changes or goes", async () => {
      const user = userEvent.setup();
      function Swap() {
        const [hint, setHint] = useState("a1b2");
        return (<><SecretField label="API key" value="" onChange={() => undefined} saved savedHint={hint} onReveal={async () => "sk-old"} />
          <button onClick={() => setHint("zz99")}>saved again</button></>);
      }
      render(<Swap />);
      await user.click(screen.getByRole("button", { name: "Show API key" }));
      await waitFor(() => expect(screen.getByLabelText("API key")).toHaveValue("sk-old"));
      await user.click(screen.getByText("saved again"));
      expect(screen.getByLabelText("API key")).toHaveValue("");
      expect(screen.getByLabelText("API key")).toHaveAttribute("type", "password");
    });
  });

  it("is off as a whole when disabled", () => {
    render(<SecretField label="Token" value="" onChange={() => undefined} saved onRemove={() => undefined} disabled />);
    for (const name of ["Show Token", "Remove saved Token"]) expect(screen.getByRole("button", { name })).toBeDisabled();
  });
});
