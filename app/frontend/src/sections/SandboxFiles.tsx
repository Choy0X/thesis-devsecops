import { useCallback, useEffect, useRef, useState } from "react";
import { errorText, sandbox, type SandboxChanges, type SandboxListing } from "../lib/api";
import { Panel, Button, Skeleton, ErrorState, EmptyState } from "../components/ui";
import { FileInput } from "../components/kit";
import { useToast } from "../components/Toast";
import { Icon } from "../components/Icon";
import { Dialog } from "../components/hud/Dialog";
import { Field, Input } from "../components/ui";
import { useConfirm } from "../components/hud/AlertDialog";

// The files of one container: browse a folder, take one file out, put one in, and see what the container changed against the image it started from. Paths are inside the container only; the
// server checks every one (absolute, no "..").

export const sizeWords = (bytes: number | null) => (bytes == null ? "" : bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`);
const when = (s: number | null) => (s ? new Date(s * 1000).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" }) : "");

/** The folders of a path as clickable steps: "/work/src" gives "/", "work", "src". */
export function crumbs(path: string): { label: string; path: string }[] {
  const parts = path.split("/").filter(Boolean);
  return [{ label: "/", path: "/" }, ...parts.map((p, i) => ({ label: p, path: "/" + parts.slice(0, i + 1).join("/") }))];
}

export function SandboxFiles({ cid }: { cid: string }) {
  const confirm = useConfirm();
  const [edit, setEdit] = useState<{ operation: string; source: string } | null>(null);
  const [destination, setDestination] = useState("");
  const [changing, setChanging] = useState(false);
  const [path, setPath] = useState("/work");
  const [listing, setListing] = useState<SandboxListing | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [problem, setProblem] = useState("");
  const [uploading, setUploading] = useState(false);
  const [waiting, setWaiting] = useState<File | null>(null);            // a file that would replace one already there, until the person says so
  const [changes, setChanges] = useState<SandboxChanges | null>(null);
  const [showChanges, setShowChanges] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const load = useCallback((p: string) => sandbox.files(cid, p).then((l) => { setListing(l); setError(null); setPath(l.path); }).catch(setError), [cid]);
  useEffect(() => { setListing(null); setChanges(null); setShowChanges(false); setWaiting(null); setProblem(""); load("/work"); }, [load]);

  const go = (p: string) => { setProblem(""); setWaiting(null); setListing(null); load(p); };

  const send = async (file: File, overwrite: boolean) => {
    setUploading(true); setProblem("");
    try {
      await sandbox.upload(cid, path, file, overwrite);
      toast.success(`${file.name} was put in ${path}.`);
      setWaiting(null);
      await load(path);
    } catch (e) {
      const text = errorText(e);
      if (!overwrite && /already in/.test(text)) setWaiting(file); else setProblem(text);
    } finally { setUploading(false); if (input.current) input.current.value = ""; }
  };

  const seeChanges = async () => {
    setShowChanges(true);
    try { setChanges(await sandbox.changes(cid)); } catch (e) { setProblem(errorText(e)); }
  };
  const applyChange = async () => {
    if (!edit) return;
    setChanging(true); setProblem("");
    try {
      const result = await sandbox.fileAction(cid, edit.operation, edit.operation === 'mkdir' ? destination : edit.source, edit.operation === 'move' ? destination : undefined);
      if (!result.confirmed) throw new Error('The file change was not confirmed.');
      setEdit(null); await load(path);
    } catch (e) { setProblem(errorText(e)); } finally { setChanging(false); }
  };
  const remove = async (target: string) => {
    if (!await confirm({ title: 'Delete this workspace entry?', description: `${target}. A folder and its contents are removed.`, tone: 'critical', confirmLabel: 'Delete entry' })) return;
    setChanging(true); setProblem("");
    try { const result = await sandbox.fileAction(cid, 'delete', target); if (!result.confirmed) throw new Error('Deletion was not confirmed.'); await load(path); } catch (e) { setProblem(errorText(e)); } finally { setChanging(false); }
  };

  return (
    <Panel title="Files" desc="Look inside the container, take one file out or put one in. Nothing here touches your computer's folders."
      right={<div className="flex gap-2"><Button disabled={changing || !path.startsWith('/work')} onClick={() => { setEdit({ operation: 'mkdir', source: path }); setDestination(path + '/new-folder'); }}>New folder</Button><Button variant="subtle" onClick={() => (showChanges ? setShowChanges(false) : seeChanges())}>{showChanges ? "Hide the changes" : "What changed"}</Button></div>}>
      <div className="space-y-4">
        <nav aria-label="Folder" className="flex flex-wrap items-center gap-1 font-mono text-[0.8rem]">
          {crumbs(path).map((c, i, all) => (
            <span key={c.path} className="flex items-center gap-1">
              {i === all.length - 1
                ? <span aria-current="page" className="font-semibold">{c.label}</span>
                : <button type="button" onClick={() => go(c.path)} className="text-brand-strong underline-offset-2 hover:underline">{c.label}</button>}
              {i > 0 && i < all.length - 1 ? <span className="text-faint">/</span> : null}
            </span>
          ))}
        </nav>

        {error != null && <ErrorState error={error} title={`${path} could not be listed`} onRetry={() => load(path)} />}
        {!listing && error == null && <Skeleton className="h-24" />}
        {listing && (listing.entries.length === 0 ? <EmptyState icon={<Icon.box />} title="This folder is empty" /> : (
          <table className="w-full text-[0.8rem]">
            <caption className="sr-only">Files and folders in {listing.path}</caption>
            <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wider text-faint"><th className="py-1.5 pr-3">Name</th><th className="pr-3 text-right">Size</th><th className="pr-3">Changed</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {listing.parent != null && (
                <tr className="border-b border-line/50"><td colSpan={4} className="py-1.5"><button type="button" onClick={() => go(listing.parent!)} className="font-mono text-brand-strong hover:underline">..</button></td></tr>
              )}
              {listing.entries.map((e) => (
                <tr key={e.name} className="border-b border-line/50">
                  <td className="py-1.5 pr-3 font-mono">
                    {e.kind === "folder"
                      ? <button type="button" onClick={() => go(`${listing.path === "/" ? "" : listing.path}/${e.name}`)} className="text-brand-strong hover:underline">{e.name}/</button>
                      : <span>{e.name}{e.kind === "link" && e.link ? <span className="text-faint"> to {e.link}</span> : null}</span>}
                  </td>
                  <td className="tnum pr-3 text-right text-muted">{sizeWords(e.size)}</td>
                  <td className="pr-3 text-muted">{when(e.modified)}</td>
                  <td className="text-right">
                    {e.kind === "file" && <a href={sandbox.downloadUrl(cid, `${listing.path === "/" ? "" : listing.path}/${e.name}`)} download={e.name} className="text-brand-strong hover:underline">Download</a>}
                    {listing.path.startsWith('/work') && <span className="ml-2 inline-flex gap-2">
                      <Button size="sm" disabled={changing} onClick={() => { const target = listing.path + '/' + e.name; setEdit({ operation: 'move', source: target }); setDestination(target); }}>Move / rename</Button>
                      <Button size="sm" disabled={changing} onClick={() => remove(listing.path + '/' + e.name)}>Delete</Button>
                    </span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
        {listing?.truncated && <p className="text-[0.76rem] text-warn">Only the first entries of this folder are listed.</p>}

        <div className="flex flex-wrap items-center gap-3 border-t border-line pt-3">
          <span className="flex items-center gap-2 text-[0.8rem] text-muted">Put a file in {path}
            <FileInput ref={input} disabled={uploading} aria-label="File to put in" buttonLabel="Choose a file" onChange={(e) => { const f = e.target.files?.[0]; if (f) send(f, false); }} />
          </span>
          <span className="text-[0.72rem] text-faint">One file at a time, up to 25 MB.</span>
        </div>
        {waiting && (
          <div role="alert" className="flex flex-wrap items-center gap-3 border border-warn-line bg-warn-tint px-3 py-2 text-[0.8rem] text-warn">
            <span>{waiting.name} is already in {path}.</span>
            <Button variant="subtle" loading={uploading} onClick={() => send(waiting, true)}>Replace it</Button>
            <Button variant="ghost" onClick={() => setWaiting(null)}>Keep the one there</Button>
          </div>
        )}
        {problem && <p role="alert" className="text-[0.8rem] text-crit-soft">{problem}</p>}

        {showChanges && (
          <div className="border-t border-line pt-3">
            <h3 className="mb-2 text-[0.82rem] font-medium">Changed since the container started</h3>
            {!changes ? <Skeleton className="h-12" /> : changes.total === 0 ? <p className="text-[0.8rem] text-muted">Nothing has changed against the image.</p> : (
              <>
                <p className="mb-2 text-[0.78rem] text-muted">{changes.counts.added} added, {changes.counts.changed} changed, {changes.counts.deleted} deleted{changes.truncated ? " (the first part of the list is shown)" : ""}.</p>
                <ul className="max-h-56 overflow-auto font-mono text-[0.76rem]" aria-label="Changed files">
                  {changes.changes.map((c) => <li key={c.path}><span className={c.kind === "deleted" ? "text-crit-soft" : c.kind === "added" ? "text-ok" : "text-warn"}>{c.kind}</span> {c.path}</li>)}
                </ul>
              </>
            )}
          </div>
        )}
      </div>
      <Dialog open={edit !== null} onOpenChange={(open) => { if (!open) setEdit(null); }} title={edit?.operation === 'mkdir' ? 'New workspace folder' : 'Move or rename workspace entry'}>
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); applyChange(); }}>
          {edit?.operation === 'move' && <p className="break-all font-mono text-xs">From: {edit.source}</p>}
          <Field label="Destination path" hint="An absolute path inside /work. Existing entries are preserved."><Input value={destination} onChange={(e) => setDestination(e.target.value)} autoFocus /></Field>
          {problem && <p role="alert" className="text-sm text-crit-soft">{problem}</p>}
          <Button type="submit" disabled={changing || !destination.trim()}>Apply file change</Button>
        </form>
      </Dialog>
    </Panel>
  );
}
