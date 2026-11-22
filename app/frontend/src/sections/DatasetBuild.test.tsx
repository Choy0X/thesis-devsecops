import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { AdmissionCounts, BuildCard } from "./DatasetBuild";
vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return { ...actual, api: { buildDetail: vi.fn() } };
});
it('explains that excluded records are retained but not training examples', () => {
  render(<AdmissionCounts counts={{records: 27315, views: 363, supported: 15, protected: 14, insufficient_context: 144, excluded: 27156}} />);
  expect(screen.getByText(/Excluded records produce no training examples/)).toBeInTheDocument();
  expect(screen.getByText(/Protected examples may be derived/)).toBeInTheDocument();
});
it("renders a ready profile row without legacy fields", () => {
  render(<BuildCard b={{ tag: "profile-v1", status: "frozen", tier_mix: { human_reviewed: 12 }, counts: { records: 12, views: 24 } } as any} />);
  expect(screen.getByText("dataset_profile-v1")).toBeInTheDocument();
  expect(screen.getByText("frozen")).toBeInTheDocument();
  expect(screen.getByText(/Training readiness follows/)).toBeInTheDocument();
  expect(screen.queryByText(/auto_checked and above can supervise/)).not.toBeInTheDocument();
});
it("labels external datasets accurately when optional legacy metadata is absent", () => {
  render(<BuildCard b={{ tag: "imported", status: "external" } as any} />);
  expect(screen.getByText("external")).toBeInTheDocument();
  expect(screen.queryByText("preview")).not.toBeInTheDocument();
});
it('shows automatic admission outcomes and keeps historical review provenance in details', () => {
  render(<BuildCard b={{tag: 'auto-v1', status: 'frozen', counts: {automatically_eligible: 12, insufficient_context: 4, excluded: 3}, tier_mix: {auto_checked: 12}}} />);
  expect(screen.getByText('Automatically eligible').parentElement).toHaveTextContent('12');
  expect(screen.getByText('Insufficient context').parentElement).toHaveTextContent('4');
  expect(screen.getByText('Excluded').parentElement).toHaveTextContent('3');
  expect(screen.getByText(/human reviewed:/)).not.toBeVisible();
});
it('shows supported and protected records separately from insufficient and excluded outcomes', () => {
  render(<AdmissionCounts counts={{supported: 7, protected: 5, insufficient_context: 3, excluded: 2, automatically_eligible: 12}} />);
  expect(screen.getByText('Supported vulnerable').parentElement).toHaveTextContent('7');
  expect(screen.getByText('Protected').parentElement).toHaveTextContent('5');
  expect(screen.getByText('Insufficient context').parentElement).toHaveTextContent('3');
  expect(screen.getByText('Excluded').parentElement).toHaveTextContent('2');
});
