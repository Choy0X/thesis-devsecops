import { useEffect, useState, type ReactNode } from "react";
import { api, errorText, type CodeSummary, type MiningCheckpoint } from "../lib/api";
import { Button, Field, Input } from "../components/ui";
import { Checkbox, Hint } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { useConfirm } from "../components/hud/AlertDialog";
import { useToast } from "../components/Toast";
import { num } from "../lib/format";

// The steps that turn the dataset records into code pairs, in the order they are run. Every step can be run again: it picks up what is new and leaves
// the rest alone. The mining step keeps its own checkpoint (resume or dismiss an interrupted run).

const Now = ({ children }: { children: string }) => <p className="-mt-1 mb-3 text-[0.74rem] text-faint">{children}</p>;
const Check = ({ checked, onChange, children, title }: { checked: boolean; onChange: (v: boolean) => void; children: string; title?: string }) => (
  <Checkbox className="mb-2" checked={checked} onCheckedChange={onChange} label={children} title={title} />
);

export function MiningSteps({ sum, onDone }: { sum: CodeSummary | null; onDone: () => void }) {
  const f = sum?.fix_commits, p = sum?.pairs;
  const [onlyUnchecked, setOnlyUnchecked] = useState(true);
  const [maxFunctions, setMaxFunctions] = useState("20");
  const [refreshDeps, setRefreshDeps] = useState(false);
  const [limit, setLimit] = useState("");

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Step n={1} title="Extract fix commits"
        desc="Collect the fix-commit links of the dataset records: the advisory's git ranges and fix references, and the patch references of its CVE in the NVD. Only records are looked at; nothing else is queued.">
        <Now>{f ? `${num(f.total)} fix commits known, ${num(f.by_status.pending || 0)} waiting to be mined.` : ""}</Now>
        <RunConsole stage="extract_fixes" params={{ nvd: true }} label="Extract" onDone={onDone} hint="Safe to run again; new commits are queued." />
      </Step>

      <Step n={2} title="Recover missing fix links"
        desc="For advisories that name no commit, follow their pull requests, closing commits and fixed-version ranges to a fix commit, and queue what is found. Needs a GitHub token (Settings) and can take a while.">
        <Now>{f ? `${num(f.by_status.unlinked || 0)} advisories have no fix commit yet.` : ""}</Now>
        <div className="mb-3 max-w-[14rem]"><Field label="Try at most (blank for all)"><Input type="number" min={1} value={limit} onChange={(e) => setLimit(e.target.value)} /></Field></div>
        <RunConsole stage="recover_fixes" label="Recover" params={limit ? { limit: Number(limit) } : {}} onDone={onDone}
          confirmText="This follows links through the GitHub API with your token and uses part of its hourly allowance. Start it?"
          hint="Uses the GitHub token from Settings." />
      </Step>

      <MineStep n={3} sum={sum} onDone={onDone} />

      <Step n={4} title="Check the fixes against release dates"
        desc="Each mined fix commit is compared with the package's release dates. A commit that cannot be the fix, for example one made after the fixed release, is set aside so it never reaches the dataset.">
        <Now>{p ? `${num(p.usable)} usable pairs at the moment.` : ""}</Now>
        <Check checked={onlyUnchecked} onChange={setOnlyUnchecked}>Only the commits that were not checked yet</Check>
        <RunConsole stage="verify_fixes" label="Check" params={{ only_unchecked: onlyUnchecked }} onDone={onDone}
          confirmText={onlyUnchecked ? undefined : "This checks every mined fix commit again. It can take a long time. Start it?"}
          hint="Needs no re-download of any code." />
      </Step>

      <Step n={5} title="Split files into functions"
        desc="Turns the stored file-level pairs into function-level and region-level pairs, so a pair is the changed function and not a whole file. Nothing is mined again.">
        <Now>{p ? `${num(p.functions)} function pairs and ${num(p.by_granularity?.hunk || 0)} region pairs so far.` : ""}</Now>
        <div className="mb-3 max-w-[14rem]"><Field label="Most functions per file"><Input type="number" min={1} value={maxFunctions} onChange={(e) => setMaxFunctions(e.target.value)} /></Field></div>
        <RunConsole stage="resplit_pairs" label="Split" params={{ max_functions: Number(maxFunctions) || 20 }} onDone={onDone}
          confirmText="Pairs are rebuilt from the stored files; pairs you edited by hand are kept. Start it?" hint="Runs on the stored code only." />
      </Step>

      <Step n={6} title="Collect dependency versions"
        desc="For the records whose fix was mined: the affected versions, the first fixed release and the newest safe release, read from the package registry. Records checked in the last 7 days are skipped.">
        <Check checked={refreshDeps} onChange={setRefreshDeps} title="Marks every record as not checked first, so all are asked again.">Check every record again, even those checked recently</Check>
        <RunConsole stage="dep_versions" label="Collect" params={{ only_mined: true, refresh: refreshDeps }} onDone={onDone}
          confirmText={refreshDeps ? "Every record will be asked of the package registries again. Start it?" : undefined}
          hint="Asks the package registries only." />
      </Step>
    </div>
  );
}

// ── the pieces the steps are made of ────────────────────────────────────────

export function Step({ n, title, desc, children }: { n: number; title: string; desc: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-line bg-surface-2/40 p-4">
      <div className="mb-1 flex items-center gap-2">
        <span className="grid h-5 w-5 place-items-center rounded-full border border-line-strong text-[0.66rem] tnum text-muted">{n}</span>
        <span className="text-[0.86rem] font-medium">{title}</span>
      </div>
      <p className="mb-3 text-[0.76rem] text-muted leading-snug">{desc}</p>
      {children}
    </div>
  );
}


export function MineStep({ onDone, sum, n = 2 }: { onDone: () => void; sum: CodeSummary | null; n?: number }) {
  const [batch, setBatch] = useState("200");
  const [pause, setPause] = useState("60");
  const [workers, setWorkers] = useState("4");
  const [all, setAll] = useState(true);
  const [retry, setRetry] = useState(false);
  const [timeoutS, setTimeoutS] = useState("600");
  const [deps, setDeps] = useState(true);
  const pending = sum?.fix_commits.by_status.pending || 0;
  const toast = useToast();
  const [cp, setCp] = useState<MiningCheckpoint | null>(null);
  const [consoleKey, setConsoleKey] = useState(0);
  const loadCp = () => api.miningCheckpoint().then(setCp).catch(() => setCp(null));
  useEffect(() => { loadCp(); }, [sum]);
  const resume = async () => {
    try { await api.miningResume(); setConsoleKey((k) => k + 1); toast.success("Mining resumed with the interrupted run's settings."); loadCp(); }
    catch (e: any) { toast.error(`Resume failed: ${String(e?.message || e).replace(/^\d+ /, "")}`); }
  };
  const discard = async () => {
    try { await api.miningDiscard(); } catch (e) { toast.error(`Discard failed: ${errorText(e)}`); }
    loadCp();
  };
  const stopOrphan = async () => {
    try { const r = await api.miningStopOrphan(); toast.success(`Stopped the mining process ${r.pid}.`); loadCp(); }
    catch (e: any) { toast.error(String(e?.message || e).replace(/^\d+ /, "")); }
  };
  const run = cp?.run;
  return (
    <Step n={n} title="Mine code"
      desc="Fetch each record's fix commit and its parent (GitHub API with a token, otherwise git), detect the CVE's language from the repository and the changed files, and store before/after code + diff in that language only. Runs in batches and waits out GitHub rate limits.">
      <p className="-mt-1 mb-3 text-[0.74rem] leading-snug text-faint">
        Each fix commit is checked against the package's release dates (a commit that cannot be the fix is set aside) and every
        changed file is split into its changed functions or regions. Both run automatically during Mine; code mined earlier is
        brought up to date after Mine and before Build. Nothing is downloaded again.
      </p>
      <div className="mb-3 grid grid-cols-3 gap-2">
        <Field label="Batch size (commits)"><Input type="number" min={1} value={batch} onChange={(e) => setBatch(e.target.value)} /></Field>
        <Field label="Pause between batches (s)"><Input type="number" min={0} value={pause} onChange={(e) => setPause(e.target.value)} /></Field>
        <Field label="Workers"><Input type="number" min={1} max={16} value={workers} onChange={(e) => setWorkers(e.target.value)} /></Field>
      </div>
      <Checkbox className="mb-1.5" checked={all} onCheckedChange={setAll} label="Keep going until every queued commit is mined" />
      <Checkbox className="mb-1.5" checked={deps} onCheckedChange={setDeps} label="Then collect dependency versions of the mined records (affected → first fixed / latest safe release)"
        title="Runs after mining and the fix-commit check, only for the records whose code was mined: affected ranges, first fixed release and the newest safe release from the package registry. Records checked in the last 7 days are skipped." />
      <div className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-2">
        <Checkbox checked={retry} onCheckedChange={setRetry} label={`Retry previously failed commits${cp?.failed ? ` (${num(cp.failed)})` : ""}`} />
        <label className="flex items-center gap-2 text-[0.78rem] text-muted">
          <Hint label="A commit taking longer is marked failed and the batch moves on" className="no-underline">Give up on a commit after</Hint>
          <Input type="number" min={30} value={timeoutS} onChange={(e) => setTimeoutS(e.target.value)} className="w-24 min-h-8" inputClassName="h-8 tnum" suffix="s" />
        </label>
      </div>
      {cp?.interrupted && run && (
        <div className="mb-3 rounded-lg border border-warn-line bg-warn-tint p-3 text-[0.78rem]">
          <div className="font-medium text-warn">Mining was interrupted</div>
          <div className="mt-0.5 text-muted">
            The run started {new Date(run.started).toLocaleString()} stopped without finishing (app closed or crashed) after
            {" "}{num(run.processed)} of {num(run.total)} commits; last sign of life {new Date(run.heartbeat).toLocaleTimeString()}.
            {" "}{num(cp.pending)} commits are still queued. Nothing is lost: each commit is saved as soon as it is mined.
          </div>
          <div className="mt-2 flex gap-2">
            <Button variant="primary" onClick={resume}>Resume</Button>
            <Button variant="ghost" onClick={discard}>Dismiss</Button>
          </div>
        </div>
      )}
      {cp?.active && !cp.active_here && (
        <div className="mb-3 rounded-lg border border-crit-line bg-crit-tint p-3 text-[0.78rem]">
          <div className="font-medium text-crit-soft">Another mining process is still running</div>
          <div className="mt-0.5 text-muted">Process {run?.pid} (started {run && new Date(run.started).toLocaleString()}) was left by an earlier session. A new run is refused until it stops.</div>
          <div className="mt-2"><Button variant="ghost" onClick={stopOrphan}>Stop it</Button></div>
        </div>
      )}
      <RunConsole key={consoleKey} stage="mine_code" label="Mine"
        params={{ batch_size: Number(batch) || 200, pause_seconds: Number(pause) || 0,
          workers: Number(workers) || 4, until_done: all, retry_failed: retry, commit_timeout: Number(timeoutS) || 600,
          collect_deps: deps }}
        onDone={() => { onDone(); loadCp(); }}
        hint={`Resumable; stop any time.${pending ? ` ${num(pending)} commit${pending === 1 ? "" : "s"} queued.` : ""}`} />
    </Step>
  );
}


export function ResetControls({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const [keep, setKeep] = useState(true);
  const [repos, setRepos] = useState(false);
  const [cp, setCp] = useState<MiningCheckpoint | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.miningCheckpoint().then(setCp).catch(() => setCp(null)); }, []);
  const gb = (b: number) => b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`;

  const confirm = useConfirm();
  const go = async (scope: "mining" | "all") => {
    const what = scope === "mining"
      ? "delete all mined code pairs and put every fix commit back in the queue (mine again from scratch)"
      : "delete all mined code pairs AND the fix-commit queue (run Extract again)";
    const sure = await confirm({ title: scope === "mining" ? "Mine again from scratch?" : "Remove the mined pairs and the queue?", tone: "critical", typeToConfirm: "RESET",
      description: `This will ${what}. Source records are kept${keep ? ", and so are hand-made and edited pairs" : ""}.`,
      confirmLabel: scope === "mining" ? "Delete pairs and re-queue" : "Delete pairs and queue" });
    if (!sure) return;
    setBusy(true);
    try {
      const r = await api.resetMined({ scope, keep_manual: keep, delete_repos: repos });
      toast.success(`Cleared: ${r.pairs_deleted} pairs, ${r.commits_requeued ?? r.commits_deleted} commits ${scope === "mining" ? "re-queued" : "removed"}`
        + (r.repos_freed_bytes ? `, ${gb(Number(r.repos_freed_bytes))} of git cache freed` : "") + ".");
      onDone();
      api.miningCheckpoint().then(setCp).catch(() => undefined);   // ignored: the checkpoint panel keeps its last state
    } catch (e: any) { toast.error(`Reset failed: ${String(e?.message || e).replace(/^\d+ /, "")}`); }
    finally { setBusy(false); }
  };

  return (
    <div className="space-y-3">
      <p className="text-[0.76rem] leading-snug text-muted">An interrupted run does not need a reset: it resumes from the queue. Reset only to start the
        collection again. Source records are never deleted.</p>
      <Checkbox checked={keep} onCheckedChange={setKeep} label="Keep hand-made and hand-edited code pairs" />
      <Checkbox checked={repos} onCheckedChange={setRepos} label={`Also delete the cached git repositories${cp ? ` (${cp.repos_bytes == null ? "measuring size" : gb(cp.repos_bytes)})` : ""}`} />
      <div className="flex flex-wrap gap-2">
        <Button variant="ghost" loading={busy} onClick={() => go("mining")}>Clear mined code (re-mine from scratch)</Button>
        <Button variant="ghost" loading={busy} onClick={() => go("all")}>Clear mined code and fix-commit queue</Button>
      </div>
    </div>
  );
}

