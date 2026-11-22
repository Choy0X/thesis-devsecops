import { useState, type ReactNode } from "react";
import { Bell, Cog, Cpu, HardDrive, Plus, RefreshCw, Trash2 } from "lucide-react";
import {
  Button, Checkbox, FileInput, Glitch, Hint, IconButton, Input, KeyValue, NumberField, Radio, RadioGroup, SecretField, SelectField, SettingFacts, SettingNote, SettingRow, SettingsCard,
  Stat, Switch, TextField, TextareaField,
} from "../components/kit";
import { CodeViewer } from "../components/code/CodeViewer";
import { SettingsMenu } from "../components/SettingsMenu";
import { Badge, SeverityBadge } from "../components/hud/Badge";
import { Progress } from "../components/hud/Progress";
import { useConfirm } from "../components/hud/AlertDialog";
import { Dialog } from "../components/hud/Dialog";
import { useToast } from "../components/Toast";
import { EmptyState, ErrorState, Panel, StatusPill } from "../components/ui";

// Every component of the kit in every state, on one page, for looking at the interface as a whole, for the contrast and accessibility checks and for the screenshots. It is not in the left
// rail: it is reached from Settings, Developer, or at /kit. Nothing here talks to the backend.

function Row({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title}>
      <h3 className="caps mb-2 text-faint">{title}</h3>
      <div className="flex flex-wrap items-start gap-3">{children}</div>
    </section>
  );
}

const DEVICES = [{ value: "auto", label: "Automatic" }, { value: "gpu", label: "Graphics card", hint: "6 GB" }, { value: "cpu", label: "Processor" }];
const MODELS = [
  { label: "Downloaded", options: [{ value: "qwen", label: "Qwen2.5 Coder 0.5B", hint: "469 MB" }, { value: "jina", label: "Jina code embeddings 0.5B", hint: "953 MB" }] },
  { label: "Other", options: [{ value: "none", label: "None selected" }] },
];

const SAMPLE = ["export function total(prices: number[]): number {", "  // the sum of every price, in cents", "  return prices.reduce((sum, p) => sum + Math.round(p * 100), 0);", "}"].join(String.fromCharCode(10));

export function KitGallery() {
  const [device, setDevice] = useState("auto");
  const [model, setModel] = useState("qwen");
  const [engine, setEngine] = useState("local");
  const [on, setOn] = useState(true);
  const [secret, setSecret] = useState("");
  const [jobs, setJobs] = useState("4");
  const [dialog, setDialog] = useState(false);
  const confirm = useConfirm();
  const toast = useToast();

  return (
    <div className="space-y-7">
      <Panel title="Buttons and icon buttons" desc="Square with a cut corner pair, caps labels. Filled buttons draw their focus ring in the foreground colour.">
        <div className="space-y-5">
          <Row title="Variants">
            <Button variant="primary">Primary</Button><Button>Subtle</Button><Button variant="ghost">Ghost</Button><Button variant="danger">Danger</Button><Button variant="critical">Critical</Button>
          </Row>
          <Row title="Sizes, states">
            <Button size="sm" variant="primary">Small</Button><Button variant="primary" loading>Loading</Button><Button disabled>Disabled</Button><Button icon={<Plus size={14} aria-hidden />}>With icon</Button>
          </Row>
          <Row title="Icon buttons (tooltip and name from the label)">
            <IconButton label="Settings" tone="subtle"><Cog size={16} aria-hidden /></IconButton>
            <IconButton label="Add"><Plus size={16} aria-hidden /></IconButton>
            <IconButton label="Delete" tone="danger"><Trash2 size={16} aria-hidden /></IconButton>
            <IconButton label="Disabled" disabled><Plus size={16} aria-hidden /></IconButton>
          </Row>
        </div>
      </Panel>

      <Panel title="Fields" desc="A label is a real label: clicking it focuses the control and nothing else.">
        <div className="grid gap-5 md:grid-cols-2">
          <TextField label="Endpoint" placeholder="http://localhost:8080/v1" hint="The /v1 root of an OpenAI-compatible server." />
          <TextField label="Host" prefix="https://" suffix=".local" defaultValue="models" />
          <TextField label="With an error" defaultValue="not a url" error="That is not a web address." />
          <TextField label="Disabled" defaultValue="read only" disabled />
          <NumberField label="Jobs at once" value={jobs} min={1} max={16} onChange={(e) => setJobs(e.target.value)} hint="Between 1 and 16." />
          <Input aria-label="A field with no label" placeholder="A bare field for filters" />
          <div className="md:col-span-2"><TextareaField label="Notes" placeholder="Anything worth keeping" /></div>
          <SecretField label="API key" value={secret} onChange={setSecret} placeholder="sk-..." hint="Kept on this computer only." />
          <SecretField label="Saved key" value="" onChange={() => undefined} saved savedHint="a1b2" onRemove={() => toast.info("Remove was pressed.")} />
        </div>
      </Panel>

      <Panel title="Selects">
        <div className="grid gap-5 md:grid-cols-2">
          <SelectField label="Device" options={DEVICES} value={device} onValueChange={setDevice} hint="Where the embedder runs." />
          <SelectField label="Model (grouped)" options={MODELS} value={model} onValueChange={setModel} />
          <SelectField label="With an error" options={DEVICES} value="" onValueChange={() => undefined} placeholder="Choose a device" error="Pick one." />
          <SelectField label="Disabled" options={DEVICES} value="cpu" onValueChange={() => undefined} disabled />
        </div>
      </Panel>

      <Panel title="Settings cards and rows" desc="A card is one concern; its rows have the same two columns in every card, so the controls of a page share one line. A kit field inside a card becomes a row by itself.">
        <div className="grid gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
          <SettingsMenu items={[{ id: "engines", label: "Engines", icon: Cpu }, { id: "notify", label: "Notifications", icon: Bell }, { id: "storage", label: "Storage and compute", icon: HardDrive }]}
            value="notify" to={(id) => `/kit#${id}`} />
          <SettingsCard title="Models folder" desc="The one folder that holds every model on this computer."
            action={<IconButton label="Look again"><RefreshCw size={15} aria-hidden /></IconButton>}
            footer={<><Button variant="primary">Save</Button><Button variant="subtle">Reset to default</Button></>}>
            <TextField label="Folder" placeholder="data/models" hint="Empty means the default folder." />
            <SelectField label="Device" options={DEVICES} value={device} onValueChange={setDevice} hint="Where the embedder runs." />
            <SecretField label="API key" value="" onChange={() => undefined} saved savedHint="a1b2" onRemove={() => toast.info("Remove was pressed.")} />
            <SettingRow label="Engine" hint="A model on this computer, or an endpoint.">
              <RadioGroup legend="Engine" hideLegend variant="segmented" value={engine} onValueChange={setEngine} options={[{ value: "local", label: "Local" }, { value: "external", label: "External" }]} />
            </SettingRow>
            <SettingRow tight label="Ask before quitting" hint="A switch is as tall as its name, so the name is not lowered.">
              <Switch aria-label="Ask before quitting in the gallery" checked={on} onCheckedChange={setOn} />
            </SettingRow>
            <SettingFacts label="Facts of the folder"><dt>In use now</dt><dd className="font-mono">data/models</dd><dt>Free space</dt><dd>293.2 GB</dd></SettingFacts>
            <SettingNote className="text-warn">A note that belongs to the card, across the full width.</SettingNote>
          </SettingsCard>
        </div>
      </Panel>

      <Panel title="Switches, checkboxes and choices">
        <div className="grid gap-5 md:grid-cols-2">
          <div className="space-y-4">
            <Switch label="Ask before quitting" description="Shows a question every time the app is closed." checked={on} onCheckedChange={setOn} />
            <Switch label="Off by default" />
            <Switch label="Disabled" disabled />
            <Checkbox label="Do not ask me again" />
            <Checkbox label="Checked" defaultChecked description="With a line of explanation." />
            <Checkbox label="With a tooltip" title="The explanation shows on hover and on keyboard focus." />
          </div>
          <div className="space-y-5">
            <RadioGroup legend="LLM engine" variant="segmented" value={engine} onValueChange={setEngine} options={[{ value: "local", label: "Local" }, { value: "external", label: "External" }]} />
            <RadioGroup legend="Compute" value={device} onValueChange={setDevice} options={[
              { value: "auto", label: "Automatic", description: "Use the card if it fits." }, { value: "gpu", label: "Graphics card" }, { value: "cpu", label: "Processor", disabled: true },
            ]} />
            <div className="flex flex-wrap gap-3" role="group" aria-label="Single radios">
              <Radio name="gallery-single" label="One radio of a card" description="Laid out by the page." defaultChecked className="border border-line px-3 py-1.5" />
              <Radio name="gallery-single" label="Another" className="border border-line px-3 py-1.5" />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <FileInput aria-label="Choose a file" buttonLabel="Choose a file" />
              <FileInput aria-label="Choose a file, ghost" variant="ghost" buttonLabel="Import" />
              <FileInput aria-label="Choose a file, disabled" buttonLabel="Disabled" disabled />
            </div>
            <p className="text-[0.85rem] text-muted">A <Hint label="Dotted, with a tooltip on hover and keyboard focus.">hint inside a sentence</Hint> marks itself.</p>
          </div>
        </div>
      </Panel>

      <Panel title="Telemetry">
        <div className="grid gap-6 md:grid-cols-3">
          <Stat label="Jobs" value={12} unit="running" hint="of 16 allowed" />
          <Stat label="Failed" value={3} tone="crit" hint="in the last hour" />
          <Stat label="Free memory" value="5.8" unit="GB" tone="warn" />
        </div>
        <div className="mt-6 grid gap-6 md:grid-cols-2">
          <KeyValue items={[{ label: "Model", value: "qwen2.5-coder-0.5b" }, { label: "Device", value: "GPU (6 GB)" }, { label: "Context", value: "4096" }]} />
          <div className="space-y-3"><Progress value={62} label="Indexing" /><div className="flex flex-wrap gap-2"><StatusPill status="running" /><StatusPill status="done" /><StatusPill status="error" /><StatusPill status="interrupted" />
            <Badge>v1.2</Badge><SeverityBadge level="low" /><SeverityBadge level="high" /><SeverityBadge level="critical" /></div></div>
        </div>
      </Panel>

      <Panel title="States and effects">
        <div className="grid gap-5 md:grid-cols-2">
          <ErrorState error={new Error("The server did not answer.")} onRetry={() => toast.info("Retry")} />
          <div className="border border-line"><EmptyState title="Nothing here yet" hint="Index this project to search it." action={<Button size="sm" variant="primary">Index</Button>} /></div>
        </div>
        <p className="mt-6 text-[1.4rem] font-semibold"><Glitch text="Vuln Framework" on="both" /> <span className="text-[0.8rem] font-normal text-muted">hover or focus it</span></p>
      </Panel>

      <Panel title="Code viewer" desc="A file's content as CodeMirror draws it: read-only, numbered from the line the excerpt begins at, coloured by the file's name, and loaded only when a file is shown.">
        <CodeViewer code={SAMPLE} path="sample.ts" startLine={12} marked={[14]} label="sample.ts, from line 12" fallback={<pre className="border border-line bg-terminal p-2 font-mono text-[0.74rem]">{SAMPLE}</pre>} />
      </Panel>

      <Panel title="Dialogs and toasts">
        <div className="flex flex-wrap gap-3">
          <Button onClick={() => setDialog(true)}>Open a dialog</Button>
          <Button onClick={() => confirm({ title: "Quit the app?", description: "No job is running.", confirmLabel: "Quit", rememberLabel: "Do not ask me again" })}>Ask (with a box)</Button>
          <Button variant="danger" onClick={() => confirm({ title: "Reset everything?", description: "All selected data is deleted.", confirmLabel: "Reset", tone: "critical", typeToConfirm: "RESET" })}>Ask (critical)</Button>
          <Button onClick={() => toast.success("Saved.")}>Success toast</Button>
          <Button onClick={() => toast.error("Could not save the settings.")}>Error toast</Button>
          <Button onClick={() => toast.info("The model is loading.")}>Info toast</Button>
        </div>
        <Dialog open={dialog} onOpenChange={setDialog} title="Test the connection" description="Sends one small request with the values on this page."><p className="text-[0.85rem] text-muted">A dialog body.</p></Dialog>
      </Panel>
    </div>
  );
}
