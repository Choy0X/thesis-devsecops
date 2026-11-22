import { useEffect, useState } from "react";
import { FileDiff, GitBranch, RefreshCw } from "lucide-react";
import { DiffView } from "./DiffView";
import { get } from "./client";
import { friendlyRunError } from "./runError";

type Checkout = { available: boolean; branch: string | null; revision: string | null };
type FileEntry = { path: string; status: string };
type Listing = Checkout & { files: FileEntry[]; truncated: boolean };
type FileDiffResult = Checkout & { diffs: { kind: "staged" | "unstaged" | "untracked"; diff: string; truncated: boolean }[]; message?: string };
const labels = { staged: "Staged", unstaged: "Unstaged", untracked: "Untracked" };

function fileStatus(status: string) {
  if (status === "??") return "Untracked";
  if (status.includes("U") || status === "AA" || status === "DD") return "Conflict";
  return [status[0] !== " " && "Staged", status[1] !== " " && "Unstaged"].filter(Boolean).join(" + ");
}

export function CheckoutChanges({ projectId }: { projectId: string }) {
  const endpoint = "/api/ide/projects/" + encodeURIComponent(projectId) + "/checkout";
  const [listing, setListing] = useState<Listing>();
  const [selection, setSelection] = useState("");
  const [file, setFile] = useState<FileDiffResult>();
  const [version, setVersion] = useState(0);
  const [failure, setFailure] = useState("");
  const [fileError, setFileError] = useState("");
  const refresh = () => setVersion(value => value + 1);
  useEffect(() => {
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, []);
  useEffect(() => {
    const pending = new AbortController();
    setFailure("");
    void get<Listing>(endpoint, pending.signal).then(result => {
      if (!pending.signal.aborted) setListing(result);
    }).catch(reason => { if (!pending.signal.aborted) setFailure(friendlyRunError(reason)); });
    return () => pending.abort();
  }, [endpoint, version]);
  const selected = listing?.files.find(row => row.path === selection) || listing?.files[0];
  const path = selected?.path;
  useEffect(() => {
    setFile(undefined);
    setFileError("");
    if (!path) return;
    const pending = new AbortController();
    void get<FileDiffResult>(endpoint + "?" + new URLSearchParams({ path }), pending.signal).then(result => {
      if (!pending.signal.aborted) setFile(result);
    }).catch(reason => { if (!pending.signal.aborted) setFileError(friendlyRunError(reason)); });
    return () => pending.abort();
  }, [endpoint, path, listing]);
  const branch = listing?.branch ?? (listing?.revision ? "Detached HEAD" : "No branch");
  const stale = file && (file.branch !== listing?.branch || file.revision !== listing?.revision);
  return <div className="checkout-view">
    <header className="checkout-heading">
      {listing?.available && <div className="checkout-branch" aria-label="Current branch">
        <GitBranch size={16} aria-hidden /><strong title={branch}>{branch}</strong>
        <code>{listing.revision?.slice(0, 8) ?? "No commits"}</code>
      </div>}
      <button className="ide-icon" type="button" onClick={refresh} aria-label="Refresh working tree" title="Refresh working tree"><RefreshCw size={16} aria-hidden /></button>
    </header>
    {failure && <p className="ide-error" role="alert">{failure}</p>}
    {!listing && !failure && <p role="status">Reading the working tree...</p>}
    {listing && !failure && (!listing.available
      ? <p className="checkout-empty">This project folder has no Git checkout.</p>
      : !listing.files.length ? <p className="checkout-empty">The working tree is clean.</p>
        : <div className="checkout-grid">
          <nav className="checkout-files" aria-label="Changed files">
            <p>{listing.files.length}{listing.truncated ? "+" : ""} changed {listing.files.length === 1 ? "file" : "files"}</p>
            {listing.files.map(row => <button type="button" key={row.path} aria-pressed={row.path === path} onClick={() => {
              if (path !== row.path) { setFile(undefined); setFileError(""); setSelection(row.path); }
            }}><FileDiff size={14} aria-hidden /><span><strong>{row.path}</strong><small>{fileStatus(row.status)}</small></span></button>)}
          </nav>
          <section className="checkout-diffs" aria-label={`Git diff for ${path}`}>
            <h3><code>{path}</code></h3>
            {fileError && <p className="ide-error" role="alert">{fileError}</p>}
            {!file && !fileError && <p role="status">Reading the diff...</p>}
            {stale && <p role="status">The branch changed during review. Refresh the working tree.</p>}
            {file && !stale && <>
              {file.message && <p>{file.message}</p>}
              {!file.message && !file.diffs.length && <p>This file has no pending changes. Refresh to update the list.</p>}
              {file.diffs.map(diff => <section key={diff.kind} aria-label={labels[diff.kind] + " diff"}>
                <h4>{labels[diff.kind]}</h4>
                {diff.diff ? <DiffView diff={diff.diff} /> : <p>Git has no text patch for this change.</p>}
                {diff.truncated && <p>The diff preview reached its display limit.</p>}
              </section>)}
            </>}
          </section>
        </div>)}
  </div>;
}
