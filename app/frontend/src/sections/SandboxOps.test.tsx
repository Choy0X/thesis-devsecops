import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, sandbox: Object.fromEntries(["policy", "setLimits", "recreate", "files", "downloadUrl", "upload", "changes", "imageList", "pin", "unpin", "build", "prunePlan", "prune",
    "historyKnown", "historyOf", "forget", "saveSession", "session", "deleteSession"].map((k) => [k, vi.fn()])) };
});
vi.mock("../components/RunConsole", () => ({
  RunConsole: ({ label, launch, params, disabled }: any) => <button type="button" disabled={disabled} onClick={() => launch?.(params)}>{label}</button>,
}));
import { ApiError, sandbox, type SandboxHistory, type SandboxImages, type SandboxListing, type SandboxPolicy } from "../lib/api";
import { ConfirmProvider } from "../components/hud/AlertDialog";
import { ToastProvider } from "../components/Toast";
import { SandboxLimits } from "./SandboxPolicy";
import { SandboxFiles, crumbs, sizeWords } from "./SandboxFiles";
import { SandboxImagesTab, TEMPLATE } from "./SandboxImages";
import { SandboxHistoryTab, sampleSeries } from "./SandboxHistory";

const wrap = (ui: React.ReactNode) => render(<ToastProvider><ConfirmProvider>{ui}</ConfirmProvider></ToastProvider>);
const refused = (title: string) => new ApiError({ status: 400, title });

const policy = (over: Partial<SandboxPolicy> = {}): SandboxPolicy => ({ cpus: 1, memory_mb: 512, swap_mb: 512, pids: 256, network: "none", read_only: false, editable: true, reason: null,
  bounds: { max_cpus: 8, max_memory_mb: 8192, min_cpus: 0.25, min_memory_mb: 64, min_pids: 16, max_pids: 4096 },
  warnings: ["The root filesystem is writable: a program can change the container's system files (not your computer's)."], ...over });

beforeEach(() => {
  for (const fn of Object.values(sandbox)) (fn as any).mockReset?.();
  vi.mocked(sandbox.downloadUrl).mockImplementation((cid, path) => `/dl/${cid}?path=${path}`);
});

describe("limits and network", () => {
  it("says what the container has, warns in words, and changes only the figures that were edited, in place", async () => {
    vi.mocked(sandbox.policy).mockResolvedValue(policy());
    vi.mocked(sandbox.setLimits).mockResolvedValue({ ...policy(), memory_mb: 1024, changed: { memory_mb: 1024 } } as never);
    wrap(<SandboxLimits cid="c1" onRecreated={vi.fn()} />);
    expect(await screen.findByText(/Now: 1 CPUs, 512 MB of memory, 256 processes, no network, files can be written\./)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: /isolation/ })).toHaveTextContent("The root filesystem is writable");
    const apply = screen.getByRole("button", { name: "Apply the limits" });
    expect(apply).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Memory \(MB\)/), { target: { value: "1024" } });
    expect(apply).toBeEnabled();
    fireEvent.click(apply);
    await waitFor(() => expect(sandbox.setLimits).toHaveBeenCalledWith("c1", { memory_mb: 1024 }));
  });

  it("shows the sentence of a refused figure", async () => {
    vi.mocked(sandbox.policy).mockResolvedValue(policy());
    vi.mocked(sandbox.setLimits).mockRejectedValue(refused("CPUs must be between 0.25 and 8."));
    wrap(<SandboxLimits cid="c1" onRecreated={vi.fn()} />);
    fireEvent.change(await screen.findByLabelText(/^CPUs/), { target: { value: "99" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply the limits" }));
    expect(await screen.findByText("CPUs must be between 0.25 and 8.")).toBeInTheDocument();
  });

  it("asks before creating the container again, says what is lost, and does nothing when the question is declined", async () => {
    vi.mocked(sandbox.policy).mockResolvedValue(policy());
    vi.mocked(sandbox.recreate).mockResolvedValue({ ...policy({ network: "bridge" }), id: "c2", name: "aivdf-demo", warnings: ["It can now reach the network."] });
    const onRecreated = vi.fn();
    wrap(<SandboxLimits cid="c1" onRecreated={onRecreated} />);
    const create = await screen.findByRole("button", { name: "Create it again with these settings" });
    expect(create).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Network on"));
    fireEvent.click(create);
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Programs that are running and the open terminal are lost");
    expect(dialog).toHaveTextContent("can reach the internet and other computers on your network");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(sandbox.recreate).not.toHaveBeenCalled();

    fireEvent.click(create);
    fireEvent.click(await within(await screen.findByRole("alertdialog")).findByRole("button", { name: "Create it again" }));
    await waitFor(() => expect(sandbox.recreate).toHaveBeenCalledWith("c1", { network: true }));
    await waitFor(() => expect(onRecreated).toHaveBeenCalledWith("c2", ["It can now reach the network."]));
  });

  it("leaves a verification container alone and says why", async () => {
    vi.mocked(sandbox.policy).mockResolvedValue(policy({ editable: false, reason: "This container belongs to a verification run." }));
    wrap(<SandboxLimits cid="c1" onRecreated={vi.fn()} />);
    expect(await screen.findByText("This container belongs to a verification run.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^CPUs/)).toBeDisabled();
    expect(screen.getByRole("button", { name: "Apply the limits" })).toBeDisabled();
  });
});

describe("files", () => {
  const listing = (over: Partial<SandboxListing> = {}): SandboxListing => ({ path: "/work", parent: "/", truncated: false, entries: [
    { name: "src", kind: "folder", size: null, mode: "drwxr-xr-x", modified: 1760000100, link: null },
    { name: "a.txt", kind: "file", size: 2048, mode: "-rw-r--r--", modified: 1760000200, link: null },
    { name: "link", kind: "link", size: null, mode: "lrwxrwxrwx", modified: 1760000400, link: "a.txt" }], ...over });

  it("lists a folder, offers a download only for files, and opens a folder on a click", async () => {
    vi.mocked(sandbox.files).mockImplementation(async (_c, p) => (p === "/work/src" ? listing({ path: "/work/src", parent: "/work", entries: [] }) : listing()));
    wrap(<SandboxFiles cid="c1" />);
    expect(await screen.findByRole("button", { name: "src/" })).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getByText(/to a\.txt/)).toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "Download" });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "/dl/c1?path=/work/a.txt");
    fireEvent.click(screen.getByRole("button", { name: "src/" }));
    expect(await screen.findByText("This folder is empty")).toBeInTheDocument();
    expect(sandbox.files).toHaveBeenLastCalledWith("c1", "/work/src");
  });

  it("puts a file in, and replaces one that is there only after the person says so", async () => {
    vi.mocked(sandbox.files).mockResolvedValue(listing());
    vi.mocked(sandbox.upload).mockRejectedValueOnce(refused("new.txt is already in /work. Choose another name, or say that it may be replaced.")).mockResolvedValue({ folder: "/work", name: "new.txt", bytes: 5 });
    wrap(<SandboxFiles cid="c1" />);
    const input = await screen.findByLabelText("File to put in");
    const file = new File(["hello"], "new.txt");
    fireEvent.change(input, { target: { files: [file] } });
    const alert = await screen.findByText("new.txt is already in /work.");
    expect(sandbox.upload).toHaveBeenCalledWith("c1", "/work", file, false);
    fireEvent.click(within(alert.parentElement!).getByRole("button", { name: "Replace it" }));
    await waitFor(() => expect(sandbox.upload).toHaveBeenLastCalledWith("c1", "/work", file, true));
  });

  it("shows what changed against the image only when asked", async () => {
    vi.mocked(sandbox.files).mockResolvedValue(listing());
    vi.mocked(sandbox.changes).mockResolvedValue({ changes: [{ path: "/etc/hosts", kind: "changed" }, { path: "/work/a.txt", kind: "added" }], truncated: false, counts: { added: 1, changed: 1, deleted: 0 }, total: 2 });
    wrap(<SandboxFiles cid="c1" />);
    await screen.findByRole("button", { name: "src/" });
    expect(sandbox.changes).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "What changed" }));
    expect(await screen.findByText(/1 added, 1 changed, 0 deleted/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Changed files" })).toHaveTextContent("/etc/hosts");
  });

  it("splits a path into steps and writes sizes in words", () => {
    expect(crumbs("/work/src")).toEqual([{ label: "/", path: "/" }, { label: "work", path: "/work" }, { label: "src", path: "/work/src" }]);
    expect(crumbs("/")).toEqual([{ label: "/", path: "/" }]);
    expect([sizeWords(null), sizeWords(12), sizeWords(2048), sizeWords(3 * 1048576)]).toEqual(["", "12 B", "2.0 KB", "3.0 MB"]);
  });
});

describe("images", () => {
  const images = (over: Partial<SandboxImages> = {}): SandboxImages => ({ docker_available: true, pinned: null, images: [
    { id: "sha256:aaaaaaaaaaaa", full_id: "sha256:" + "a".repeat(64), tags: ["python:3.11-slim"], digest: "sha256:" + "f".repeat(64), size_mb: 120.5, created: "2026-10-01T00:00:00", containers: 1, ours: false, dangling: false, pinned: false },
    { id: "sha256:cccccccccccc", full_id: "sha256:" + "c".repeat(64), tags: ["aivdf/old:1"], digest: "sha256:" + "c".repeat(64), size_mb: 10, created: "2026-09-01T00:00:00", containers: 0, ours: true, dangling: false, pinned: false }], ...over });

  it("lists images with digest, use and origin, and pins one", async () => {
    vi.mocked(sandbox.imageList).mockResolvedValue(images());
    vi.mocked(sandbox.pin).mockResolvedValue({ tag: "python:3.11-slim", id: "x", digest: "y", pinned_at: "z" });
    wrap(<SandboxImagesTab />);
    expect(await screen.findByText("python:3.11-slim")).toBeInTheDocument();
    expect(screen.getByText(/No image is pinned/)).toBeInTheDocument();
    expect(screen.getByText("made by this app")).toBeInTheDocument();
    expect(screen.getByTitle("sha256:" + "f".repeat(64))).toHaveTextContent("sha256:ffffffffffff...");
    fireEvent.click(screen.getAllByRole("button", { name: "Pin" })[0]);
    await waitFor(() => expect(sandbox.pin).toHaveBeenCalledWith("python:3.11-slim"));
  });

  it("says which image is pinned and what happens if its tag moves", async () => {
    const list = images();
    list.images[0].pinned = true;
    vi.mocked(sandbox.imageList).mockResolvedValue({ ...list, pinned: { tag: "python:3.11-slim", id: "x", digest: "sha256:" + "f".repeat(64), pinned_at: "2026-10-10T00:00:00Z" } });
    wrap(<SandboxImagesTab />);
    expect(await screen.findByRole("status")).toHaveTextContent("Verification is pinned to python:3.11-slim");
    expect(screen.getByRole("status")).toHaveTextContent("refuses to run");
    expect(screen.getAllByRole("button", { name: "Pin" })).toHaveLength(1);                    // the pinned image has no Pin button
  });

  it("marks the tool image of Scan Codebase, which is never offered for clean-up", async () => {
    const list = images();
    list.images[1] = { ...list.images[1], tags: ["aivdf/scan-tools:1"], toolkit: true };
    vi.mocked(sandbox.imageList).mockResolvedValue(list);
    wrap(<SandboxImagesTab />);
    expect(await screen.findByText("used by Scan Codebase")).toBeInTheDocument();
    expect(screen.getByText(/Neither the tool image of Scan Codebase nor an image of another project/)).toBeInTheDocument();
  });

  it("explains that Docker is needed when it is not there", async () => {
    vi.mocked(sandbox.imageList).mockResolvedValue({ docker_available: false, images: [], pinned: null });
    wrap(<SandboxImagesTab />);
    expect(await screen.findByText("Docker is not available")).toBeInTheDocument();
  });

  it("starts a build with the Dockerfile and tag written, with no network unless asked", async () => {
    vi.mocked(sandbox.imageList).mockResolvedValue(images());
    vi.mocked(sandbox.build).mockResolvedValue({ id: "j1", stage: "sandbox_build_image", tag: "aivdf/tools:1" });
    wrap(<SandboxImagesTab />);
    await screen.findByText("python:3.11-slim");
    fireEvent.click(screen.getByRole("button", { name: "Build the image" }));
    await waitFor(() => expect(sandbox.build).toHaveBeenCalledWith({ dockerfile: TEMPLATE, tag: "aivdf/tools:1", network: false }));
    fireEvent.click(screen.getByLabelText("Let the build steps use the network"));
    expect(screen.getByRole("alert")).toHaveTextContent("can then reach the internet");
    fireEvent.change(screen.getByLabelText(/^Tag/), { target: { value: "" } });
    expect(screen.getByRole("button", { name: "Build the image" })).toBeDisabled();
  });

  it("offers only what the server plans to remove, asks, and sends exactly those", async () => {
    vi.mocked(sandbox.imageList).mockResolvedValue(images());
    vi.mocked(sandbox.prunePlan).mockResolvedValue({ images: [{ id: "sha256:cccccccccccc", full_id: "sha256:" + "c".repeat(64), tags: ["aivdf/old:1"], size_mb: 10, ours: true, dangling: false, created: "2026-09-01" }], size_mb: 10 });
    vi.mocked(sandbox.prune).mockResolvedValue({ removed: ["sha256:cccccccccccc"], skipped: [], freed_mb: 10 });
    wrap(<SandboxImagesTab />);
    await screen.findByText("python:3.11-slim");
    fireEvent.click(screen.getByRole("button", { name: "Find unused images" }));
    expect(await screen.findByRole("list", { name: "Images that would be removed" })).toHaveTextContent("aivdf/old:1");
    fireEvent.click(screen.getByRole("button", { name: "Remove 1 (10 MB)" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("containers and the pinned image are not touched");
    expect(sandbox.prune).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove 1 image" }));
    await waitFor(() => expect(sandbox.prune).toHaveBeenCalledWith(["sha256:" + "c".repeat(64)]));
  });
});

describe("history", () => {
  const record = (over: Partial<SandboxHistory> = {}): SandboxHistory => ({ name: "aivdf-demo", sessions: [], samples: [
    { at: "2026-10-10T00:00:00Z", cpu_pct: 10, mem_mb: 100, mem_limit_mb: 512 }, { at: "2026-10-10T00:00:10Z", cpu_pct: 30, mem_mb: 150, mem_limit_mb: 512 }], events: [
    { at: "2026-10-10T00:00:01Z", kind: "created" },
    { at: "2026-10-10T00:00:02Z", kind: "command", command: "pytest -q", exit_code: 1, seconds: 3.2, timed_out: false, output_tail: "1 failed" },
    { at: "2026-10-10T00:00:05Z", kind: "command", command: "ls", exit_code: 0, seconds: 0.1, timed_out: false, output_tail: "a.txt" }], ...over });
  const open = async () => {
    vi.mocked(sandbox.historyKnown).mockResolvedValue({ containers: [{ name: "aivdf-demo", events: 3, commands: 2, last: "2026-10-10T00:00:05Z", sessions: 0 }] });
    wrap(<SandboxHistoryTab />);
    fireEvent.click(await screen.findByRole("button", { name: /demo/ }));
  };

  it("shows the commands with how they ended, and the output only on request", async () => {
    vi.mocked(sandbox.historyOf).mockResolvedValue(record());
    await open();
    const events = await screen.findByRole("list", { name: "Events" });
    expect(events).toHaveTextContent("pytest -q");
    expect(events).toHaveTextContent("exit 1");
    expect(events).toHaveTextContent("Created");
    expect(screen.getByText("1 failed").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("CPU")).toBeInTheDocument();
    expect(screen.getByText("Memory")).toBeInTheDocument();
  });

  it("says when nothing is recorded", async () => {
    vi.mocked(sandbox.historyKnown).mockResolvedValue({ containers: [] });
    wrap(<SandboxHistoryTab />);
    expect(await screen.findByText("Nothing recorded yet")).toBeInTheDocument();
  });

  it("saves a named session, opens it as a copy and deletes it only after a question", async () => {
    const saved = { id: "20261010-000000-first", label: "first", saved_at: "2026-10-10T00:01:00Z", events: 3, commands: 2 };
    vi.mocked(sandbox.historyOf).mockResolvedValueOnce(record()).mockResolvedValue(record({ sessions: [saved] }));
    vi.mocked(sandbox.saveSession).mockResolvedValue({ id: saved.id, label: "first", events: 3 });
    vi.mocked(sandbox.session).mockResolvedValue({ ...saved, container: "aivdf-demo", events: [{ at: "2026-10-10T00:00:02Z", kind: "command", command: "old command", exit_code: 0, seconds: 1, output_tail: "" }] });
    vi.mocked(sandbox.deleteSession).mockResolvedValue({ id: saved.id, deleted: true });
    await open();
    await screen.findByRole("list", { name: "Events" });
    fireEvent.change(screen.getByLabelText("Name of the session"), { target: { value: "first" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the session" }));
    await waitFor(() => expect(sandbox.saveSession).toHaveBeenCalledWith("aivdf-demo", "first"));
    fireEvent.click(await screen.findByRole("button", { name: "Open" }));
    expect(await screen.findByText("Session: first")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Events" })).toHaveTextContent("old command");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(sandbox.deleteSession).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete the session" }));
    await waitFor(() => expect(sandbox.deleteSession).toHaveBeenCalledWith("aivdf-demo", saved.id));
  });

  it("forgets a record only after a question that says the container is not touched", async () => {
    vi.mocked(sandbox.historyOf).mockResolvedValue(record());
    vi.mocked(sandbox.forget).mockResolvedValue({ name: "aivdf-demo", forgotten: true });
    await open();
    fireEvent.click(await screen.findByRole("button", { name: "Forget the record" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("The container itself is not touched");
    fireEvent.click(within(dialog).getByRole("button", { name: "Forget the record" }));
    await waitFor(() => expect(sandbox.forget).toHaveBeenCalledWith("aivdf-demo"));
  });

  it("turns samples into series over the seconds since the first", () => {
    const s = sampleSeries(record().samples);
    expect(s.cpu[0].points).toEqual([{ x: 0, y: 10 }, { x: 10, y: 30 }]);
    expect(s.memory[0].points).toEqual([{ x: 0, y: 100 }, { x: 10, y: 150 }]);
    expect(sampleSeries([])).toEqual({ cpu: [], memory: [] });
  });
});
