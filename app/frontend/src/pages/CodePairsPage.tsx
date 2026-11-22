import { useEffect, useState } from "react";
import { Navigate, useSearchParams } from 'react-router-dom';
import { api, type CodeSummary } from "../lib/api";
import { Button, Panel } from "../components/ui";
import { Dialog } from "../components/hud/Dialog";
import { Tabs } from "../components/hud/Tabs";
import { useDataSync, useDataVersion } from "../lib/dataSync";
import { useTabParam } from "../lib/tabParam";
import { Stats } from "../sections/PairStats";
import { ResetControls } from "../sections/MiningSteps";
import { CampaignContextItems } from "../sections/CampaignContextItems";
import { usePageRequest } from "../components/PageScope";
import { PairsBrowser } from "../sections/PairsBrowser";

// Source inspection and optional context improvements. Builds automatically
// assess evidence; navigation never starts collection or generation.

const TABS = ["pairs", "context", "maintenance"] as const;

export function CodePairsPage() {
  const request = usePageRequest();
  const [tab, setTab] = useTabParam(TABS);
  const [params] = useSearchParams();
  const [sum, setSum] = useState<CodeSummary | null>(null);
  const [dialog, setDialog] = useState<"reset" | null>(null);
  const sync = useDataSync();
  const refresh = () => sync.refresh();                     // a job finished: every part of the page reloads once through the data version

  const load = () => request(() => api.codeSummary()).then(setSum).catch(() => undefined);   // ignored: the last numbers stay on screen when a refresh fails
  const v = useDataVersion(3000);                           // live numbers while jobs run
  useEffect(() => { load(); }, [v]);                        // eslint-disable-line react-hooks/exhaustive-deps

  if (params.get('tab') === 'review') return <Navigate to="/pairs?tab=context" replace />;
  return (
    <div className="space-y-5">
      <Tabs value={tab} onValueChange={setTab} items={[
        { id: "pairs", label: "Pairs", count: sum?.pairs.usable, content: <PairsBrowser languages={sum?.languages ?? []} /> },
        { id: "context", label: "Context improvements", content: <CampaignContextItems /> },
        { id: "maintenance", label: "Reset", content: (
          <div className="max-w-xl">
            <Panel title="Reset mined pairs" desc="Clear mined pairs and optionally clear the fix-commit queue.">
              <Button variant="ghost" className="text-crit-soft" onClick={() => setDialog("reset")}>Reset mined data</Button>
            </Panel>
          </div>) },
      ]} />
      {tab === "pairs" && <Panel title="Collection summary"><Stats sum={sum} /></Panel>}
      <Dialog open={dialog === "reset"} onOpenChange={(o) => { if (!o) setDialog(null); }} title="Reset mined data" className="max-h-[90vh]">
        {dialog === "reset" && <ResetControls onDone={() => { refresh(); setDialog(null); }} />}
      </Dialog>
    </div>
  );
}
