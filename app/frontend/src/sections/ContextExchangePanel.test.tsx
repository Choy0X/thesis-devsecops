import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { vi, test, expect, beforeEach } from "vitest";
import { ContextExchangePanel } from "./ContextExchangePanel";
import { contextReview } from "../lib/api";

vi.mock("../components/RunConsole", () => ({ RunConsole: ({ label, disabled }: any) => <button disabled={disabled}>{label}</button> }));
vi.mock("../lib/api", async importOriginal => ({ ...await importOriginal<typeof import('../lib/api')>(), contextReview: {
  status: vi.fn(async () => ({ engine: "local", configured: true, model: "installed-model", execution: { slots: { llm: { runs_on: "cpu" } } } })),
  jsonPreview: vi.fn(async () => ({ upload_id: 'upload', packet_id: "packet", missing: ["pair:8"], items: [
    { item_id: "pair:7", status: "valid", reason: null }, { item_id: "pair:9", status: "stale", reason: "source changed" }] })),
  jsonApply: vi.fn(async () => ({ packet_id: 'packet', items: [], missing: [], imported: 1, unchanged: 0 })),
} }));

beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear();
});

test("shows CPU execution and imports only selected valid drafts", async () => {
  render(<ContextExchangePanel ids={[7]} onDone={() => undefined} />);
  expect(await screen.findByText(/CPU execution/)).toBeTruthy();
  const file = new File(['{}'], "response.json", { type: "application/json" });
  Object.defineProperty(file, "text", { value: async () => '{}' });
  fireEvent.change(screen.getByLabelText("Returned JSON files"), { target: { files: [file] } });
  expect(await screen.findByText("source changed")).toBeTruthy();
  expect(screen.getByText(/1 not returned/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Import 1 selected drafts from this file" }));
  await waitFor(() => expect(contextReview.jsonApply).toHaveBeenCalledWith('upload', ["pair:7"]));
  expect(await screen.findByText(/1 imported/)).toBeTruthy();
});

test('offers byte-bounded JSON export and import without selection', async () => {
  render(<ContextExchangePanel ids={[]} onDone={() => undefined} />);
  expect(screen.getByRole('button', {name: 'Prepare JSON export'})).toBeDisabled();
  expect(screen.getByLabelText('Returned JSON files')).toBeEnabled();
  expect(screen.queryByRole('button', {name: 'Download packet 1'})).toBeNull();
});

test('loads earlier response imports only when requested and exposes no approval controls', async () => {
  render(<ContextExchangePanel ids={[]} onDone={() => undefined} />);
  expect(screen.queryByLabelText('Returned review JSON files')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', {name: 'Import earlier JSON responses'}));
  expect(await screen.findByLabelText('Returned review JSON files')).toHaveAttribute('multiple');
  expect(screen.queryByRole('button', {name: 'Prepare review JSON export'})).not.toBeInTheDocument();
  expect(screen.queryByRole('button', {name: /Approve/})).not.toBeInTheDocument();
});
