import { useSearchParams } from "react-router-dom";
import { Tabs } from "../components/hud/Tabs";
import { useTabParam } from "../lib/tabParam";
import { Catalog, InstalledModels } from "../sections/ModelHub";
import { OllamaModels } from "../sections/OllamaModels";
import { Serving } from "../sections/Serving";

const TABS = ["installed", "catalog", "serving"] as const;

export function ModelHubPage() {
  const [tab, setTab] = useTabParam(TABS);
  const [params] = useSearchParams();
  const embedding = params.get("purpose") === "embedding";
  return (
    <Tabs value={tab} onValueChange={setTab} items={[
      { id: "installed", label: "Installed", content: <div className="space-y-5"><OllamaModels embedding={embedding} /><InstalledModels /></div> },
      { id: "catalog", label: "Catalog", content: <Catalog /> },
      { id: "serving", label: "Serving", content: <Serving /> },
    ]} />
  );
}
