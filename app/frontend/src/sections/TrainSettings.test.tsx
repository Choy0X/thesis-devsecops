import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { tipOf } from "../test/hint";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { trainConfig: vi.fn() } };
});
import { api, type TrainConfig } from "../lib/api";
import { DevModeProvider } from "../lib/devMode";
import { TrainSettings, limitsText, valueText, words } from "./TrainSettings";

const cfg: TrainConfig = {
  counts: { settings: 4, fixed: 2, changeable: 2 }, file_sha256: "a".repeat(64), unresolved: ["model.revision"],
  sections: [
    { key: "lora", title: "LoRA adapters", fields: [
      { key: "lora.r", name: "r", kind: "int", value: 16, fixed: false, minimum: 1 },
      { key: "lora.lora_dropout", name: "lora_dropout", kind: "float", value: 0.05, fixed: false, minimum: 0, below: 1 }] },
    { key: "training", title: "Training", fields: [
      { key: "training.packing", name: "packing", kind: "bool", value: false, fixed: true, fixed_reason: "A rule the experiments depend on, or a method that has no implementation yet." },
      { key: "training.optimizer", name: "optimizer", kind: "choice", value: "adamw_torch", fixed: true, choices: ["adamw_torch"], fixed_reason: "Only one choice is implemented." }] },
  ],
};
const show = (dev = false) => render(<DevModeProvider initial={dev}><TrainSettings /></DevModeProvider>);

beforeEach(() => { vi.mocked(api.trainConfig).mockReset(); vi.mocked(api.trainConfig).mockResolvedValue(cfg); });

describe("the words", () => {
  it("writes values and limits in plain language", () => {
    expect(valueText(true)).toBe("yes");
    expect(valueText(null)).toBe("not set");
    expect(valueText([])).toBe("none");
    expect(valueText([1, 2])).toBe("1, 2");
    expect(valueText({ decision_head: 0.5, patch: 1 })).toBe("decision head 0.5, patch 1");
    expect(words("lora_dropout")).toBe("lora dropout");
    expect(limitsText({ key: "k", name: "k", kind: "float", value: 0, fixed: false, minimum: 0, below: 1 })).toBe("at least 0, less than 1");
    expect(limitsText({ key: "k", name: "k", kind: "choice", value: "a", fixed: false, choices: ["a", "b"] })).toBe("one of a, b");
    expect(limitsText({ key: "k", name: "k", kind: "choice", value: "a", fixed: true, choices: ["a"] })).toBe("");
  });
});

describe("TrainSettings", () => {
  it("counts the settings and says which are fixed in this phase", async () => {
    show();
    expect(await screen.findByText("4 settings: 2 can be changed, 2 are fixed in this phase.")).toBeInTheDocument();
    expect(screen.getByText(/1 values are filled in when a run starts/)).toBeInTheDocument();
  });

  it("shows each setting with its value, its limits and Fixed or Can change", async () => {
    show();
    const lora = (await screen.findByText("LoRA adapters", { selector: "summary" })).closest("details") as HTMLElement;
    fireEvent.click(within(lora).getByText("LoRA adapters", { selector: "summary" }));
    const row = within(lora).getByText("lora dropout").closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("0.05");
    expect(row).toHaveTextContent("at least 0, less than 1");
    expect(row).toHaveTextContent("Can change");
    const training = screen.getByText("Training", { selector: "summary" }).closest("details") as HTMLElement;
    const packing = within(training).getByText("packing").closest("tr") as HTMLElement;
    expect(packing).toHaveTextContent("no");
    expect(await tipOf(within(packing).getByText("Fixed"))).toContain("experiments depend");
  });

  it("finds a setting by a word of its name and opens its section", async () => {
    show();
    fireEvent.change(await screen.findByLabelText("Find a setting"), { target: { value: "packing" } });
    expect(screen.getByText("packing")).toBeInTheDocument();
    expect(screen.queryByText("LoRA adapters", { selector: "summary" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Find a setting"), { target: { value: "zzz" } });
    expect(screen.getByText("No setting matches.")).toBeInTheDocument();
  });

  it("can hide the fixed settings", async () => {
    show();
    await screen.findByText("LoRA adapters", { selector: "summary" });
    fireEvent.click(screen.getByRole("checkbox", { name: "Only those that can be changed" }));
    expect(screen.queryByText("Training", { selector: "summary" })).toBeNull();
    expect(screen.getByText("LoRA adapters", { selector: "summary" })).toBeInTheDocument();
  });

  it("uses no retrieval setup and reports an invalid training configuration", async () => {
    const { unmount } = show();
    expect(await screen.findByRole("heading", { name: "Training settings" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /retrieval|knowledge base/i })).toBeNull();
    unmount();
    vi.mocked(api.trainConfig).mockRejectedValue(new Error("The configuration is invalid."));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read the training settings");
    expect(screen.queryByRole("heading", { name: /retrieval|knowledge base/i })).toBeNull();
  });

  it("keeps the dotted keys and the file hash for Developer mode", async () => {
    const { unmount } = show(false);
    await screen.findByText("LoRA adapters", { selector: "summary" });
    expect(screen.queryByText("lora.r")).toBeNull();
    unmount();
    show(true);
    expect(await screen.findByText("lora.r")).toBeInTheDocument();
    expect(screen.getByText(/configuration file sha256 a{64}/)).toBeInTheDocument();
  });
});
