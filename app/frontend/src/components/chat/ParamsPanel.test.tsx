import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ChatEndpoint, ChatParams } from "../../lib/api";
import { ConfirmProvider } from "../hud/AlertDialog";
import { approxTokens, contextWords, ParamsPanel } from "./ParamsPanel";

const params = (over: Partial<ChatParams> = {}): ChatParams => ({ system: "", temperature: null, top_p: null, max_tokens: null, autocompact: true, web_search: false, ...over });
const local: ChatEndpoint = { configured: true, model: "qwen2.5-coder-7b.gguf", host: "127.0.0.1", local: true, provider: "managed" };
const show = (props: Partial<React.ComponentProps<typeof ParamsPanel>> = {}) => {
  const onChange = vi.fn();
  const onChangeInstructions = vi.fn().mockResolvedValue(undefined);
  render(<ConfirmProvider><ParamsPanel params={params()} endpoint={local} context={4096} used={1200} onChange={onChange} onChangeInstructions={onChangeInstructions} {...props} /></ConfirmProvider>);
  return onChange;
};
const field = (name: string) => screen.getByRole("spinbutton", { name }) as HTMLInputElement;

describe("the words", () => {
  it("estimates four characters to a token plus the instructions that go with every question", () => {
    expect(approxTokens(0)).toBe(700);
    expect(approxTokens(4000)).toBe(1700);
    expect(approxTokens(1)).toBe(701);
  });

  it("says how much of the context is used, or that its size is not known", () => {
    expect(contextWords(1200, 4096)).toBe("about 1,200 of 4,096 tokens of context");
    expect(contextWords(900, null)).toBe("about 900 tokens so far; the model's context size is not known");
  });
});

describe("ParamsPanel", () => {
  it("says the model, that it runs on this computer and how much of its context is used", () => {
    show();
    expect(screen.getByText("qwen2.5-coder-7b.gguf")).toBeInTheDocument();
    expect(screen.getByText("Runs on this computer.")).toBeInTheDocument();
    expect(screen.getByText("about 1,200 of 4,096 tokens of context")).not.toHaveClass("text-warn");
  });

  it("warns when the model is not on this computer, because the lines of the files it reads are sent there", () => {
    show({ endpoint: { ...local, local: false, host: "api.example.org" } });
    expect(screen.getByText(/Runs on api\.example\.org, not this computer: the lines of the files it reads are sent there/)).toHaveClass("text-warn");
  });

  it("warns in the meter when the context is nearly full", () => {
    show({ used: 3600 });
    expect(screen.getByText("about 3,600 of 4,096 tokens of context")).toHaveClass("text-warn");
  });

  it("says nothing about trimming or compaction for a conversation that was never affected by either", () => {
    show();
    expect(screen.queryByText(/sent to the model; earlier ones/)).toBeNull();
    expect(screen.queryByText(/summarized to save tokens/)).toBeNull();
  });

  it("warns, in a way that stands out, once the oldest turns were ever left out of what the model is sent", () => {
    show({ everTrimmed: true });
    expect(screen.getByText("Only the most recent messages are sent to the model; earlier ones are kept here but no longer reused.")).toHaveClass("text-warn");
  });

  it("says, calmly, once the oldest turns were ever folded into a summary instead", () => {
    show({ everCompacted: true });
    expect(screen.getByText("Earlier messages were summarized to save tokens; nothing was dropped.")).not.toHaveClass("text-warn");
  });

  it("says when there is no model, and while it is still looking", () => {
    const { unmount } = render(<ConfirmProvider><ParamsPanel params={params()} endpoint={null} context={null} used={700} onChange={vi.fn()} onChangeInstructions={vi.fn()} /></ConfirmProvider>);
    expect(screen.getByText("Looking for the model...")).toBeInTheDocument();
    unmount();
    show({ endpoint: { ...local, configured: false, model: null } });
    expect(screen.getByRole("status")).toHaveTextContent("No language model is set up");
  });

  it("shows a placeholder for the conversation's instructions when there are none yet", () => {
    show();
    expect(screen.getByText("Added after the system instructions, for this conversation only.")).toBeInTheDocument();
  });

  it("previews the values of the conversation, and the saved values where there are any", () => {
    show({ params: params({ system: "Answer briefly.", temperature: 0.4, max_tokens: 512 }) });
    expect(screen.getByText("Answer briefly.")).toBeInTheDocument();
    expect(field("Temperature")).toHaveValue(0.4);
    expect(field("Longest answer")).toHaveValue(512);
  });

  it("shows a real default for temperature, top p and the longest answer instead of leaving them blank", () => {
    show();
    expect(field("Temperature")).toHaveValue(0.2);
    expect(field("Top p")).toHaveValue(0.9);
    expect(field("Longest answer")).toHaveValue(1024);
  });

  it("shows automatic compaction as on or off and saves it at once", () => {
    const onChange = show({ params: params({ autocompact: false }) });
    const toggle = screen.getByRole("switch", { name: "Automatic compaction" });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    expect(onChange).toHaveBeenCalledWith({ autocompact: true });
  });

  it("disables search the web and says why when no provider is set up in Settings", () => {
    show();
    const toggle = screen.getByRole("switch", { name: "Search the web" });
    expect(toggle).toBeDisabled();
    expect(screen.getByText("No web search provider is set up in Settings.")).toBeInTheDocument();
  });

  it("shows search the web as on or off, names the provider, and saves it at once", () => {
    const onChange = show({ params: params({ web_search: true }), endpoint: { ...local, web_search_provider: "brave" } });
    const toggle = screen.getByRole("switch", { name: "Search the web" });
    expect(toggle).toBeEnabled();
    expect(toggle).toBeChecked();
    expect(screen.getByText(/Sends your question to Brave Search to search the web/)).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(onChange).toHaveBeenCalledWith({ web_search: false });
  });

  it("tells an agent session that the switch gives the agent tools, and what stays in its own hands", () => {
    show({ agent: true, endpoint: { ...local, web_search_provider: "brave" } });
    expect(screen.getByText(/Gives the agent two tools: search the web through Brave Search, and read a page/)).toBeInTheDocument();
    expect(screen.queryByText(/Sends your question to/)).toBeNull();
  });

  it("edits the conversation's instructions in a dialog, saving only when they changed", async () => {
    const onChange = show({ params: params({ system: "Answer briefly." }) });
    fireEvent.click(screen.getByRole("button", { name: "Edit instructions" }));
    const dialog = await screen.findByRole("dialog", { name: "Instructions" });
    const box = within(dialog).getByRole("textbox", { name: "Instructions" });
    expect(box).toHaveValue("Answer briefly.");
    const save = within(dialog).getByRole("button", { name: "Save changes" });
    expect(save).toBeDisabled();
    fireEvent.change(box, { target: { value: "Name the file first." } });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ system: "Name the file first." }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("discards the draft and saves nothing when the instructions dialog is cancelled", async () => {
    const onChange = show({ params: params({ system: "Answer briefly." }) });
    fireEvent.click(screen.getByRole("button", { name: "Edit instructions" }));
    const dialog = await screen.findByRole("dialog", { name: "Instructions" });
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Instructions" }), { target: { value: "Something else." } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText("Answer briefly.")).toBeInTheDocument();
  });

  it("saves a number as a number when its field is left, and an emptied field as null", () => {
    const onChange = show({ params: params({ temperature: 0.2 }) });
    fireEvent.change(field("Temperature"), { target: { value: "0.7" } });
    fireEvent.blur(field("Temperature"));
    expect(onChange).toHaveBeenLastCalledWith({ temperature: 0.7 });
    fireEvent.change(field("Top p"), { target: { value: "0.5" } });
    fireEvent.blur(field("Top p"));
    expect(onChange).toHaveBeenLastCalledWith({ top_p: 0.5 });
    fireEvent.change(field("Longest answer"), { target: { value: "256" } });
    fireEvent.blur(field("Longest answer"));
    expect(onChange).toHaveBeenLastCalledWith({ max_tokens: 256 });
    fireEvent.change(field("Temperature"), { target: { value: "" } });
    fireEvent.blur(field("Temperature"));
    expect(onChange).toHaveBeenLastCalledWith({ temperature: null });
    expect(onChange).toHaveBeenCalledTimes(4);
  });

  it("saves nothing when a number was not changed", () => {
    const onChange = show({ params: params({ temperature: 0.2 }) });
    fireEvent.blur(field("Temperature"));
    fireEvent.change(field("Temperature"), { target: { value: "0.5" } });
    fireEvent.change(field("Temperature"), { target: { value: "0.2" } });
    fireEvent.blur(field("Temperature"));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("shows the sentence the server gave when it refused a value", () => {
    show({ error: "The temperature must be between 0 and 2." });
    expect(screen.getByRole("alert")).toHaveTextContent("The temperature must be between 0 and 2.");
  });

  it("is off while an answer is written", () => {
    show({ disabled: true });
    expect(screen.getByRole("button", { name: "Edit instructions" })).toBeDisabled();
    expect(field("Temperature")).toBeDisabled();
    expect(field("Top p")).toBeDisabled();
    expect(field("Longest answer")).toBeDisabled();
  });

  it("says what each number does", () => {
    show();
    expect(screen.getByText(/0 is steady and repeats itself, 2 is varied/)).toBeInTheDocument();
    expect(screen.getByText(/Only the likeliest words that add up to this share/)).toBeInTheDocument();
    expect(screen.getByText("In tokens. Empty lets the model decide.")).toBeInTheDocument();
  });

  it("shows no system instructions preview when the endpoint does not carry them", () => {
    show();
    expect(screen.queryByText("System instructions")).toBeNull();
  });

  it("previews the project's system instructions when the endpoint carries them", () => {
    show({ endpoint: { ...local, system_prompt: "You are the security assistant of the project Shop.", system_prompt_default: "You are the security assistant of the project Shop.", system_prompt_custom: false } });
    expect(screen.getByText("System instructions")).toBeInTheDocument();
    expect(screen.getByText("You are the security assistant of the project Shop.")).toBeInTheDocument();
  });

  it("saves the system instructions once they are changed, with no extra confirmation", async () => {
    const onChangeInstructions = vi.fn().mockResolvedValue(undefined);
    show({ endpoint: { ...local, system_prompt: "Default text.", system_prompt_default: "Default text.", system_prompt_custom: false }, onChangeInstructions });
    fireEvent.click(screen.getByRole("button", { name: "Edit system instructions" }));
    const dialog = await screen.findByRole("dialog", { name: "System instructions" });
    const box = within(dialog).getByRole("textbox", { name: "System instructions" });
    const save = within(dialog).getByRole("button", { name: "Save changes" });
    expect(save).toBeDisabled();
    expect(within(dialog).queryByRole("button", { name: "Revert to default" })).toBeNull();
    fireEvent.change(box, { target: { value: "Custom text." } });
    expect(save).toBeEnabled();
    fireEvent.click(save);
    await waitFor(() => expect(onChangeInstructions).toHaveBeenCalledWith("Custom text."));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("offers Revert to default only for a custom override, with no extra confirmation", async () => {
    const onChangeInstructions = vi.fn().mockResolvedValue(undefined);
    show({ endpoint: { ...local, system_prompt: "Custom text.", system_prompt_default: "Default text.", system_prompt_custom: true }, onChangeInstructions });
    fireEvent.click(screen.getByRole("button", { name: "Edit system instructions" }));
    const dialog = await screen.findByRole("dialog", { name: "System instructions" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Revert to default" }));
    await waitFor(() => expect(onChangeInstructions).toHaveBeenCalledWith(""));
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});
