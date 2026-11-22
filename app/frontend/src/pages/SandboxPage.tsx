import { Tabs } from "../components/hud/Tabs";
import { useTabParam } from "../lib/tabParam";
import { Sandbox } from "../sections/Sandbox";
import { Verification } from "../sections/Verification";
import { SandboxImagesTab } from "../sections/SandboxImages";
import { SandboxHistoryTab } from "../sections/SandboxHistory";

const TABS = ["containers", "images", "history", "verification"] as const;

export function SandboxPage() {
  const [tab, setTab] = useTabParam(TABS);
  return (
    <Tabs value={tab} onValueChange={setTab} items={[
      { id: "containers", label: "Containers", content: <Sandbox /> },
      { id: "images", label: "Images", content: <SandboxImagesTab /> },
      { id: "history", label: "Run history", content: <SandboxHistoryTab /> },
      { id: "verification", label: "Verification", content: <Verification /> },
    ]} />
  );
}
