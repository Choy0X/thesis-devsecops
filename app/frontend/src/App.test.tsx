import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// Each page is replaced by a stub that counts how many times it was mounted and keeps a value in its own state.
const mounts: Record<string, number> = {};
vi.mock("./sections/Overview", async () => await stub("Overview", "Overview"));
vi.mock("./pages/ModelHubPage", async () => await stub("ModelHubPage", "ModelHub"));
vi.mock("./pages/CollectionsPage", async () => await stub("CollectionsPage", "Collections"));
vi.mock("./pages/CampaignsPage", async () => await stub("CampaignsPage", "Campaigns"));
vi.mock("./pages/RecordsPage", async () => await stub("RecordsPage", "Records"));
vi.mock("./pages/CodePairsPage", async () => await stub("CodePairsPage", "CodePairs"));
vi.mock("./pages/BuildPage", async () => await stub("BuildPage", "Build"));
vi.mock("./pages/TrainPage", async () => await stub("TrainPage", "Train"));
vi.mock("./pages/SandboxPage", async () => await stub("SandboxPage", "Sandbox"));
vi.mock("./pages/ProjectsPage", async () => await stub("ProjectsPage", "Projects"));
vi.mock("./pages/SettingsPage", async () => await stub("SettingsPage", "SettingsPage"));
vi.mock("./components/ComputingChip", () => ({ ComputingChip: () => null }));
vi.mock("./components/DatabaseBanner", () => ({ DatabaseBanner: () => null }));
vi.mock("./components/DeviceStrip", () => ({ DeviceStrip: () => null }));
vi.mock("./components/JobsTray", () => ({ JobsTray: () => null }));
vi.mock("./components/TitleBar", () => ({ TitleBar: () => <div data-testid="titlebar" /> }));
vi.mock("./components/CloseGuard", () => ({ CloseGuard: () => <div data-testid="closeguard" /> }));
vi.mock("./lib/dataSync", () => ({ DataSyncProvider: ({ children }: any) => children, SyncButton: () => <span>sync</span> }));
vi.mock("./lib/api", async () => {
  const actual = await vi.importActual<typeof import("./lib/api")>("./lib/api");
  return { ...actual, api: { getSettings: vi.fn() } };
});

async function stub(exported: string, label: string) {
  const React = await import("react");
  const Page = (props: { go?: (id: string) => void }) => {
    const [text, setText] = React.useState("");
    React.useEffect(() => { mounts[label] = (mounts[label] || 0) + 1; }, []);
    return React.createElement("div", null,
      React.createElement("input", { "aria-label": `${label} input`, value: text, onChange: (e: any) => setText(e.target.value) }),
      props.go ? React.createElement("button", { onClick: () => props.go!("data") }, "open data") : null);
  };
  return { [exported]: Page };
}

import App from "./App";
import { api } from "./lib/api";
import { legacyTarget, redirectOf, resolve, SECTIONS } from "./routes";

beforeEach(() => {
  for (const k of Object.keys(mounts)) delete mounts[k];
  vi.mocked(api.getSettings).mockResolvedValue({} as any);
  delete document.documentElement.dataset.motion;
});

const renderAt = (path: string) => render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
// A page is loaded the first time it is opened, so wait until its stub is on screen.
const go = async (label: string, stubLabel: string) => {
  fireEvent.click(screen.getByRole("link", { name: label }));
  await screen.findByLabelText(`${stubLabel} input`);
};

describe("the left rail", () => {
  it("has the eleven sections in order, each a link", () => {
    renderAt("/");
    const names = screen.getAllByRole("link").map((a) => a.textContent).filter((t) => SECTIONS.some((s) => s.label === t));
    expect(names).toEqual(["Overview", "Model Hub", "CVE Collections", "Campaigns", "Source records", "Collected data", "Build dataset", "Train", "Sandbox", "Projects", "Settings"]);
    expect(SECTIONS).toHaveLength(11);
  });

  it("marks the open section and names it in the header", async () => {
    renderAt("/records");
    await screen.findByLabelText("Records input");
    expect(screen.getByRole("link", { name: "Source records" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Source records");
  });
});

describe("the shell mounts only the current page", () => {
  it("unmounts inactive pages and mounts them again when revisited", async () => {
    renderAt("/");
    await screen.findByLabelText("Overview input");
    expect(mounts).toEqual({ Overview: 1 });
    await go("Settings", "SettingsPage");
    await go("Source records", "Records");
    await go("Overview", "Overview");
    await go("Settings", "SettingsPage");
    expect(mounts).toEqual({ Overview: 2, SettingsPage: 2, Records: 1 });
    expect(screen.queryByLabelText("Sandbox input")).toBeNull();            // a page nobody opened is not mounted
  });

  it("has no inactive page tree after navigation", async () => {
    renderAt("/settings");
    await screen.findByLabelText("SettingsPage input");
    await go("Overview", "Overview");
    expect(screen.queryByLabelText("SettingsPage input")).not.toBeInTheDocument();
  });
});

describe("addresses", () => {
  it("opens a section straight from its address", async () => {
    renderAt("/train");
    expect(await screen.findByLabelText("Train input")).toBeInTheDocument();
    expect(mounts).toEqual({ Train: 1 });
  });

  it("leads an address of the earlier layout to the section that holds that page now", async () => {
    for (const [id, label] of [["data", "Records"], ["pairs", "CodePairs"], ["build", "Build"], ["review", "CodePairs"], ["finetune", "Train"], ["results", "Train"], ["engine", "Train"],
      ["sandbox", "Sandbox"], ["settings", "SettingsPage"], ["logs", "SettingsPage"], ["collection", "Collections"], ["overview", "Overview"]]) {
      const { unmount } = renderAt(`/legacy/${id}`);
      expect(await screen.findByLabelText(`${label} input`)).toBeInTheDocument();
      unmount();
    }
  });

  it("says so for an address nobody owns, and offers the way home", () => {
    renderAt("/nowhere");
    expect(screen.getByText("There is no page at this address")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to the Overview" })).toHaveAttribute("href", "/");
  });

  it("sends a link written for the earlier layout to the section that holds that page now", async () => {
    renderAt("/");
    fireEvent.click(await screen.findByRole("button", { name: "open data" }));
    expect(await screen.findByLabelText("Records input")).toBeInTheDocument();
    expect(legacyTarget("results")).toBe("/train");
    expect(legacyTarget("review")).toBe("/pairs?tab=context");
    expect(legacyTarget("unknown")).toBe("/");
  });

  it("resolves only the paths it owns", () => {
    expect(resolve("/")?.key).toBe("overview");
    expect(resolve("/pairs/")?.key).toBe("pairs");
    expect(resolve("/legacy/data")).toBeNull();
    expect(resolve("/legacy")).toBeNull();
    expect(redirectOf("/legacy/data")).toBe("/records");
    expect(redirectOf("/legacy/review/")).toBe("/pairs?tab=context");
    expect(redirectOf("/legacy/nothing")).toBe("/");
    expect(redirectOf("/legacy")).toBeNull();
    expect(redirectOf("/records")).toBeNull();
  });
});

describe("keyboard and motion", () => {
  it("offers a way past the rail to the page, the first thing a keyboard reaches, and it moves focus to the page", async () => {
    renderAt("/");
    await screen.findByLabelText("Overview input");
    const skip = screen.getByRole("link", { name: "Skip to the page" });
    expect(skip).toHaveAttribute("href", "#content");
    expect(document.querySelectorAll("a, button, input, [tabindex]")[0]).toBe(skip);
    fireEvent.click(skip);
    expect(document.activeElement).toBe(document.getElementById("content"));
    expect(screen.getByRole("main")).toHaveAttribute("id", "content");
  });

  it("cuts animation when the window is drawn without the graphics card, and leaves it alone otherwise", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ ui_gpu: "off" } as any);
    const { unmount } = renderAt("/");
    await screen.findByLabelText("Overview input");
    await vi.waitFor(() => expect(document.documentElement.dataset.motion).toBe("reduced"));
    unmount();
    delete document.documentElement.dataset.motion;
    vi.mocked(api.getSettings).mockResolvedValue({ ui_gpu: "" } as any);
    renderAt("/");
    await screen.findByLabelText("Overview input");
    await new Promise((r) => setTimeout(r, 30));
    expect(document.documentElement.dataset.motion).toBeUndefined();
  });

  it("carries on when the settings cannot be read", async () => {
    vi.mocked(api.getSettings).mockRejectedValue(new Error("offline"));
    renderAt("/");
    expect(await screen.findByLabelText("Overview input")).toBeInTheDocument();
  });
});

describe("the Projects IDE shell", () => {
  it.each([
    "/projects",
    "/projects?project=shop",
    "/projects?project=shop&chat=review",
    "/projects?project=shop&focus=chat",
    "/projects?project=shop&focus=agent",
    "/projects?focus=chat",
    "/projects?focus=agent",
    "/projects?focus=other",
  ])("gives the IDE the available page area while retaining app chrome at %s", async path => {
    renderAt(path);
    await screen.findByLabelText("Projects input");
    expect(screen.getByTestId("titlebar")).toBeInTheDocument();
    expect(screen.getByTestId("closeguard")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Sections" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Projects");
    const main = document.getElementById("content")!;
    expect(main).toHaveClass("overflow-hidden");
    expect(main.firstElementChild).toHaveClass("h-full", "w-full");
    expect(main.firstElementChild).not.toHaveClass("max-w-6xl");
    expect(main.querySelector("section")).toHaveClass("h-full");
  });

  it.each(["patch", "response"])("uses the full workbench for an obsolete %s tab link", async tab => {
    renderAt(`/projects?project=shop&focus=chat&tab=${tab}`);
    await screen.findByLabelText("Projects input");
    expect(screen.getByRole("navigation", { name: "Sections" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Projects");
    const main = document.getElementById("content")!;
    expect(main).toHaveClass("overflow-hidden");
    expect(main.firstElementChild).toHaveClass("h-full", "w-full");
    expect(main.firstElementChild).not.toHaveClass("max-w-6xl");
    expect(main.querySelector("section")).toHaveClass("h-full");
  });

  it("restores IDE width while remounting after visiting another section", async () => {
    renderAt("/projects?project=shop");
    await screen.findByLabelText("Projects input");
    fireEvent.change(screen.getByLabelText("Projects input"), { target: { value: "draft edit" } });
    await go("Settings", "SettingsPage");
    expect(document.getElementById("content")!.firstElementChild).toHaveClass("max-w-6xl");
    await go("Projects", "Projects");
    expect(document.getElementById("content")!.firstElementChild).not.toHaveClass("max-w-6xl");
    expect(screen.getByLabelText("Projects input")).toHaveValue("");
    expect(mounts.Projects).toBe(2);
  });
});
