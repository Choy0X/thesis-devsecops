import { useEffect, useState } from "react";
import { Button, Field, Input, Panel, Skeleton, Textarea } from "../components/ui";
import { SelectField } from "../components/kit";
import { api, type AuditSample, type ErrorRates, type ReviewDetail, type ReviewItem, type ReviewQueue, type ReviewSummary } from "../lib/api";
import { num } from "../lib/format";
import { useOnActivate } from "../components/ActivePage";
import { useWorkspaceState } from "../lib/workspaceState";
import { ReviewExchangePanel } from "./ReviewExchangePanel";

const LABELS = [
  ["vulnerability_supported", "Vulnerability supported by the code shown"],
  ["no_supported_finding_in_scope", "No supported finding in the scope checked"],
  ["insufficient_context", "Insufficient context to decide"],
] as const;
const PAGE = 100;
const REVIEWER_KEY = "ztsa.reviewer";

// The review queue (pipeline/review.py, rubric_v1): gate candidates waiting for a person, one decision at a time,
// and a stratified audit sample that estimates how often the automatic checks are wrong.
export function Review() {
  const [sum, setSum] = useState<ReviewSummary | null>(null);
  const [q, setQ] = useState<ReviewQueue | null>(null);
  const [page, setPage] = useWorkspaceState("review.page", 0);
  const [eco, setEco] = useWorkspaceState("review.eco", "");
  const [reviewState, setReviewState] = useWorkspaceState<'waiting' | 'ai_adjudicated' | 'all'>("review.reviewState", 'waiting');
  const [waitingTotal, setWaitingTotal] = useState(0);
  const [open, setOpen] = useWorkspaceState<string | null>("review.open", null);
  const [err, setErr] = useState("");

  const loadSummary = () => api.reviewSummary().then(setSum).catch((e) => setErr(String(e?.message || e)));
  const loadQueue = () => Promise.all([
    api.reviewQueue({ limit: PAGE, offset: page * PAGE, ecosystem: eco, review_state: reviewState }),
    reviewState === 'waiting' ? Promise.resolve(null) : api.reviewQueue({ limit: 1, offset: 0, ecosystem: eco, review_state: 'waiting' }),
  ]).then(([x, waiting]) => { setQ(x); setWaitingTotal(waiting ? waiting.total : x.total); setErr(""); })
    .catch((e) => setErr(String(e?.message || e)));
  useEffect(() => { loadSummary(); }, []);
  useOnActivate(() => { loadSummary(); loadQueue(); });
  useEffect(() => { loadQueue(); }, [page, eco, reviewState]);
  const refresh = () => { loadSummary(); loadQueue(); };

  if (!sum || !q) return err ? <p className="text-[0.82rem] text-crit">Could not read the review queue: {err}</p> : <Skeleton className="h-48 w-full" />;
  const failed = Object.entries(sum.failed).sort((a, b) => b[1] - a[1]);

  return (
    <div className="space-y-5">
      <Panel title="Review" desc={`Rubric ${sum.rubric_version}. Candidates are rows whose deterministic checks passed; a decision raises a row above auto_checked, and unreviewed rows never supervise a model.`}>
        <div className="flex flex-wrap gap-x-8 gap-y-2 text-[0.82rem]">
          <Stat label="Usable pairs" v={num(sum.rows)} />
          <Stat label="Candidates" v={num(sum.candidates)} />
          <Stat label="Human reviewed" v={num(sum.tiers.human_reviewed || 0)} />
          <Stat label="Decisions" v={num(sum.decisions)} />
          <Stat label="Gate error rate (audit)" v={sum.audit_error_rate === "unestimated" ? "not estimated yet" : `${(sum.audit_error_rate * 100).toFixed(1)}%`} />
        </div>
        {sum.not_evaluated.length > 0 && (
          <p className="mt-3 text-[0.74rem] text-faint">Not evaluated yet at this stage: {sum.not_evaluated.map((n) => n.replace(/_/g, " ")).join(", ")}. They are inputs of the dataset build, which makes the final tier.</p>
        )}
        {failed.length > 0 && (
          <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-[0.76rem] text-muted">
            {failed.slice(0, 8).map(([k, n]) => <li key={k}>{k.replace(/_/g, " ")} failed: <b className="font-medium text-fg tnum">{num(n)}</b></li>)}
          </ul>
        )}
      </Panel>

      <div className="flex flex-wrap items-end gap-4">
        <div className="w-64"><SelectField label="Review state" value={reviewState} onValueChange={(value) => { setPage(0); setOpen(null); setReviewState(value as typeof reviewState); }} options={[
          {value: 'waiting', label: 'Waiting for review'}, {value: 'ai_adjudicated', label: 'AI adjudicated'}, {value: 'all', label: 'All review states'},
        ]} /></div>
        <div className="w-48"><Field label="Ecosystem"><Input placeholder="All ecosystems" value={eco} onChange={(e) => { setPage(0); setEco(e.target.value); }} /></Field></div>
      </div>
      <ReviewExchangePanel ecosystem={eco || undefined} pairKeys={q.items.map(item => item.pair_key)} waitingTotal={waitingTotal} onChanged={refresh} />

      <Panel title="Queue" desc={`${num(q.total)} ${reviewState === 'waiting' ? 'waiting' : reviewState === 'ai_adjudicated' ? 'AI adjudicated, available for independent human review' : 'across all review states'}, strongest evidence first.`}>
        {q.items.length === 0 ? <p className="text-[0.82rem] text-muted">{reviewState === 'waiting' ? 'Nothing is waiting for review.' : 'No candidates match this review state.'}</p> : (
          <ul className="divide-y divide-line">
            {q.items.map((it) => <Row key={it.pair_key} it={it} active={open === it.pair_key} onOpen={() => setOpen(open === it.pair_key ? null : it.pair_key)}>
              {open === it.pair_key && <Detail pairKey={it.pair_key} onDecided={() => { setOpen(null); refresh(); }} />}
            </Row>)}
          </ul>
        )}
        <div className="mt-3 flex items-center gap-3 text-[0.78rem] text-muted">
          <Button variant="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</Button>
          <span>{q.total ? `${page * PAGE + 1}-${Math.min((page + 1) * PAGE, q.total)} of ${num(q.total)}` : "0"}</span>
          <Button variant="ghost" disabled={(page + 1) * PAGE >= q.total} onClick={() => setPage(page + 1)}>Next</Button>
        </div>
      </Panel>

      <Audit onOpen={(k) => setOpen(k)} onChanged={loadSummary} />
    </div>
  );
}

function Stat({ label, v }: { label: string; v: string }) {
  return <span><span className="text-muted">{label} </span><b className="font-medium tnum">{v}</b></span>;
}

function Row({ it, active, onOpen, children }: { it: ReviewItem; active: boolean; onOpen: () => void; children?: React.ReactNode }) {
  return (
    <li className="py-2">
      <button className="flex w-full flex-wrap items-baseline gap-x-4 gap-y-0.5 text-left" onClick={onOpen}>
        <span className="text-[0.82rem] font-medium">{it.package || it.repository}</span>
        <span className="font-mono text-[0.74rem] text-muted">{it.file_path}{it.function_name ? ` :: ${it.function_name}` : ""}</span>
        <span className="text-[0.72rem] text-faint">{it.ecosystem} - {it.mechanism.replace(/_/g, " ")} - {it.granularity} - link {it.evidence_level} - {(it.attribution_basis || "").replace(/_/g, " ")}</span>
        <span className="ml-auto text-[0.72rem] text-faint">{active ? "close" : "review"}</span>
      </button>
      {children}
    </li>
  );
}

function Detail({ pairKey, onDecided }: { pairKey: string; onDecided: () => void }) {
  const [d, setD] = useState<ReviewDetail | null>(null);
  const [reviewer, setReviewer] = useWorkspaceState(`review.detail.${pairKey}.reviewer`, () => { try { return localStorage.getItem(REVIEWER_KEY) || ""; } catch { return ""; } });
  const [label, setLabel] = useWorkspaceState<string>(`review.detail.${pairKey}.label`, "vulnerability_supported");
  const [invariant, setInvariant] = useWorkspaceState(`review.detail.${pairKey}.invariant`, "");
  const [security, setSecurity] = useWorkspaceState(`review.detail.${pairKey}.security`, { attacker_control: "", preconditions: "", condition_scope: "", decisive_safeguard: "", evidence: "", behavioral_intent: "", fixed_scope: "", fixed_safeguard: "", fixed_evidence: "" });
  const change = (key: keyof typeof security, value: string) => setSecurity((s) => ({ ...s, [key]: value }));
  const [missing, setMissing] = useWorkspaceState(`review.detail.${pairKey}.missing`, "");
  const [lines, setLines] = useWorkspaceState(`review.detail.${pairKey}.lines`, "");
  const [notes, setNotes] = useWorkspaceState(`review.detail.${pairKey}.notes`, "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => { api.reviewItem(pairKey).then(setD).catch((e) => setErr(String(e?.message || e))); }, [pairKey]);
  if (!d) return err ? <p className="mt-2 text-[0.8rem] text-crit">{err}</p> : <Skeleton className="mt-3 h-40 w-full" />;
  const r = d.row;

  const submit = async () => {
    setBusy(true); setErr("");
    try {
      try { localStorage.setItem(REVIEWER_KEY, reviewer); } catch { /* ignored: remembering the reviewer name is optional */ }
      const fields: Record<string, any> = {};
      if (invariant.trim()) fields.violated_invariant = invariant.trim();
      for (const key of ["attacker_control", "condition_scope", "decisive_safeguard", "behavioral_intent"] as const) if (security[key].trim()) fields[key] = security[key].trim();
      if (security.preconditions.trim()) fields.preconditions = security.preconditions.split("\n").map((s) => s.trim()).filter(Boolean);
      if (security.evidence.trim()) fields.evidence_spans = [{ quote: security.evidence.trim(), reviewed: true }];
      if (security.fixed_scope.trim() || security.fixed_safeguard.trim() || security.fixed_evidence.trim()) fields.fixed_review = {
        scope: security.fixed_scope.trim(), safeguard: security.fixed_safeguard.trim(), evidence: [{ quote: security.fixed_evidence.trim() }],
      };
      if (label === "insufficient_context") fields.missing_facts = missing.split("\n").map((x) => x.trim()).filter(Boolean);
      if (label === "vulnerability_supported" && lines.trim()) {
        const spans = parseSpans(lines);
        if (typeof spans === "string") throw new Error(spans);
        fields.reviewed_security_spans = spans;
      }
      await api.reviewDecision({ pair_key: pairKey, reviewer: reviewer.trim(), rubric_version: d.rubric_version, review_status: "human_reviewed",
        label_state: label, fields, notes: notes.trim() || undefined });
      onDecided();
    } catch (e: any) { setErr(String(e?.message || e)); } finally { setBusy(false); }
  };

  return (
    <div className="mt-3 space-y-3 rounded-lg border border-line bg-surface-2 p-4">
      <div className="flex flex-wrap gap-1.5 text-[0.7rem]">
        {d.gate.failed.map((f) => <span key={f} className="rounded-full border border-crit-line px-2 py-0.5 text-crit">failed: {f.replace(/_/g, " ")}</span>)}
        {d.gate.not_evaluated.map((f) => <span key={f} className="rounded-full border border-line px-2 py-0.5 text-faint">not evaluated: {f.replace(/_/g, " ")}</span>)}
        <span className="rounded-full border border-line px-2 py-0.5 text-muted">currently {d.effective.review_status.replace(/_/g, " ")}</span>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <Code title="Before the fix (vulnerable side, numbered lines)" text={r.code_before} numbered />
        <Code title="After the fix" text={r.code_after} />
      </div>
      {r.explanation && <details className="text-[0.78rem]"><summary className="cursor-pointer text-muted">Advisory text (shown to you only, never to a student)</summary>
        <p className="mt-1 whitespace-pre-wrap leading-snug text-muted">{r.explanation}</p></details>}
      {r.commit_message && <details className="text-[0.78rem]"><summary className="cursor-pointer text-muted">Fix commit message</summary>
        <p className="mt-1 whitespace-pre-wrap font-mono text-[0.72rem] text-muted">{r.commit_message}</p></details>}
      {d.decision && <details className="text-sm"><summary className="cursor-pointer text-muted">Read current saved review</summary>
        <p className="mt-2 text-xs text-muted">{String(d.decision.review_status || '').replace(/_/g, ' ')} | {d.decision.reviewer}</p>
        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify({
          label_state: d.decision.label_state,
          fields: Object.fromEntries(Object.entries(d.decision.fields || {}).filter(([key]) => key !== '_review_exchange')),
          notes: d.decision.notes,
        }, null, 2)}</pre>
      </details>}

      <div className="grid gap-3 md:grid-cols-2">
        <SelectField label="Your decision" hint="When in doubt between supported and insufficient context, choose insufficient context." value={label} onValueChange={setLabel}
          options={LABELS.map(([v, t]) => ({ value: v, label: t }))} />
        <Field label="Reviewer"><Input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="your name or id" /></Field>
        <Field label="Violated invariant" hint="Required for supported security supervision: what must always hold, and does not.">
          <Input value={invariant} onChange={(e) => setInvariant(e.target.value)} />
        </Field>
        {label === "vulnerability_supported" && (
          <Field label="Security-relevant lines (optional)" hint="Line numbers of the vulnerable side, for example 2-4, 9. They become the evidence the localization task learns from, so mark only the lines that make the code vulnerable.">
            <Input value={lines} onChange={(e) => setLines(e.target.value)} placeholder="2-4, 9" />
          </Field>
        )}
        {label === "insufficient_context" && (
          <Field label="What is missing (one per line)"><Textarea value={missing} onChange={(e) => setMissing(e.target.value)} /></Field>
        )}
        <Field label="Scope checked" hint="The specific condition and code boundary you reviewed."><Input value={security.condition_scope} onChange={(e) => change("condition_scope", e.target.value)} /></Field>
        {label === "vulnerability_supported" && <>
          <Field label="Attacker-controlled input and input path"><Textarea value={security.attacker_control} onChange={(e) => change("attacker_control", e.target.value)} /></Field>
          <Field label="Required preconditions (one per line)"><Textarea value={security.preconditions} onChange={(e) => change("preconditions", e.target.value)} /></Field>
          <Field label="Behavior preserved by the repair"><Textarea value={security.behavioral_intent} onChange={(e) => change("behavioral_intent", e.target.value)} /></Field>
        </>}
        {label === "no_supported_finding_in_scope" && <Field label="Effective safeguard or why the finding is unsupported"><Textarea value={security.decisive_safeguard} onChange={(e) => change("decisive_safeguard", e.target.value)} /></Field>}
        {label !== "insufficient_context" && <Field label="Exact source evidence" hint="Copy a decisive block from the before side. A changed line alone is not proof."><Textarea value={security.evidence} onChange={(e) => change("evidence", e.target.value)} /></Field>}
        {label === "vulnerability_supported" && <details className="space-y-3 text-sm md:col-span-2">
          <summary className="cursor-pointer text-muted">Review the fixed side separately (optional)</summary>
          <p className="text-xs text-muted">A fixed twin enters negative supervision only when its own scope, safeguard and source evidence are reviewed.</p>
          <Field label="Fixed-side review scope"><Input value={security.fixed_scope} onChange={(e) => change("fixed_scope", e.target.value)} /></Field>
          <Field label="Fixed-side effective safeguard"><Textarea value={security.fixed_safeguard} onChange={(e) => change("fixed_safeguard", e.target.value)} /></Field>
          <Field label="Exact fixed-side evidence"><Textarea value={security.fixed_evidence} onChange={(e) => change("fixed_evidence", e.target.value)} /></Field>
        </details>}
        <Field label="Notes"><Textarea value={notes} onChange={(e) => setNotes(e.target.value)} /></Field>
      </div>
      {err && <p className="font-mono text-[0.74rem] text-crit">{err}</p>}
      <Button variant="primary" loading={busy} disabled={!reviewer.trim() || (label === "insufficient_context" && !missing.trim())} onClick={submit}>Save decision</Button>
    </div>
  );
}

// "2-4, 9" -> [{start_line: 2, end_line: 4}, {start_line: 9, end_line: 9}], or the reason it cannot be read
function parseSpans(text: string): { start_line: number; end_line: number }[] | string {
  const out: { start_line: number; end_line: number }[] = [];
  for (const part of text.split(",").map((x) => x.trim()).filter(Boolean)) {
    const m = /^(\d+)(?:\s*-\s*(\d+))?$/.exec(part);
    if (!m) return `cannot read "${part}": write line numbers like 2-4, 9`;
    out.push({ start_line: Number(m[1]), end_line: Number(m[2] ?? m[1]) });
  }
  return out;
}

function Code({ title, text, numbered }: { title: string; text: string | null; numbered?: boolean }) {
  const body = !text ? "(no code)" : numbered
    ? text.split("\n").map((l, i, a) => `${String(i + 1).padStart(String(a.length).length, " ")}  ${l}`).join("\n")
    : text;
  return (
    <div>
      <div className="mb-1 text-[0.72rem] font-medium text-muted">{title}</div>
      <pre className="max-h-64 overflow-auto rounded-lg border border-line bg-terminal p-3 font-mono text-[0.72rem] leading-[1.5]">{body}</pre>
    </div>
  );
}

function Audit({ onOpen, onChanged }: { onOpen: (pairKey: string) => void; onChanged: () => void }) {
  const [n, setN] = useState("300");
  const [s, setS] = useState<AuditSample | null>(null);
  const [rates, setRates] = useState<ErrorRates | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => api.reviewErrorRates().then(setRates).catch(() => undefined);   // ignored: the rates are an extra; the queue still works
  useEffect(() => { load(); }, []);
  const draw = () => { setBusy(true); api.reviewAudit({ n: Number(n) || 50 }).then((x) => { setS(x); load(); onChanged(); }).finally(() => setBusy(false)); };
  const done = s ? s.items.filter((i) => i.decision).length : 0;

  return (
    <Panel title="Audit sample" desc="A stratified sample of candidates (ecosystem, link source, mechanism, synthetic explanation, extraction type, at most a few per project). Reviewing it independently of the checks estimates how often they are wrong."
      right={<div className="flex items-end gap-2"><div className="w-24"><Input inputMode="numeric" value={n} onChange={(e) => setN(e.target.value.replace(/\D/g, ""))} /></div>
        <Button onClick={draw} loading={busy}>Draw sample</Button></div>}>
      {rates && (
        <p className="text-[0.8rem]">
          {rates.status === "estimated"
            ? <>Gate error rate <b className="font-medium">{((rates.rate || 0) * 100).toFixed(1)}%</b> ({((rates.wilson_low || 0) * 100).toFixed(1)}-{((rates.wilson_high || 0) * 100).toFixed(1)}% interval) from {rates.n_reviewed} reviewed of {rates.n_sampled}.</>
            : <span className="text-muted">Not estimated yet: {rates.n_sampled} sampled, {rates.n_reviewed} reviewed by a person.</span>}
        </p>
      )}
      {rates && rates.failed_strata.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-[0.76rem] text-crit">
          {rates.failed_strata.map((f) => <li key={f.dimension + f.value}>Fails: {f.dimension} = {f.value} ({f.errors} of {f.n} wrong, lower bound {(f.wilson_low * 100).toFixed(0)}%) - correct the rule behind it.</li>)}
        </ul>
      )}
      {s && (
        <div className="mt-3">
          <p className="mb-1 text-[0.76rem] text-muted">{s.audit_id}: {done} of {s.items.length} reviewed.</p>
          <ul className="max-h-56 overflow-auto text-[0.74rem]">
            {s.items.map((i) => (
              <li key={i.record_id} className="flex items-center gap-3 border-t border-line py-1">
                <span className="font-mono text-faint">{i.pair_key.slice(-40)}</span>
                <span className="text-muted">{i.stratum.ecosystem} - {(i.stratum.mechanism || "").replace(/_/g, " ")}</span>
                <span className="ml-auto text-faint">{i.decision ? i.decision.label_state.replace(/_/g, " ") : <button className="underline" onClick={() => onOpen(i.pair_key)}>to review</button>}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Panel>
  );
}
