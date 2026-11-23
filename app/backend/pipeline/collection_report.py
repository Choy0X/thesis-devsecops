"""The collection report (``notes/collection_report.md``): measured aggregates per ecosystem and the state of the latest campaigns.

Everything is read from the working store and the campaign state at the time the report is written; nothing is promised. Counts are
partial while a campaign runs, and the report says so. Malware and withdrawn advisories are listed apart (they are never queued, mined or
built). No code, no advisory text and no case is reproduced here.
"""
from __future__ import annotations

import datetime
import pathlib

from app.backend.pipeline import fsio

NOT_COLLECTED = ("The package-less, C and C++ heavy OSV sources (GIT, Linux, OSS-Fuzz) have adapters but are not enabled, and the CVEfixes "
                 "dataset is not imported by this app: neither is collected.")


def _n(x) -> str:
    return f"{int(x or 0):,}"


def render(funnel: dict, status: dict, *, generated: str) -> str:
    lines = ["# Collection report", "",
             f"Generated {generated} from the working store and the campaign state. Counts are measured at that time and are partial while a "
             "campaign runs; nothing here is a promise of totals. Aggregates only: no code, advisory text or case is reproduced.", "",
             "## Funnel per ecosystem (vulnerability-kind advisories)", "",
             "| Ecosystem | Advisories | With a fix pointer | Fix mined | Code pairs | Usable pairs |", "|---|---|---|---|---|---|"]
    for eco, f in funnel.items():
        lines.append(f"| {eco} | {_n(f['advisories'])} | {_n(f['with_fix_pointer'])} | {_n(f['mined'])} | {_n(f['pairs'])} | {_n(f['usable_pairs'])} |")
    lines += ["", "Usable pairs are not test, not tangled and have code. They are candidates, not labels: which of them may supervise a model is "
              "decided by the gate and by review (see `notes/data_funnel_report.md`).", "",
              "## Advisories that are never queued, mined or built", "",
              "| Ecosystem | Malware | Withdrawn | Informational |", "|---|---|---|---|"]
    for eco, f in funnel.items():
        lines.append(f"| {eco} | {_n(f['malware'])} | {_n(f['withdrawn'])} | {_n(f['informational'])} |")
    lines += ["", "These are counted apart and are never queued, mined or built.", "", "## Campaigns", ""]
    campaigns = status.get("campaigns") or []
    if not campaigns:
        lines += ["No campaign has been run.", ""]
    else:
        lines += [f"The latest {len(campaigns)} campaign{'s' if len(campaigns) != 1 else ''}, newest first. A campaign works through the stages of each of its ecosystems; a step that "
                  "was cut off is repeated when it is resumed. Throughput is bounded by the GitHub quota (about 5,000 requests per hour, shared by all campaigns).", "",
                  "| Campaign | State | Ecosystems | Steps done | Started |", "|---|---|---|---|---|"]
        for c in campaigns:
            steps = (c.get("summary") or {}).get("steps") or {}
            done = f"{steps.get('done', 0) + steps.get('quota', 0) + steps.get('skipped', 0)} of {steps.get('total', '?')}" if steps else "not recorded"
            lines.append(f"| `{c['campaign_id']}` | {c['state']} | {', '.join(c['ecosystems'])} | {done} | {c.get('created_at')} |")
        errors = [(c["campaign_id"], e) for c in campaigns for e in (c.get("summary") or {}).get("errors", [])]
        if errors:
            lines += ["", "Errors:", ""] + [f"- `{cid}`, {e['target']}, {e['stage']}: {e['error']}" for cid, e in errors]
        lines.append("")
    lines += ["## Not collected", "", NOT_COLLECTED, ""]
    return "\n".join(lines).rstrip() + "\n"


def write(path="notes/collection_report.md") -> dict:
    from app.backend.pipeline import derived, store
    funnel = store.ecosystem_funnel()
    from app.backend.pipeline.campaigns import repo
    with derived.connect() as conn:
        campaigns = repo.all_campaigns(conn)[:5]
    status = {"campaigns": campaigns}
    text = render(funnel, status, generated=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
    path = fsio.write_text(path, text)
    return {"path": str(path), "funnel": funnel, "campaigns": [c["campaign_id"] for c in campaigns]}


if __name__ == "__main__":
    from app.backend import config  # noqa: F401  (overlays the saved settings onto the environment)
    print(write()["path"])
