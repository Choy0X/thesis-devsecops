import type { AgentMode } from "../../lib/api";
import { Glitch } from "../kit/Glitch";
import { MatrixRain } from "../kit/MatrixRain";

// Agentic Mode's own empty state, replacing the shared EmptyState for one reason: this screen is the start of an autonomous run, not a quiet "nothing here yet" -- it earns a
// moment of its own. A framed ZTSA block mark decodes out of noise on mount, then tears into displaced horizontal bands on its own short interval (kit/Glitch's "auto" mode,
// index.css's .glitch-tear keyframe -- a real tear, not a uniform wobble), while an independent, always-on brightness pulse (.fx-idle-flicker) keeps it reading as a live readout
// between bursts, and a blinking terminal cursor sits after the headline. Three different rhythms layered over the same Matrix rain as before -- nothing behind the glyphs, no
// panel, no box; every motion here stops under reduced motion exactly like the rain does.

const AUTO_INTERVAL_MS = 1400;

const MARK = [
  "┌──────────────────────────────────┐",
  "│                                  │",
  "│  ██████  ██████  ██████   ████   │",
  "│       █    ██    ██      ██  ██  │",
  "│      █     ██    ██      ██  ██  │",
  "│     █      ██    ██████  ██████  │",
  "│    █       ██        ██  ██  ██  │",
  "│   █        ██        ██  ██  ██  │",
  "│  ██████    ██    ██████  ██  ██  │",
  "│                                  │",
  "└──────────────────────────────────┘",
].join("\n");

const SUBLINE: Record<AgentMode, string> = {
  plan: "Point it at the project. It reads the code, works out what to fix or improve, and comes back with a plan -- nothing changes until you say so.",
  manual: "Give it an order. It works a sandboxed copy and pauses for your approval before it writes or runs a thing.",
  auto: "Give it an order. It works a sandboxed copy start to finish and reports back -- the real project is never touched.",
};

const ENGINE_LINE = {
  v2: "Durable run is on: each run keeps a record, and the Task tab appears after the first instruction.",
  legacy: "Standard engine (the one used so far). Choose the durable run beside Mode to get a record of each run.",
};

export function AgentEmptyState({ mode, engine }: { mode: AgentMode; engine?: "v2" | "legacy" }) {
  return (
    <div className="relative flex h-full flex-col items-center justify-center overflow-hidden px-6 py-12 text-center">
      <MatrixRain className="opacity-15" />
      <div className="relative flex flex-col items-center gap-4">
        <Glitch as="pre" on="auto" autoIntervalMs={AUTO_INTERVAL_MS} text={MARK} className="fx-idle-flicker font-mono text-[0.78rem] leading-[1.15] text-brand-strong" />
        <div>
          <p className="caps text-fg">No target locked<span className="fx-cursor" aria-hidden /></p>
          <p className="mt-1.5 max-w-sm text-[0.8rem] leading-relaxed text-muted">{SUBLINE[mode]}</p>
          {engine && <p className="mt-1 max-w-sm text-[0.75rem] leading-relaxed text-faint">{ENGINE_LINE[engine]}</p>}
        </div>
      </div>
    </div>
  );
}
