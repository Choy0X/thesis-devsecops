"""RQ7 - the prospective prediction study.

The strongest contamination argument in the thesis: freeze a list of at-risk
packages now, under a version-control tag, then verify months later which of them
actually received a published advisory in the interval. Because those advisories
did not exist at freeze time, no contamination argument is needed - it is genuine
pre-disclosure prediction, bounded strictly to the definition in section 1.2.

Two subcommands:

* ``freeze``  - rank currently-tracked packages by anti-pattern risk and write
  ``experiments/prospective/frozen_flags_<date>.csv`` plus a manifest recording
  the OSV snapshot date. Commit and tag this file. Run once, around November 2026.
* ``verify``  - given a later OSV snapshot (re-run ``src.ingest.osv`` first),
  count how many frozen-flagged packages received an advisory published after the
  freeze date, and report the base rate among non-flagged tracked packages for
  comparison. Run around March 2027.
"""
import json
import pathlib
import argparse
import datetime

import pandas as pd

PROC = pathlib.Path("data/processed")
INTERIM = pathlib.Path("data/interim")
OUT = pathlib.Path("experiments/prospective")


def freeze(dataset="data/processed/dataset_v1.parquet", top_frac=0.25):
    """Rank tracked packages by a simple historical-risk score and freeze the top.

    The risk score here is the count and maximum severity of a package's
    historical advisories in the TRAIN split only; richer scoring (embedding
    similarity of the package's current code to anti-patterns) can replace it
    when the codebase scanner output is available. The point of the study is the
    freeze-then-verify protocol, not the sophistication of the ranker.
    """
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(dataset)
    train = df[df.split == "train"]
    sev_rank = {"Recommendation": 1, "Warning": 2, "Critical": 3}
    g = (train.assign(sev=train.label.map(sev_rank))
              .groupby("package")
              .agg(n_hist=("package", "size"), max_sev=("sev", "max")))
    g["risk"] = g.n_hist * g.max_sev
    g = g.sort_values("risk", ascending=False)
    n_top = max(1, int(len(g) * top_frac))
    flagged = g.head(n_top).reset_index()

    snap = (pathlib.Path("data/raw/osv_snapshot_date.txt").read_text().strip()
            if pathlib.Path("data/raw/osv_snapshot_date.txt").exists() else "unknown")
    today = datetime.date.today().isoformat()
    path = OUT / f"frozen_flags_{today}.csv"
    flagged.to_csv(path, index=False)
    manifest = {"freeze_date": today, "osv_snapshot_date": snap,
                "dataset": dataset, "n_flagged": int(len(flagged)),
                "n_tracked": int(len(g)), "top_frac": top_frac}
    (OUT / f"manifest_{today}.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    print(f"frozen flags -> {path}")
    print("COMMIT AND TAG this file now; it is the pre-registration of the study.")
    return flagged


def verify(frozen_csv, manifest_json):
    """Check frozen flags against advisories published after the freeze date."""
    flagged = pd.read_csv(frozen_csv)
    manifest = json.loads(pathlib.Path(manifest_json).read_text())
    freeze_date = pd.Timestamp(manifest["freeze_date"], tz="UTC")

    osv = pd.read_parquet(INTERIM / "osv_pypi.parquet")
    osv["pub"] = pd.to_datetime(osv.published, errors="coerce", utc=True, format="mixed")
    new = osv[osv.pub > freeze_date]
    new_pkgs = set(new.package.dropna())

    flagged_set = set(flagged.package)
    tracked = set(pd.read_parquet(manifest["dataset"]).package.dropna())
    unflagged = tracked - flagged_set

    hit = flagged_set & new_pkgs
    base_hit = unflagged & new_pkgs
    result = {
        "freeze_date": manifest["freeze_date"],
        "verify_date": datetime.date.today().isoformat(),
        "n_flagged": len(flagged_set),
        "flagged_with_new_advisory": len(hit),
        "flagged_hit_rate": len(hit) / len(flagged_set) if flagged_set else None,
        "n_unflagged": len(unflagged),
        "unflagged_with_new_advisory": len(base_hit),
        "base_rate": len(base_hit) / len(unflagged) if unflagged else None,
        "example_hits": sorted(hit)[:25],
    }
    (OUT / "verification.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--dataset", default="data/processed/dataset_v1.parquet")
    f.add_argument("--top-frac", dest="top_frac", type=float, default=0.25)
    v = sub.add_parser("verify")
    v.add_argument("--frozen", required=True)
    v.add_argument("--manifest", required=True)
    a = ap.parse_args()
    if a.cmd == "freeze":
        freeze(a.dataset, a.top_frac)
    else:
        verify(a.frozen, a.manifest)
