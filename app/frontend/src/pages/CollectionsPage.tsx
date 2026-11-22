import { LineagePanel, YearSeverityPanel } from "../components/charts/Panels";
import { EcosystemTable } from "../sections/EcosystemTable";
import { CveSearch } from "../sections/CveSearch";
import { StartCampaignEntry } from "../sections/campaigns/StartCampaignEntry";

export function CollectionsPage() {
  return <div className="space-y-5">
    <CveSearch />
    <EcosystemTable />
    <StartCampaignEntry />
    <div className="grid items-start gap-5 xl:grid-cols-2">
      <YearSeverityPanel />
      <LineagePanel />
    </div>
  </div>;
}
