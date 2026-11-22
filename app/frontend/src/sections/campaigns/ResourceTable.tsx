import type { CampaignVerdict } from "../../lib/api";
import { num } from "../../lib/format";

// What the campaign asks of this computer against what is free, one row each. "Fits" or "Short" is a word, not a colour.

export function ResourceTable({ needs, free, running }: { needs: CampaignVerdict["needs"]; free: CampaignVerdict["free"]; running?: string[] }) {
  const rows: [string, number, number, string][] = [
    ["Processor cores", needs.cpu, free.cpu, ""], ["Memory", needs.ram_gb, free.ram_gb, " GB"], ["GitHub calls an hour", needs.github_per_hour, free.github_per_hour, ""],
    ["Free disk", needs.disk_gb, free.disk_gb, " GB"], ["Jobs at once", 1, free.jobs, ""],
  ];
  return (
    <div>
      <table className="w-full text-[0.8rem]">
        <caption className="sr-only">What the campaign needs and what is free</caption>
        <thead><tr className="text-left text-faint"><th className="py-1 pr-4 font-medium">Resource</th><th className="py-1 pr-4 text-right font-medium">Needs</th>
          <th className="py-1 pr-4 text-right font-medium">Free</th><th className="py-1 font-medium">Result</th></tr></thead>
        <tbody>
          {rows.map(([name, need, have, unit]) => (
            <tr key={name} className="border-t border-line">
              <td className="py-1.5 pr-4">{name}</td>
              <td className="tnum py-1.5 pr-4 text-right">{num(need)}{unit}</td>
              <td className="tnum py-1.5 pr-4 text-right">{num(have)}{unit}</td>
              <td className="py-1.5">{need <= have ? "Fits" : <strong className="font-medium text-warn">Short by {num(Math.round((need - have) * 10) / 10)}{unit}</strong>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {running && running.length > 0 && <p className="mt-2 text-[0.76rem] text-muted">Campaigns running now: {running.join(", ")}. What they use is already taken out of the free amounts.</p>}
    </div>
  );
}
