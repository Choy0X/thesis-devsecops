import { useEffect, useState } from "react";
import { api, type ByYear, type Lineage, type Mix } from "../../lib/api";
import { useDataVersion } from "../../lib/dataSync";
import { ErrorState, Skeleton } from "../ui";
import { FlowChart } from "./FlowChart";
import { RadarChart, type Axis } from "./RadarChart";
import { YearSeverityColumns } from "./YearSeverityColumns";

// The two charts that read the store: where advisories drop out on the way to a dataset, and what the collected data is made of.

export function LineagePanel() {
  const [data, setData] = useState<Lineage | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const load = () => api.lineage().then((d) => { setData(d); setErr(null); }).catch(setErr);
  const v = useDataVersion(15000);
  useEffect(() => { load(); }, [v]);                                          // eslint-disable-line react-hooks/exhaustive-deps
  if (!data) return err != null ? <ErrorState error={err} title="Could not read the flow" onRetry={load} /> : <Skeleton className="h-48 w-full" />;
  return <FlowChart data={data} />;
}

const axes = (counts: Record<string, number>): Axis[] => Object.entries(counts).map(([label, value]) => ({ label, value }));

/** Three radars (language, ecosystem, severity) of the collected advisories, or of one dataset build when `dataset` is its tag. */
export function MixPanel({ dataset = "" }: { dataset?: string }) {
  const [mix, setMix] = useState<Mix | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const load = () => api.mix(dataset).then((m) => { setMix(m); setErr(null); }).catch(setErr);
  const v = useDataVersion(15000);
  useEffect(() => { load(); }, [dataset, v]);                                 // eslint-disable-line react-hooks/exhaustive-deps
  if (!mix) return err != null ? <ErrorState error={err} title="Could not read the make-up" onRetry={load} /> : <Skeleton className="h-48 w-full" />;
  return (
    <div className="grid gap-4 md:grid-cols-3">
      <RadarChart title="Language" description="Records by programming language." axes={axes(mix.language)} unit="records" />
      <RadarChart title="Ecosystem" description="Advisories by package ecosystem." axes={axes(mix.ecosystem)} unit="records" />
      {mix.severity
        ? <RadarChart title="Severity" description="Advisories by the severity the advisory database gives." axes={axes(mix.severity)} unit="advisories" />
        : <p className="border border-line bg-surface p-4 text-[0.8rem] text-muted">A dataset build does not record severity, so its make-up is shown by language and ecosystem only.</p>}
    </div>
  );
}

/** How many vulnerabilities were published in each year, as columns split by severity. */
export function YearSeverityPanel() {
  const [data, setData] = useState<ByYear | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const load = () => api.byYear().then((d) => { setData(d); setErr(null); }).catch(setErr);
  const v = useDataVersion(30000);
  useEffect(() => { load(); }, [v]);                                          // eslint-disable-line react-hooks/exhaustive-deps
  if (!data) return err != null ? <ErrorState error={err} title="Could not read the advisories" onRetry={load} /> : <Skeleton className="h-64 w-full" />;
  return <YearSeverityColumns data={data} />;
}
