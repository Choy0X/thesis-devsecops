import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { choose } from "../test/select";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { codeSummary: vi.fn(), codePairs: vi.fn(), codePair: vi.fn(), listJobs: vi.fn(), miningCheckpoint: vi.fn(), dataStamp: vi.fn(), enrichCandidates: vi.fn(), datasetOptions: vi.fn(), codeDatasetOptions: vi.fn() } };
});
import { api, type CodePair } from "../lib/api";
import { ToastProvider } from "../components/Toast";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { DevModeProvider } from "../lib/devMode";
import { CodePairsPage } from "./CodePairsPage";

const pair = (id: number, over: Partial<CodePair> = {}): CodePair => ({
  id, pair_key: `k${id}`, commit_key: "c", cve_id: `CVE-2024-${1000 + id}`, ghsa_id: null, ecosystem: "npm", package: `pkg-${id}`, cwe: "CWE-79", cvss_vector: null,
  published: null, commit_date: null, repo_url: "https://github.com/o/r", fix_commit: "abcdef1234567890", parent_commit: "0123456789abcdef", file_path: `src/file${id}.js`,
  language: "javascript", function_name: `fn${id}`, granularity: "function", is_test: 0, tangled: 0, code_available: 1, skip_reason: null, source: "mined", llm_model: null, ...over,
} as CodePair);

// Sample text for the viewer to show; it is only displayed, never run.
const DIFF = "--- a/x.js\n+++ b/x.js\n@@ -1,2 +1,2 @@\n const a = 1;\n-eval(input);\n+safe(input);\n";

const summary = {
  fix_commits: { total: 10, by_status: { mined: 6, pending: 3, failed: 1, unlinked: 2 }, cves_with_fix: 5 },
  pairs: { usable: 4, functions: 3, by_granularity: { hunk: 1 }, cves: 2, commits: 3, by_language: { javascript: 4 }, by_skip_reason: {}, tangled: 0, tests: 0, enriched: 0 },
  dataset: null, languages: ["javascript", "python"],
} as any;

const show = (dev = false, path = "/") => render(
  <MemoryRouter initialEntries={[path]}><DevModeProvider initial={dev}><ToastProvider><ConfirmProvider><CodePairsPage /></ConfirmProvider></ToastProvider></DevModeProvider></MemoryRouter>);

beforeEach(() => {
  for (const fn of Object.values(api)) (fn as any).mockReset?.();
  vi.mocked(api.codeSummary).mockResolvedValue(summary);
  vi.mocked(api.codePairs).mockResolvedValue({ total: 2, records: [pair(1), pair(2, { code_available: 0, skip_reason: "binary" })] });
  vi.mocked(api.codePair).mockImplementation(async (id: number) => pair(id, { diff: DIFF, code_before: "const a = 1;\neval(input);\n", code_after: "const a = 1;\nsafe(input);\n", explanation: "The advisory says input is evaluated." }));
  vi.mocked(api.listJobs).mockResolvedValue([]);
  vi.mocked(api.miningCheckpoint).mockResolvedValue({ interrupted: false, active: false, active_here: false, pending: 3, failed: 1 } as any);
});

describe("CodePairsPage", () => {
  it("lists the pairs with the file, function, level and a status word", async () => {
    show();
    const table = await screen.findByRole("table", { name: "Code pairs" });
    expect(within(table).getByText("CVE-2024-1001")).toBeInTheDocument();
    expect(within(table).getByText("src/file1.js :: fn1")).toBeInTheDocument();
    expect(within(table).getByText("usable")).toBeInTheDocument();
    expect(within(table).getByText("binary")).toBeInTheDocument();
    expect(screen.getByText("1 to 2 of 2 pairs")).toBeInTheDocument();
  });

  it("asks the server for a filtered page", async () => {
    show();
    await screen.findByRole("table", { name: "Code pairs" });
    const user = userEvent.setup();
    await choose(user, screen.getByRole("combobox", { name: "Language" }), "python");
    await waitFor(() => expect(api.codePairs).toHaveBeenLastCalledWith(expect.objectContaining({ language: "python" }), 100, 0));
    await choose(user, screen.getByRole("combobox", { name: "Usability" }), "usable for training");
    await waitFor(() => expect(api.codePairs).toHaveBeenLastCalledWith(expect.objectContaining({ language: "python", available: "usable" }), 100, 0));
  });

  it("says what to do first when there are no pairs at all", async () => {
    vi.mocked(api.codePairs).mockResolvedValue({ total: 0, records: [] });
    show();
    expect(await screen.findByText("No code pairs yet")).toBeInTheDocument();
  });

  it("opens a pair as a change with line counts, then as the vulnerable and the fixed code", async () => {
    show();
    fireEvent.click(await screen.findByText("src/file1.js :: fn1"));
    const dialog = await screen.findByRole("dialog", { name: "CVE-2024-1001" });
    expect(await within(dialog).findByText("1 removed")).toBeInTheDocument();
    fireEvent.mouseDown(within(dialog).getByRole("tab", { name: "Vulnerable" }), { button: 0 });
    fireEvent.click(within(dialog).getByRole("tab", { name: "Vulnerable" }));
    expect(await within(dialog).findByRole("group", { name: /Vulnerable code, 2 lines/ })).toBeInTheDocument();
    expect(within(dialog).getByText(/eval\(input\)/)).toBeInTheDocument();
  });

  it("shows the commit and the internal ids only in Developer mode", async () => {
    show(false);
    fireEvent.click(await screen.findByText("src/file1.js :: fn1"));
    const plain = await screen.findByRole("dialog", { name: "CVE-2024-1001" });
    await within(plain).findByText("1 removed");
    expect(within(plain).queryByText(/abcdef1234567890/)).toBeNull();
  });

  it("shows them in Developer mode", async () => {
    show(true);
    fireEvent.click(await screen.findByText("src/file1.js :: fn1"));
    const dialog = await screen.findByRole("dialog", { name: "CVE-2024-1001" });
    expect(await within(dialog).findByText(/abcdef1234567890/)).toBeInTheDocument();
  });

  it("links to the fix commit only for a repository on GitHub or GitLab, never to an address that merely mentions one", async () => {
    vi.mocked(api.codePair).mockImplementation(async (id: number) => pair(id, { diff: DIFF, repo_url: "javascript:alert(1)//github.com" }));
    show();
    fireEvent.click(await screen.findByText("src/file1.js :: fn1"));
    const dialog = await screen.findByRole("dialog", { name: "CVE-2024-1001" });
    await within(dialog).findByText("1 removed");
    expect(within(dialog).queryByRole("link", { name: "Open the fix commit" })).toBeNull();
  });

  it("links to the commit page when the repository is on GitHub", async () => {
    show();
    fireEvent.click(await screen.findByText("src/file1.js :: fn1"));
    const dialog = await screen.findByRole("dialog", { name: "CVE-2024-1001" });
    expect(await within(dialog).findByRole("link", { name: "Open the fix commit" })).toHaveAttribute("href", "https://github.com/o/r/commit/abcdef1234567890");
  });

  it("keeps collection jobs out of the collected-data review page", async () => {
    show(false, "/?tab=mining");
    await screen.findByRole('table', {name: 'Code pairs'});
    expect(screen.queryByRole('tab', {name: 'Mining steps'})).toBeNull();
    expect(screen.queryByRole('tab', {name: 'Model-written context'})).toBeNull();
  });

  it('replaces the approval queue with optional context improvements', async () => {
    show();
    await screen.findByRole('table', {name: 'Code pairs'});
    expect(screen.getByRole('tab', {name: 'Context improvements'})).toBeInTheDocument();
    expect(screen.queryByRole('tab', {name: 'Review and JSON exchange'})).not.toBeInTheDocument();
    expect(screen.queryByText(/until independently reviewed/)).not.toBeInTheDocument();
  });

  it("puts the reset behind a dialog on the last tab", async () => {
    show(false, "/?tab=maintenance");
    fireEvent.click(await screen.findByRole("button", { name: "Reset mined data" }));
    const dialog = await screen.findByRole("dialog", { name: "Reset mined data" });
    expect(within(dialog).getByRole("button", { name: /Clear mined code \(re-mine from scratch\)/ })).toBeInTheDocument();
    expect(within(dialog).getByText(/Source records are never deleted/)).toBeInTheDocument();
  });
});
