import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Plus, Workflow } from "lucide-react";
import { Button, EmptyState, ErrorState, Field, Input, Panel, Skeleton } from "../components/ui";
import { refreshCampaigns } from "../lib/campaignsStore";
import { api, type CampaignListing } from "../lib/api";
import { SelectField } from "../components/kit";
import { useWorkspaceState } from "../lib/workspaceState";
import { CampaignDetail } from "../sections/campaigns/CampaignDetail";
import { CampaignList } from "../sections/campaigns/CampaignList";
import { InterruptedBanner } from "../sections/campaigns/InterruptedBanner";
import { StartCampaignDialog } from "../sections/campaigns/StartCampaignDialog";

// Campaigns: each one collects, mines and checks the advisories of the ecosystems it was started on, with its own progress. Several can run at once if the
// computer has the room; any can be paused, and a paused one frees its processor and memory. A campaign is opened with ?c=<id>; ?start=npm,PyPI opens the
// start dialog with those ecosystems chosen (the Collections page links here).

export function CampaignsPage() {
  const [params, setParams] = useSearchParams();
  const open = params.get("c");
  const [listing, setListing] = useState<CampaignListing | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [search, setSearch] = useWorkspaceState("campaigns.search", "");
  const [state, setState] = useWorkspaceState("campaigns.state", "");
  const [offset, setOffset] = useWorkspaceState("campaigns.offset", 0);
  const campaigns = listing?.campaigns ?? [];
  const total = listing?.total ?? campaigns.length;
  const loaded = !!listing;
  const load = () => api.campaigns({ limit: 100, offset, search, state }).then(value => { setListing(value); setError(null); }).catch(setError);
  useEffect(() => { if (!open) load(); }, [open, offset, search, state]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (open) return; const timer = window.setInterval(load, campaigns.some(c => ["running", "pausing"].includes(c.state)) ? 3000 : 15000); return () => window.clearInterval(timer); }, [open, offset, search, state, campaigns.some(c => ["running", "pausing"].includes(c.state))]); // eslint-disable-line react-hooks/exhaustive-deps
  const go = (id: string | null) => setParams((p) => { const next = new URLSearchParams(p); if (id) next.set("c", id); else next.delete("c"); return next; });

  const asked = params.get("start");
  const [starting, setStarting] = useState(asked !== null);
  const closeStart = () => { setStarting(false); if (asked !== null) setParams((p) => { const next = new URLSearchParams(p); next.delete("start"); return next; }, { replace: true }); };

  if (open) return <CampaignDetail id={open} onBack={() => go(null)} onDeleted={() => { go(null); refreshCampaigns(); }} />;
  return (
    <div className="space-y-5">
      <InterruptedBanner campaigns={campaigns} onOpen={go} />
      <Panel title="Collection jobs"
        right={<Button variant="primary" icon={<Plus size={14} aria-hidden />} onClick={() => setStarting(true)}>Start a campaign</Button>}>
        <div className="mb-4 grid gap-3 sm:grid-cols-[minmax(0,1fr)_13rem]">
          <Field label="Search campaigns"><Input value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} /></Field>
          <SelectField label="State" value={state} onValueChange={value => { setState(value); setOffset(0); }} options={[{ value: "", label: "All states" }, ...["running", "paused", "interrupted", "failed", "done"].map(value => ({ value, label: value }))]} />
        </div>
        {error != null && !campaigns.length ? <ErrorState error={error} title="Could not read the campaigns" onRetry={load} />
          : !loaded ? <Skeleton className="h-40 w-full" />
          : campaigns.length === 0 ? <EmptyState icon={<Workflow size={20} aria-hidden />}
            title={search || state ? "No matching campaigns" : "No campaigns yet"}
            hint={search || state ? "Clear the filters to see other campaigns." : "Start a campaign to collect advisories and their fix commits."} />
          : <CampaignList campaigns={campaigns} onOpen={go} onChanged={load} />}
      </Panel>
      {(offset > 0 || total > 100) && <div className="flex flex-wrap items-center gap-3 text-[0.78rem] text-muted"><Button variant="ghost" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 100))}>Previous</Button><span>{total ? `${offset + 1}-${Math.min(offset + 100, total)} of ${total}` : "0 campaigns"}</span><Button variant="ghost" disabled={offset + 100 >= total} onClick={() => setOffset(offset + 100)}>Next</Button></div>}
      <StartCampaignDialog open={starting} onClose={closeStart} initialEcosystems={(asked ?? "").split(",").map((e) => e.trim()).filter(Boolean)}
        onStarted={(id) => { setStarting(false); setParams((p) => { const next = new URLSearchParams(p); next.delete("start"); next.set("c", id); return next; }); }} />
    </div>
  );
}
