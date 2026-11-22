import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { Dependencies } from "./Dependencies";

it("keeps update status separate from advisory evidence and exposes limits", () => {
  render(<Dependencies packages={[{ id: "npm:example", name: "example", ecosystem: "npm", installed: ["1.0.0"], latest_stable: "2.0.0", outdated: true,
    checks: { "1.0.0": {
      status: "affected", lookup: { status: "fresh", fetched_at: 1700000000, age_seconds: 10 },
      advisories: [{ id: "GHSA-test", ids: ["GHSA-test", "CVE-2024-12345"], severity: [{ type: "CVSS_V3", score: "7.5" }],
        affected: [{ ranges: [{ events: [{ introduced: "0" }, { fixed: "1.0.1" }] }] }],
      }],
    } }, recommendation: { version: "1.0.1", limitations: "API compatibility requires validation." }, recommendation_limits: "Only fresh advisory checks support recommendations." }]} selection={null} onSelection={() => undefined} />);
  expect(screen.getByText("Outdated")).toBeInTheDocument();
  expect(screen.getByText("1 known advisory")).toBeInTheDocument();
  expect(screen.getByText("2.0.0")).toBeInTheDocument();
  expect(screen.getByText("1.0.1")).toBeInTheDocument();
  expect(screen.getByText("API compatibility requires validation.")).toBeInTheDocument();
  expect(screen.getAllByText(/CVE-2024-12345/).length).toBeGreaterThan(0);
  expect(screen.getByText(/Source availability: unknown/)).toBeInTheDocument();
});
it("never treats absent advisory checks as a clean result", () => {
  render(<Dependencies packages={[{ id: "PyPI:demo", name: "demo", ecosystem: "PyPI", locked: ["1.0"] }]} selection={null} onSelection={() => undefined} />);
  expect(screen.getByText("Not checked")).toBeInTheDocument();
  expect(screen.queryByText("No known vulnerabilities")).not.toBeInTheDocument();
  expect(screen.getByText("None established")).toBeInTheDocument();
});
