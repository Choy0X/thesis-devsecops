import type { InstallPlan } from "../lib/api";
import { gigabytes as bytes } from "../lib/fitWords";                 // the Model Hub writes sizes this way everywhere: 4.4 GB, 15 GB, 6.2 MB

// What installing a model so that it is ready to run would do and cost, before anything starts: the format it is converted to and why, what is downloaded, how long the conversion takes
// (an estimate, and it says what it is built on), the disk, the converter scripts that are fetched first when they are missing, and every reason it cannot be done.

/** A time in words, rounded the way an estimate should be: "about 40 seconds", "about 12 minutes", "about 1.5 hours". */
export function aboutTime(seconds: number): string {
  if (seconds < 90) return `about ${Math.max(10, Math.round(seconds / 10) * 10)} seconds`;
  const minutes = Math.round(seconds / 60);
  return minutes < 90 ? `about ${minutes} minutes` : `about ${(seconds / 3600).toFixed(1)} hours`;
}

const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 py-1"><dt className="text-muted">{label}</dt><dd className="min-w-0">{children}</dd></div>
);

/** The words of the question before an install starts: what is fetched, what is made, what it needs. Used by the dialog's confirmation. */
export function installQuestion(plan: InstallPlan, extra = ""): string {
  const parts: string[] = [];
  if (plan.download) {
    parts.push(`downloads ${plan.download.files} file${plan.download.files === 1 ? "" : "s"}, ${bytes(plan.download.bytes)}, from huggingface.co${plan.download.partial_bytes ? ` (${bytes(plan.download.partial_bytes)} of it is already there)` : ""}`);
  }
  if (plan.scripts?.will_fetch) parts.push(`fetches llama.cpp ${plan.scripts.tag}'s converter scripts from github.com (${plan.scripts.note ?? "a few small files"})`);
  if (plan.convert) parts.push(`converts the model on this computer's processor to ${plan.format} GGUF (about ${bytes(plan.convert.final_bytes)}; ${aboutTime(plan.convert.seconds)}, an estimate)`);
  const what = parts.length > 1 ? `${parts.slice(0, -1).join("; ")}; and ${parts[parts.length - 1]}` : (parts[0] ?? "installs the model");           // the parts hold commas of their own, so they are joined with semicolons
  return `This ${what}. It needs about ${bytes(plan.disk_needed_bytes ?? 0)} of free disk space. The downloaded files are kept.${extra}`;
}

export function InstallPlanView({ plan }: { plan: InstallPlan }) {
  const c = plan.convert;
  const s = plan.scripts;
  return (
    <div className="space-y-2 text-[0.8rem]">
      {plan.problems.length > 0 && (
        <ul role="alert" className="space-y-1 border border-crit-line bg-crit-tint px-3 py-2 text-crit-soft">{plan.problems.map((p) => <li key={p}>{p}</li>)}</ul>
      )}
      {c && (
        <dl className="divide-y divide-line border-y border-line">
          <Row label="Format">
            <span className="font-medium">{plan.format}</span> ({bytes(c.final_bytes)}{c.fallback ? `, up to ${bytes(c.final_bytes_max)} because some layers fall back to a larger format` : ""})
            {plan.format_reason && <span className="mt-0.5 block text-[0.76rem] text-muted">{plan.format_reason}</span>}
          </Row>
          {plan.download && (
            <Row label="Download">{bytes(plan.download.bytes)} in {plan.download.files} file{plan.download.files === 1 ? "" : "s"}{plan.download.partial_bytes ? `, ${bytes(plan.download.partial_bytes)} of it already here` : ""}</Row>
          )}
          <Row label="Conversion">
            {aboutTime(c.seconds)} on the processor
            <span className="mt-0.5 block text-[0.76rem] text-muted">{c.seconds_note}</span>
          </Row>
          <Row label="Disk">
            about {bytes(plan.disk_needed_bytes ?? 0)} while it runs{plan.download?.free_bytes != null ? `; ${bytes(plan.download.free_bytes)} is free` : ""}
            <span className="mt-0.5 block text-[0.76rem] text-muted">The converted file is about {bytes(c.final_bytes)}; the rest is taken back once it is done.</span>
          </Row>
          {s && (
            <Row label="Converter">
              {s.will_fetch ? `llama.cpp ${s.tag}'s converter scripts are fetched first from github.com (${s.note ?? "a few small files"}), and every file is checked against what GitHub lists.` : `llama.cpp ${s.installed_tag}'s converter scripts are already installed.`}
            </Row>
          )}
        </dl>
      )}
      {plan.notes.length > 0 && <ul className="list-disc space-y-0.5 pl-5 text-[0.76rem] text-muted">{plan.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
    </div>
  );
}
