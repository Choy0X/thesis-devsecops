import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { dbStatus: vi.fn(), dbStart: vi.fn() } };
});
import { api, type DbStatus } from "../lib/api";
import { DatabaseBanner } from "./DatabaseBanner";

const status = (over: Partial<DbStatus>): DbStatus => ({
  engine: "sqlite", embedded: true, path: "/records/aivf.sqlite3", uri: "/records/aivf.sqlite3", database: "aivf", reachable: true, container: { name: "", state: "not_required" }, ...over,
});

beforeEach(() => {
  vi.mocked(api.dbStatus).mockReset();
  vi.mocked(api.dbStart).mockReset();
});

describe("DatabaseBanner", () => {
  it("shows nothing while the database answers", async () => {
    vi.mocked(api.dbStatus).mockResolvedValue(status({}));
    const { container } = render(<DatabaseBanner pollMs={60_000} />);
    await waitFor(() => expect(api.dbStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("explains a local file failure and rechecks it on request", async () => {
    vi.mocked(api.dbStatus).mockResolvedValueOnce(status({ reachable: false, reason: "The folder cannot be written to." }))
      .mockResolvedValue(status({}));
    render(<DatabaseBanner pollMs={60_000} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The folder cannot be written to.");
    expect(screen.getByRole("alert")).toHaveTextContent("/records/aivf.sqlite3");
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(api.dbStart).not.toHaveBeenCalled();
  });

  it("offers a recheck for local storage errors without a daemon control", async () => {
    vi.mocked(api.dbStatus).mockResolvedValue(status({ reachable: false, reason: "The database is busy." }));
    render(<DatabaseBanner pollMs={60_000} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("The database is busy.");
    expect(screen.queryByRole("button", { name: "Start the database" })).toBeNull();
    expect(screen.getByRole("button", { name: "Check again" })).toBeInTheDocument();
  });
});
