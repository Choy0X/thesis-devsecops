import { TablePager } from "../components/TablePager";
import { useWorkspaceState } from "../lib/workspaceState";
import { useCallback, useEffect, useState } from "react";
import { errorText, sandbox, type SandboxImages, type SandboxPrunePlan } from "../lib/api";
import { Panel, Button, Field, Input, Textarea, Skeleton, ErrorState, EmptyState } from "../components/ui";
import { Checkbox, Hint } from "../components/kit";
import { RunConsole } from "../components/RunConsole";
import { useConfirm } from "../components/hud/AlertDialog";
import { useToast } from "../components/Toast";
import { Icon } from "../components/Icon";
import { useWhileActive } from "../components/ActivePage";

// The images on this computer: what each is (digest, size, age, who uses it), which one verification is pinned to, building one from a Dockerfile as a job, and removing only the unused ones this
// app made. Nothing is pulled or built until the person asks.

const short = (digest: string) => (digest.length > 19 ? `${digest.slice(0, 19)}...` : digest);
export const TEMPLATE = "FROM python:3.11-slim\n# Add the tools you want here. The steps have no network unless you turn it on below, so a pip install needs it.\n# RUN pip install --no-cache-dir bandit\n";

export function SandboxImagesTab() {
  const [offset, setOffset] = useWorkspaceState("sandbox.images.offset", 0);
  const [data, setData] = useState<SandboxImages | null>(null);
  const [error, setError] = useState<unknown>(null);
  const load = useCallback(() => sandbox.imageList({ limit: 100, offset }).then((d) => { setData(d); setError(null); }).catch(setError), [offset]);
  useEffect(() => { load(); }, [load]);
  useWhileActive(load, 15000);

  if (error != null && !data) return <Panel title="Images"><ErrorState error={error} title="The images could not be listed" onRetry={load} /></Panel>;
  if (!data) return <Panel title="Images"><Skeleton className="h-32" /></Panel>;
  if (!data.docker_available) {
    return <Panel title="Images"><EmptyState icon={<Icon.box />} title="Docker is not available" hint="The Docker daemon is not reachable on this machine. Start Docker to see and build images." /></Panel>;
  }
  return (
    <div className="space-y-5">
      <ImageList data={data} reload={load} />
      <TablePager offset={offset} total={data.total ?? data.images.length} onOffset={setOffset} />
      <BuildImage onBuilt={load} />
      <Prune onPruned={load} />
    </div>
  );
}

function ImageList({ data, reload }: { data: SandboxImages; reload: () => void }) {
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const toast = useToast();
  const pin = async (tag: string) => {
    setBusy(tag); setProblem("");
    try { await sandbox.pin(tag); toast.success(`Verification now runs on ${tag}.`); reload(); } catch (e) { setProblem(errorText(e)); } finally { setBusy(null); }
  };
  const unpin = async () => {
    setBusy("unpin"); setProblem("");
    try { await sandbox.unpin(); toast.success("The pin was removed."); reload(); } catch (e) { setProblem(errorText(e)); } finally { setBusy(null); }
  };
  return (
    <Panel title="Images on this computer" desc="The digest is what Docker records for the image; two images with the same tag can differ.">
      <div className="space-y-4">
        {data.pinned
          ? <p role="status" className="border border-line bg-surface-2 px-3 py-2 text-[0.8rem]">Verification is pinned to <span className="font-mono">{data.pinned.tag}</span> <span className="font-mono text-faint">({short(data.pinned.digest)})</span>. If that tag later points to a different image, verification refuses to run until you pull the pinned one again or change the pin.
              <Button variant="ghost" className="ml-2" loading={busy === "unpin"} onClick={unpin}>Remove the pin</Button></p>
          : <p className="text-[0.8rem] text-muted">No image is pinned: verification uses whichever image carries the tag at the time. Pin one so every receipt names the same image.</p>}
        {data.images.length === 0 ? <EmptyState icon={<Icon.box />} title="No images yet" hint="An image appears here after a sandbox is created or an image is built." /> : (
          <table className="w-full text-[0.8rem]">
            <caption className="sr-only">Images on this computer</caption>
            <thead><tr className="border-b border-line text-left text-[0.72rem] uppercase tracking-wider text-faint"><th className="py-1.5 pr-3">Image</th><th className="pr-3">Digest</th><th className="pr-3 text-right">Size</th><th className="pr-3">Made</th><th className="pr-3 text-right">Used by</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {data.images.map((im) => (
                <tr key={im.full_id} className="border-b border-line/50 align-top">
                  <td className="py-1.5 pr-3 font-mono">
                    {im.tags.length ? im.tags.map((t) => <div key={t}>{t}</div>) : <span className="text-faint">untagged ({im.id})</span>}
                    <div className="mt-0.5 flex gap-1.5 font-sans text-[0.68rem]">
                      {im.pinned && <span className="border border-brand-line bg-brand-tint px-1.5 text-brand-strong">pinned</span>}
                      {im.toolkit && <Hint label="No container keeps this one running between scans, so it is never offered for clean-up" className="border border-line bg-surface-2 px-1.5 text-muted no-underline">used by Scan Codebase</Hint>}
                      {im.ours && <span className="border border-line bg-surface-2 px-1.5 text-muted">made by this app</span>}
                    </div>
                  </td>
                  <td className="pr-3 font-mono text-muted" title={im.digest}>{short(im.digest)}</td>
                  <td className="tnum pr-3 text-right text-muted">{im.size_mb} MB</td>
                  <td className="pr-3 text-muted">{im.created.slice(0, 10)}</td>
                  <td className="tnum pr-3 text-right text-muted">{im.containers}</td>
                  <td className="text-right">{im.tags.length > 0 && !im.pinned && <Button variant="ghost" loading={busy === im.tags[0]} onClick={() => pin(im.tags[0])}>Pin</Button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {problem && <p role="alert" className="text-[0.8rem] text-crit-soft">{problem}</p>}
      </div>
    </Panel>
  );
}

function BuildImage({ onBuilt }: { onBuilt: () => void }) {
  const [tag, setTag] = useWorkspaceState("sandbox.image.tag", "aivdf/tools:1");
  const [dockerfile, setDockerfile] = useWorkspaceState("sandbox.image.dockerfile", TEMPLATE);
  const [network, setNetwork] = useWorkspaceState("sandbox.image.network", false);
  const ready = tag.trim() !== "" && dockerfile.trim() !== "";
  return (
    <Panel title="Build an image" desc="From a Dockerfile you write. The build runs as a job with its log; the image is labelled as this app's, so it can be removed here later.">
      <div className="space-y-3">
        <Field label="Tag" hint="Lower-case letters, digits and . _ / - with an optional :version.">
          <Input value={tag} onChange={(e) => setTag(e.target.value)} spellCheck={false} className="font-mono" />
        </Field>
        <Field label="Dockerfile">
          <Textarea value={dockerfile} onChange={(e) => setDockerfile(e.target.value)} spellCheck={false} rows={8} className="font-mono text-[0.78rem]" />
        </Field>
        <Checkbox checked={network} onCheckedChange={setNetwork} label="Let the build steps use the network" />
        {network && <p role="alert" className="border border-warn-line bg-warn-tint px-3 py-2 text-[0.78rem] text-warn">The build steps can then reach the internet (to install packages, for example). Build only a Dockerfile you wrote or have read.</p>}
        <RunConsole stage="sandbox_build_image" label="Build the image" disabled={!ready} params={{ dockerfile, tag: tag.trim(), network }} onDone={onBuilt}
          launch={(p) => sandbox.build({ dockerfile: p.dockerfile, tag: p.tag, network: !!p.network })} />
      </div>
    </Panel>
  );
}

function Prune({ onPruned }: { onPruned: () => void }) {
  const [plan, setPlan] = useState<SandboxPrunePlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const confirm = useConfirm();
  const toast = useToast();
  const look = async () => {
    setBusy(true); setProblem("");
    try { setPlan(await sandbox.prunePlan()); } catch (e) { setProblem(errorText(e)); } finally { setBusy(false); }
  };
  const remove = async () => {
    if (!plan) return;
    const ok = await confirm({
      title: "Remove these images?", tone: "critical", confirmLabel: `Remove ${plan.images.length} ${plan.images.length === 1 ? "image" : "images"}`,
      description: `${plan.images.length} unused ${plan.images.length === 1 ? "image" : "images"} (${plan.size_mb} MB) will be removed from this computer. They can be built or pulled again; containers and the pinned image are not touched.`,
    });
    if (!ok) return;
    setBusy(true); setProblem("");
    try {
      const r = await sandbox.prune(plan.images.map((i) => i.full_id));
      toast.success(`${r.removed.length} removed, ${r.freed_mb} MB freed.${r.skipped.length ? ` ${r.skipped.length} could not be removed.` : ""}`);
      setPlan(null); onPruned();
    } catch (e) { setProblem(errorText(e)); } finally { setBusy(false); }
  };
  return (
    <Panel title="Clean up" desc="Only images this app made (or that a recreated container left), that no container uses and that are not pinned. Neither the tool image of Scan Codebase nor an image of another project, tagged or not, is ever offered.">
      <div className="space-y-3">
        {!plan && <Button loading={busy} onClick={look}>Find unused images</Button>}
        {plan && (plan.images.length === 0 ? <p className="text-[0.8rem] text-muted">Nothing to remove.</p> : (
          <>
            <ul aria-label="Images that would be removed" className="font-mono text-[0.78rem]">
              {plan.images.map((i) => <li key={i.full_id}>{i.tags[0] ?? `untagged (${i.id})`} <span className="text-faint">{i.size_mb} MB</span></li>)}
            </ul>
            <div className="flex gap-2">
              <Button className="text-crit hover:border-crit" loading={busy} onClick={remove}>Remove {plan.images.length} ({plan.size_mb} MB)</Button>
              <Button variant="ghost" onClick={() => setPlan(null)}>Cancel</Button>
            </div>
          </>
        ))}
        {problem && <p role="alert" className="text-[0.8rem] text-crit-soft">{problem}</p>}
      </div>
    </Panel>
  );
}
