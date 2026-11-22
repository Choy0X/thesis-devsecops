import { useEffect, useRef, useState } from "react";
import { useJob } from "../../lib/useJob";
import { Checkbox, IdeSelect } from "./Controls";
import { DiffView } from "./DiffView";
import { endpoint, get, write } from "./client";
import { friendlyRunError } from "./runError";
import type { PatchCandidate, PatchIndex } from "./workbenchTypes";

function download(candidate: PatchCandidate) {
  if (!candidate.diff) return;
  const link = document.createElement("a");
  const url = URL.createObjectURL(new Blob([candidate.diff], { type: "text/plain" }));
  link.href = url;
  link.download = candidate.candidate_id + ".diff";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function PatchReview({ projectId, chatId, findingId, busy, onStaged, onClearFinding }: {
  projectId: string; chatId: string; findingId?: string; busy: boolean; onStaged: () => void; onClearFinding: () => void;
}) {
  const base = endpoint(chatId) + "/patches";
  const [index, setIndex] = useState<PatchIndex>();
  const [selected, setSelected] = useState("");
  const [candidate, setCandidate] = useState<PatchCandidate>();
  const [version, refresh] = useState(0);
  const [failure, setFailure] = useState("");
  const [working, setWorking] = useState(false);
  const [authorize, setAuthorize] = useState(false);
  const [runChecks, setRunChecks] = useState(false);
  const [link, setLink] = useState("");
  const job = useJob();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const controller = new AbortController();
    void get<PatchIndex>(base, controller.signal).then(result => { if (!controller.signal.aborted) setIndex(result); })
      .catch(reason => { if (!controller.signal.aborted) setFailure(friendlyRunError(reason)); });
    return () => controller.abort();
  }, [base, version]);
  useEffect(() => { if (job.status === "done") refresh(value => value + 1); }, [job.status]);

  const rows = index?.candidates || [];
  const id = rows.some(row => row.candidate_id === selected) ? selected : rows[0]?.candidate_id;
  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    void get<PatchCandidate>(base + "/" + encodeURIComponent(id), controller.signal).then(result => { if (!controller.signal.aborted) setCandidate(result); })
      .catch(reason => { if (!controller.signal.aborted) setFailure(friendlyRunError(reason)); });
    return () => controller.abort();
  }, [base, id, version]);

  const perform = async (operation: () => Promise<unknown>) => {
    setWorking(true); setFailure("");
    try { await operation(); } catch (reason) { if (alive.current) setFailure(friendlyRunError(reason)); }
    finally { if (alive.current) setWorking(false); }
  };
  const projectEndpoint = "/api/ide/projects/" + encodeURIComponent(projectId);
  const unavailable = busy || working || job.status === "running";
  const review = candidate?.candidate_id === id ? candidate : undefined;
  const linkable = index?.linkableProjects || [];
  return <section className="chat-proposals" aria-label="Patch proposals">
    <h3>Patch proposals</h3>
    {failure && <div className="ide-toolbar" role="alert"><p className="ide-error">{failure}</p><button onClick={() => { setFailure(""); refresh(value => value + 1); }}>Retry proposals</button></div>}
    {!index && !failure && <p role="status">Loading proposals...</p>}
    {findingId && index && (index.legacyProjectId ? <div className="chat-patch-setup">
      <Checkbox label="Run configured checks in the sandbox" checked={runChecks} onCheckedChange={setRunChecks} disabled={unavailable} />
      <div className="ide-toolbar"><button className="ide-primary" disabled={unavailable} onClick={() => {
        const input = { finding_id: findingId, run_checks: runChecks };
        onClearFinding();
        void job.run("propose_project_patch", input, params => write<{ id: string }>(base, params));
      }}>Generate candidate</button><button onClick={onClearFinding}>Cancel</button></div>
    </div> : <div className="chat-patch-setup">
      {linkable.length > 1 ? <IdeSelect aria-label="Candidate source project" value={link} onChange={event => setLink(event.target.value)}>
        <option value="" disabled>Select an authorized source project</option>{linkable.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}
      </IdeSelect> : <Checkbox label="I own this code or have permission to patch it" checked={authorize} onCheckedChange={setAuthorize} />}
      <button disabled={unavailable || (linkable.length > 1 ? !link : !authorize)} onClick={() => void perform(async () => {
        if (linkable.length > 1) await write(projectEndpoint, { legacy_project_id: link }, "PATCH");
        else await write(projectEndpoint + "/patch-link", { authorized: true });
        if (alive.current) refresh(value => value + 1);
      })}>Enable patch proposals</button>
    </div>)}
    {job.status !== "idle" && <div className="ide-toolbar">
      <p role={job.error ? "alert" : "status"}>{job.error || (job.status === "running" ? "Drafting patch..." : job.status === "done" ? "Proposal ready for review." : "Proposal " + job.status)}</p>
      {job.status === "running" && job.id && <button onClick={() => void job.cancel()}>Cancel proposal</button>}
    </div>}
    {index && !rows.length && !findingId && job.status === "idle" && <p className="ide-rail-hint">Propose a patch from a supported v40 finding in the conversation.</p>}
    {!!rows.length && <div className="chat-change-list">{rows.map(row => <button key={row.candidate_id} aria-pressed={id === row.candidate_id} className={id === row.candidate_id ? "is-selected" : ""} onClick={() => { setFailure(""); setSelected(row.candidate_id); }}>
      <span>Proposal</span><code>{row.file || row.rule || row.candidate_id}</code>
    </button>)}</div>}
    {id && !review && !failure && <p role="status">Loading diff...</p>}
    {review && <section className="chat-diff" aria-label="Proposed patch">
      {review.finding?.message && <p>{review.finding.message}</p>}
      {review.diff ? <DiffView diff={review.diff} /> : <p>No diff was saved for this proposal.</p>}
      <details><summary>Verification evidence</summary>
        <p>{review.tests?.words || review.words}</p>
        <ul>{Object.entries(review.steps || {}).map(([step, result]) => <li key={step}><strong>{step.replace(/_/g, " ")}</strong> {result.status.replace(/_/g, " ")}{result.detail && <p>{result.detail}</p>}</li>)}</ul>
      </details>
      {review.stage_reason && !review.staged && <p className="ide-rail-hint">{review.stage_reason}</p>}
      <div className="ide-toolbar">
        {review.diff && <button onClick={() => download(review)}>Export diff</button>}
        {review.staged ? <span className="text-ok">Staged in workspace</span> : review.can_stage && review.diff_sha256 && <button className="ide-primary" disabled={unavailable} onClick={() => void perform(async () => {
          await write(base + "/" + encodeURIComponent(review.candidate_id) + "/stage", { expected_diff_sha256: review.diff_sha256 });
          if (alive.current) { onStaged(); refresh(value => value + 1); }
        })}>Stage patch</button>}
      </div>
    </section>}
  </section>;
}
