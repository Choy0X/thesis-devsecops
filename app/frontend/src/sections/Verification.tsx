import { useEffect, useState } from "react";
import { Boxes } from "lucide-react";
import { EmptyState, ErrorState, Field, Panel, Skeleton } from "../components/ui";
import { SelectField } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { DataTable, type Column } from "../components/hud/DataTable";
import { Dialog } from "../components/hud/Dialog";
import { VerificationTimeline, type Receipt } from "../components/VerificationTimeline";
import { Dev } from "../lib/devMode";
import { api, errorText, sandbox, type PilotCase, type PilotSummary, type ReceiptRow } from "../lib/api";

// What verification has found. The pilot replayed the real fix commits of known vulnerabilities in a container with no network, to see how often a fix can be
// reproduced and checked end to end: the test that came with the fix must fail on the old code and pass on the fixed code, and the rest of the project's
// tests must still pass. It measures the method, not a model, and nothing in it was attached to a dataset record.

export const OUTCOME: Record<string, string> = {
  verified: "Verified: the vulnerability showed before the fix, is gone after it, and the code still works",
  fixed_but_functionality_failed: "The vulnerability is gone, but the project's other tests fail",
  fixed_functionality_not_usable: "The vulnerability is gone, but the project's other tests could not be used here",
  not_reproduced: "The vulnerability did not show on the old code",
  test_not_collected: "The test runner did not find the security test",
  acceptance_needs_dependency: "The security test needs a package that is not installed",
  acceptance_needs_network: "The security test needs the network, which the container does not have",
  fix_did_not_pass: "The fix did not pass its own security test",
  patch_did_not_apply: "The change did not apply",
  patch_failed_build: "The fixed code did not build",
  integrity_failed: "The change touched files the verifier relies on",
  unavailable: "The case could not be prepared",
  environment_error: "The environment failed, so the case decided nothing",
};

const VERDICT_WORD: Record<string, string> = { verified: "Verified", not_verified: "Not verified", env_error: "Could not be decided" };

const COLUMNS: Column<ReceiptRow>[] = [
  { key: "case", label: "Case", render: (r) => <span className="font-mono text-[0.76rem]">{r.name.replace(/^.*~/, "").replace(/^pilot_/, "").replace(/_[0-9a-f]{8}$/, "")}{r.run && r.run !== "verify_pilot" ? <span className="ml-2 font-sans text-[0.7rem] text-faint">(run again)</span> : null}</span>, sortValue: (r) => r.name },
  { key: "verdict", label: "Result", width: "11rem", render: (r) => (r.readable ? (VERDICT_WORD[r.verdict ?? ""] ?? r.verdict) : "Unreadable"), sortValue: (r) => r.verdict ?? "" },
  { key: "grade", label: "Grade", width: "5rem", render: (r) => (r.grade && r.grade !== "none" ? r.grade : "none") },
  { key: "sound", label: "Receipt", width: "11rem", render: (r) => (!r.readable ? "Cannot be read" : r.sound ? "Intact" : "Does not match its content") },
];

export function Verification() {
  const [pilot, setPilot] = useState<{ ran: boolean; summary: PilotSummary | null } | null>(null);
  const [rows, setRows] = useState<ReceiptRow[] | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [open, setOpen] = useState<string | null>(null);
  const load = () => {
    setErr(null);
    api.verifyPilot().then(setPilot).catch(setErr);
    api.verifyReceipts().then((r) => setRows(r.receipts)).catch(setErr);
  };
  useEffect(load, []);
  if (err != null && !rows) return <ErrorState error={err} title="Could not read the verification results" onRetry={load} />;
  if (!pilot || !rows) return <Skeleton className="h-48 w-full" />;
  const s = pilot.summary;
  return (
    <div className="space-y-5">
      <Panel title="Verification pilot" desc="Known fixes replayed in a container, to see how often a fix can be reproduced and checked.">
        {!pilot.ran || !s ? (
          <EmptyState icon={<Boxes size={20} />} title="No pilot has been run yet" hint="Running one starts Docker work for a long time, so it is started on purpose and not from this page." />
        ) : (
          <div className="space-y-3 text-[0.82rem]">
            <p><b className="tnum text-[1.1rem] font-semibold">{s.verified} of {s.cases_attempted}</b> cases were verified, from {s.libraries.length} libraries, in about {Math.round(s.seconds / 60)} minutes.
              In {s.reproduced_before_the_fix} of {s.cases_attempted} the vulnerability showed on the old code.</p>
            <ul aria-label="Outcomes" className="space-y-1">
              {Object.entries(s.outcomes).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]).map(([k, n]) => (
                <li key={k} className="flex justify-between gap-4 border-b border-line py-1"><span className="text-muted">{OUTCOME[k] ?? k.replace(/_/g, " ")}</span><span className="tnum">{n}</span></li>
              ))}
            </ul>
            <p className="text-[0.76rem] text-faint">This measures the method on real fixes. It says nothing about how good a model's patches are, and no result was attached to a dataset record.</p>
          </div>
        )}
      </Panel>
      <Panel title="Receipts" desc="One receipt per case, with the six steps it went through. Open one to read it.">
        <DataTable caption="Verification receipts" columns={COLUMNS} rows={rows} rowKey={(r) => r.name} height={360} onRowClick={(r) => r.readable && setOpen(r.name)}
          empty={<EmptyState title="No receipts yet" hint="A receipt is written for every case a pilot attempts." />} />
      </Panel>
      <RunCase onDone={load} />
      {open && <ReceiptWindow name={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

/** Verify one case of the saved pilot again, in the hardened container. It needs Docker, and the first step builds the project's environment, which fetches its
 *  packages from the internet; the checks themselves run with no network. The result is a new receipt in its own folder and the saved pilot is not touched. */
export function RunCase({ onDone }: { onDone: () => void }) {
  const [cases, setCases] = useState<PilotCase[] | null>(null);
  const [docker, setDocker] = useState<boolean | null>(null);
  const [chosen, setChosen] = useState("");
  useEffect(() => {
    api.verifyCases().then((r) => { const list = r.cases ?? []; setCases(list); setChosen(list[0]?.case_id ?? ""); }).catch(() => setCases([]));         // ignored: without the list there is nothing to choose
    sandbox.status().then((s) => setDocker(!!s?.docker_available)).catch(() => setDocker(false));                                // ignored: no answer is treated as no Docker
  }, []);
  if (cases === null) return null;
  const label = (c: PilotCase) => `${c.library}, ${c.case_id.replace(/^pilot_/, "")}${c.outcome ? ` (last: ${c.outcome.replace(/_/g, " ")})` : ""}`;
  return (
    <Panel title="Verify a case again" desc="Runs one saved case in a container with no network for the checks. It takes minutes, and the first step fetches the project's packages from the internet.">
      {cases.length === 0 ? <p className="text-[0.82rem] text-muted">There are no saved cases to run again.</p> : (
        <div className="space-y-3">
          <SelectField label="Case" aria-label="Case to verify" value={chosen} onValueChange={setChosen} options={cases.map((c) => ({ value: c.case_id, label: label(c) }))} />
          {docker === false && <p role="status" className="border border-warn-line bg-warn-tint px-3 py-2 text-[0.78rem] text-warn">Docker is not running. Start Docker Desktop, then open this page again.</p>}
          <RunConsole stage="verify_case" label="Verify again" params={{ case_id: chosen }} onDone={onDone} disabled={docker !== true || !chosen}
            confirmText="This starts containers with no network for the checks, after building the project's environment from the internet. It can take several minutes and does not change the saved pilot. Start it?"
            hint="The result is a new receipt, listed above as a run again." />
        </div>
      )}
    </Panel>
  );
}

function ReceiptWindow({ name, onClose }: { name: string; onClose: () => void }) {
  const [data, setData] = useState<{ receipt: Receipt; problems: string[] } | null>(null);
  const [problem, setProblem] = useState("");
  useEffect(() => { api.verifyReceipt(name).then(setData).catch((e) => setProblem(errorText(e))); }, [name]);
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }} title="Verification receipt" description={name.replace(/^pilot_/, "").replace(/_[0-9a-f]{8}$/, "")} className="w-[min(44rem,96vw)] max-h-[92vh]">
      {problem ? <p role="alert" className="text-[0.8rem] text-crit-soft">{problem}</p> : !data ? <Skeleton className="h-48 w-full" /> : (
        <div className="space-y-3">
          {data.problems.length > 0 && (
            <div role="alert" className="border border-crit-line bg-crit-tint px-3 py-2 text-[0.78rem] text-crit-soft">
              This receipt does not match its own content, so it cannot be trusted.<Dev><ul className="mt-1 list-disc pl-5 font-mono text-[0.7rem]">{data.problems.map((p) => <li key={p}>{p}</li>)}</ul></Dev>
            </div>
          )}
          <VerificationTimeline receipt={data.receipt} />
        </div>
      )}
    </Dialog>
  );
}
