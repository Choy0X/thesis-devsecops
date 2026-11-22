import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MessageView, usageWords, type ViewMessage } from "./MessageView";

const question = (over: Partial<ViewMessage> = {}): ViewMessage => ({ id: "q1", role: "user", content: "Which files matter?", created_at: "2026-10-13T10:00:00.000Z", ...over });
const answer = (over: Partial<ViewMessage> = {}): ViewMessage => ({ id: "a1", role: "assistant", content: "Read **app.py** first.", created_at: "2026-10-13T10:00:01.000Z", sources: [{ tool: "read_file", summary: "Read app.py" }], ...over });
const view = (m: ViewMessage, props: Partial<React.ComponentProps<typeof MessageView>> = {}) => render(<ul><MessageView message={m} {...props} /></ul>);

beforeEach(() => {
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});

describe("usageWords", () => {
  it("says the tokens, the speed and the time, then the prompt, and leaves out what is not known", () => {
    expect(usageWords({ prompt_tokens: 1204, completion_tokens: 412, seconds: 10.78, tokens_per_second: 38.2 })).toBe("412 tokens, 38.2 tokens a second, 11 s (prompt 1,204 tokens)");
    expect(usageWords({ prompt_tokens: 5, completion_tokens: 1, seconds: 0.5, tokens_per_second: 2 })).toBe("1 token, 2.0 tokens a second, 0.5 s (prompt 5 tokens)");
    expect(usageWords({ prompt_tokens: null, completion_tokens: 30 })).toBe("30 tokens");
    expect(usageWords({ prompt_tokens: 9, completion_tokens: null })).toBe("(prompt 9 tokens)");
    expect(usageWords({ prompt_tokens: null, completion_tokens: null })).toBe("");
    expect(usageWords(null)).toBe("");
    expect(usageWords(undefined)).toBe("");
    expect(usageWords({ prompt_tokens: 1, completion_tokens: 3, seconds: 0, tokens_per_second: 0 })).toBe("3 tokens (prompt 1 token)");
  });
});

describe("a question", () => {
  it("is the person's words, said as theirs to a screen reader", () => {
    view(question());
    expect(screen.getByText("Which files matter?")).toBeInTheDocument();
    expect(screen.getByText("You:")).toHaveClass("sr-only");
  });

  it("can be copied, and edited only when the page gives a way to do it", async () => {
    view(question());
    fireEvent.click(screen.getByRole("button", { name: "Copy your question" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("Which files matter?"));
    expect(screen.queryByRole("button", { name: "Edit and send again" })).toBeNull();
  });

  it("is edited in place, says what sending it again does, and sends the trimmed text with its id", () => {
    const onEdit = vi.fn();
    view(question(), { onEdit });
    fireEvent.click(screen.getByRole("button", { name: "Edit and send again" }));
    const box = screen.getByRole("textbox", { name: "Your question, edited" });
    expect(box).toHaveValue("Which files matter?");
    expect(screen.getByText(/removes the answers that came after this question/)).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "  Which files matter most?  " } });
    fireEvent.click(screen.getByRole("button", { name: "Send again" }));
    expect(onEdit).toHaveBeenCalledWith("q1", "Which files matter most?");
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("is not sent again when it is empty, and Cancel and Escape keep the old words", () => {
    const onEdit = vi.fn();
    view(question(), { onEdit });
    fireEvent.click(screen.getByRole("button", { name: "Edit and send again" }));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "Send again" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onEdit).not.toHaveBeenCalled();
    expect(screen.getByText("Which files matter?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit and send again" }));
    expect(screen.getByRole("textbox")).toHaveValue("Which files matter?");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "something else" } });
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Edit and send again" }));
    expect(screen.getByRole("textbox")).toHaveValue("Which files matter?");
  });

  it("cannot be edited while an answer is being written", () => {
    view(question(), { onEdit: vi.fn(), busy: true });
    expect(screen.getByRole("button", { name: "Edit and send again" })).toBeDisabled();
  });
});

describe("a question with files attached by @", () => {
  it("lists the files the server gave the model under the question, and says when only the start of one was given", () => {
    view(question({ content: "What does @app.py do?", attachments: [{ path: "app.py", cut: false }, { path: "big/long.py", cut: true }] }));
    const list = screen.getByRole("list", { name: "Attached files" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["app.py", "big/long.py (start)"]);
    expect(within(list).getByText("big/long.py (start)")).toHaveAttribute("title", "Only the start of this file was given to the model");
  });

  it("shows no list when nothing was attached", () => {
    view(question());
    expect(screen.queryByRole("list", { name: "Attached files" })).toBeNull();
  });
});

describe("an agent answer: fork", () => {
  it("offers a fork from here only when it is given something to do with it, and passes the answer's id on", () => {
    const onFork = vi.fn();
    view(answer({ id: "a9", steps: [{ kind: "text", text: "Done." }] }), { onFork });
    fireEvent.click(screen.getByRole("button", { name: "Fork a new session from here" }));
    expect(onFork).toHaveBeenCalledWith("a9");
  });

  it("shows no fork button without one", () => {
    view(answer());
    expect(screen.queryByRole("button", { name: "Fork a new session from here" })).toBeNull();
  });

  it("is disabled while something is running", () => {
    view(answer({ id: "a9" }), { onFork: vi.fn(), busy: true });
    expect(screen.getByRole("button", { name: "Fork a new session from here" })).toBeDisabled();
  });
});

describe("an answer", () => {
  it("is markdown, with the files it read named, a copy button and the words that say it can be wrong", async () => {
    const { container } = view(answer({ tools: [{ ok: true, summary: "Read app.py" }, { ok: false, summary: "The file is too large" }] }));
    expect(container.querySelector("strong")).toHaveTextContent("app.py");
    const looked = screen.getByRole("list", { name: "What the assistant looked at" });
    expect(within(looked).getByText("Looked at: Read app.py")).toBeInTheDocument();
    expect(within(looked).getByText("Could not: The file is too large")).toHaveClass("text-warn");
    expect(screen.getByText(/can still misread what it reads/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy the answer" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("Read **app.py** first."));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("says when the assistant read nothing, but not while it writes, when it failed or when it was stopped", () => {
    const { rerender } = view(answer({ sources: [] }));
    expect(screen.getByText("The assistant did not read any file of the project for this answer.")).toBeInTheDocument();
    rerender(<ul><MessageView message={answer({ sources: [], streaming: true })} /></ul>);
    expect(screen.queryByText(/did not read any file/)).toBeNull();
    rerender(<ul><MessageView message={answer({ sources: [], error: "The model endpoint did not answer." })} /></ul>);
    expect(screen.queryByText(/did not read any file/)).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("The model endpoint did not answer.");
    rerender(<ul><MessageView message={answer({ sources: [], stopped: true })} /></ul>);
    expect(screen.queryByText(/did not read any file/)).toBeNull();
    expect(screen.getByText("Stopped. What was written so far is kept.")).toBeInTheDocument();
  });

  it("says Writing while nothing has arrived, with no actions yet", () => {
    view(answer({ content: "", streaming: true, sources: undefined }));
    expect(screen.getByText("Writing...")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Copy|Regenerate/ })).toBeNull();
  });

  it("shows the thinking apart, open while the model thinks and folded once the answer has begun", () => {
    const { rerender, container } = view(answer({ content: "", streaming: true, thinking: "Let me look at the imports.", sources: undefined }));
    const details = () => container.querySelector("details")!;
    expect(details()).toHaveAttribute("open");
    expect(screen.getByText("Thinking...")).toBeInTheDocument();
    expect(screen.queryByText("Writing...")).toBeNull();
    rerender(<ul><MessageView message={answer({ content: "The answer begins.", streaming: true, thinking: "Let me look at the imports.", sources: undefined })} /></ul>);
    expect(details()).not.toHaveAttribute("open");
    expect(screen.getByText("Thinking")).toBeInTheDocument();
    expect(screen.getByText("Let me look at the imports.")).toBeInTheDocument();
    expect(screen.getByText("The answer begins.")).toBeInTheDocument();
  });

  it("has no thinking part for an answer that has none", () => {
    const { container } = view(answer());
    expect(container.querySelector("details")).toBeNull();
  });

  it("shows what the model told in notices as status lines", () => {
    view(answer({ notices: ["The oldest 6 messages were left out to fit the context."] }));
    expect(screen.getByText("The oldest 6 messages were left out to fit the context.")).toHaveAttribute("role", "status");
  });

  it("shows what the run is busy with only while the answer is being written", () => {
    view(answer({ status: "Checking how this model takes tools (once, about half a minute)...", streaming: true, content: "" }));
    expect(screen.getByText(/Checking how this model takes tools/)).toHaveAttribute("role", "status");
  });

  it("does not show a status on an answer that is written", () => {
    view(answer({ status: "Checking how this model takes tools (once, about half a minute)..." }));
    expect(screen.queryByText(/Checking how this model takes tools/)).toBeNull();
  });

  it("says the model and the speed it was written at", () => {
    view(answer({ model: "qwen2.5-coder-7b", usage: { prompt_tokens: 1204, completion_tokens: 412, seconds: 10.78, tokens_per_second: 38.2 } }));
    expect(screen.getByText("qwen2.5-coder-7b")).toBeInTheDocument();
    expect(screen.getByText("412 tokens, 38.2 tokens a second, 11 s (prompt 1,204 tokens)")).toBeInTheDocument();
  });

  it("shows model_label when stored, truncated with its own tooltip, falling back to model for an older message", () => {
    view(answer({ model: "llm", model_label: "qwen2.5-coder-7b-instruct-q4_k_m.gguf" }));
    expect(screen.getByText("qwen2.5-coder-7b-instruct-q4_k_m.gguf")).toHaveAttribute("title", "qwen2.5-coder-7b-instruct-q4_k_m.gguf");
    expect(screen.queryByText("llm")).toBeNull();
  });

  it("offers Regenerate on the last answer only, and not while another answer is being written", () => {
    const onRegenerate = vi.fn();
    const { rerender } = view(answer(), { last: true, onRegenerate });
    fireEvent.click(screen.getByRole("button", { name: "Regenerate the answer" }));
    expect(onRegenerate).toHaveBeenCalledTimes(1);
    rerender(<ul><MessageView message={answer()} last={false} onRegenerate={onRegenerate} /></ul>);
    expect(screen.queryByRole("button", { name: "Regenerate the answer" })).toBeNull();
    rerender(<ul><MessageView message={answer()} last onRegenerate={onRegenerate} busy /></ul>);
    expect(screen.getByRole("button", { name: "Regenerate the answer" })).toBeDisabled();
    rerender(<ul><MessageView message={answer()} last /></ul>);
    expect(screen.queryByRole("button", { name: "Regenerate the answer" })).toBeNull();
  });

  it("keeps the partial text of a stopped answer and still lets it be copied and asked again", () => {
    view(answer({ content: "Half an ans", stopped: true, sources: undefined }), { last: true, onRegenerate: vi.fn() });
    expect(screen.getByText(/Half an ans/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy the answer" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regenerate the answer" })).toBeEnabled();
  });

  it("has no copy button for an answer with no words", () => {
    view(answer({ content: "", error: "It failed.", sources: undefined }));
    expect(screen.queryByRole("button", { name: "Copy the answer" })).toBeNull();
  });

  it("a slash command's local reply shows its own header and leaves out the footer and the sources line", () => {
    view(answer({ command: { name: "status", ok: true }, sources: [], model: "qwen2.5-coder-7b" }));
    expect(screen.getByText("/status")).toHaveClass("font-mono");
    expect(screen.queryByText(/did not read any file/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Copy the answer" })).toBeNull();
    expect(screen.queryByText("qwen2.5-coder-7b")).toBeNull();
  });

  it("an ordinary answer (no command field) keeps its footer and sources line as before", () => {
    view(answer({ sources: [] }));
    expect(screen.queryByText("/status")).toBeNull();
    expect(screen.getByText(/did not read any file/)).toBeInTheDocument();
  });
});
