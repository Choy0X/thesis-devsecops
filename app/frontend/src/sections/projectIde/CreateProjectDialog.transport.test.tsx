import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { resetRequests } from "../../lib/request";
import { CreateProjectDialog } from "./CreateProjectDialog";

afterEach(() => { resetRequests(); vi.unstubAllGlobals(); });

it("sends valid JSON through the real client when browsing and creating a project", async () => {
  const project = { id: "payments", name: "Payments", source: "D:/code/payments" };
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    calls.push(url);
    // Headers applies the same case-insensitive merging as the browser's fetch.
    const headers = new Headers(init.headers);
    if (init.method === "POST") {
      expect(headers.get("content-type")).toBe("application/json");
      if (url === "/api/shell/pick-folder") {
        expect(JSON.parse(String(init.body))).toEqual({ title: "Choose the project source folder" });
        return Response.json({ ok: true, id: "pick" });
      }
      expect(url).toBe("/api/ide/projects");
      expect(JSON.parse(String(init.body))).toEqual({ name: project.name, source: project.source });
      return Response.json(project);
    }
    expect(url).toBe("/api/shell/pick-folder/pick");
    return Response.json({ state: "chosen", path: project.source });
  }));
  const onCreated = vi.fn();
  render(<CreateProjectDialog onClose={vi.fn()} onCreated={onCreated} />);
  fireEvent.change(screen.getByRole("textbox", { name: "Project name" }), { target: { value: project.name } });
  fireEvent.click(screen.getByRole("button", { name: "Browse folders" }));
  expect(await screen.findByText(project.source)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Create project" }));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith(project));
  expect(calls).toEqual(["/api/shell/pick-folder", "/api/shell/pick-folder/pick", "/api/ide/projects"]);
});
