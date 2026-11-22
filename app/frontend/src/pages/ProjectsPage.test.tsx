import { useEffect, useState } from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mounts = vi.hoisted(() => ({ ide: 0 }));
vi.mock("../sections/projectIde/ProjectIde", () => ({
  ProjectIde: () => {
    const location = useLocation();
    const [draft, setDraft] = useState("");
    useEffect(() => { mounts.ide += 1; }, []);
    return <div data-testid="project-ide"><p>Agent IDE at {location.search || "default workspace"}</p><button aria-label="Keep IDE draft" onClick={() => setDraft("unsaved draft")}>Edit a file</button><p>{draft}</p></div>;
  },
}));
vi.mock("../sections/Patch", () => ({ Patch: () => <p>patch section</p> }));
vi.mock("../sections/Response", () => ({ Response: () => <p>response section</p> }));
import { ProjectsPage } from "./ProjectsPage";

const Where = () => <p data-testid="where">{useLocation().pathname + useLocation().search}</p>;
const at = (path: string) => render(<MemoryRouter initialEntries={[path]}><ProjectsPage /><Where /></MemoryRouter>);
beforeEach(() => { mounts.ide = 0; });

describe("ProjectsPage", () => {
  it("dedicates the page to Projects without legacy section tabs", () => {
    at("/projects");
    expect(screen.getByTestId("project-ide")).toBeVisible();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByText("Patch feasibility")).not.toBeInTheDocument();
    expect(screen.queryByText("Response")).not.toBeInTheDocument();
    expect(screen.queryByText(/Normal Chat|Agentic Mode/)).toBeNull();
  });

  it("leaves project and chat deep links available to the IDE", () => {
    at("/projects?project=shop-ab12cd&chat=review-42");
    expect(screen.getByTestId("project-ide")).toHaveTextContent("?project=shop-ab12cd&chat=review-42");
    expect(screen.getByTestId("where")).toHaveTextContent("/projects?project=shop-ab12cd&chat=review-42");
  });

  it.each(["chat", "agent", "other"])("opens the IDE for a legacy focus=%s address", focus => {
    at(`/projects?project=shop-ab12cd&focus=${focus}`);
    expect(screen.getByTestId("project-ide")).toBeVisible();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByText(/focused conversation|Normal Chat|Agentic Mode/)).toBeNull();
  });

  it.each(["patch", "response"])("opens the IDE for an obsolete %s deep link", tab => {
    at(`/projects?project=shop&chat=review&tab=${tab}`);
    expect(screen.getByTestId("project-ide")).toBeVisible();
    expect(screen.queryByText("patch section")).not.toBeInTheDocument();
    expect(screen.queryByText("response section")).not.toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent("project=shop&chat=review");
    expect(mounts.ide).toBe(1);
  });
});
