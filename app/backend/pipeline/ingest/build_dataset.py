"""Merge OSV + NVD + CVEFixes into the experimental dataset.

This is the most consequential script in the project: every experiment
downstream inherits whatever this produces. A frozen output is
``data/processed/dataset_v<N>.parquet`` with its hash recorded in
``data/processed/VERSIONS.json``; once frozen it must not change.
a freeze
takes the next unused number and the change is recorded in ``data/README.md``.

Two modes:

* ``--preview`` (default) writes ``data/processed/dataset_preview.parquet`` and a
  ``dataset_stats.json`` alongside it. Use this to inspect counts and the class
  distribution before committing to a frozen version.
* ``--freeze`` writes the next unused ``dataset_v<N>.parquet`` and freezes it.

NVD and CVEFixes are optional. Where NVD is absent (its API is unreachable from
some environments, and the enrichment backlog leaves many records unscored
anyway), the CVSS base score is computed from the OSV CVSS vector and the CWE is
taken from the OSV ``cwe_ids`` field. The provenance of every label is recorded
in the ``score_source`` / ``cwe_source`` columns and summarised in the stats, so
the proportion can be reported honestly in Chapter 6.
"""
import sys
import json
import pathlib
import argparse

import pandas as pd

from app.backend.pipeline import build_common
from app.backend import paths

INTERIM = paths.INTERIM
PROC = build_common.PROC

# Splits: all data trains; a small validation slice (whole packages) is kept for
# early stopping and threshold choice only (pipeline/split.py). No test split.
from app.backend.pipeline import split as splitter
from app.backend.pipeline import fsio, versions


def _store_to_osv_frame(sdf: pd.DataFrame) -> pd.DataFrame:
    """Shape the working-store rows like the OSV parquet the builder expects.

    The store keeps a user-entered ``cvss_score`` (renamed to ``manual_score`` so
    it never collides with the NVD score during the merge), and a guaranteed
    unique ``osv_id`` (filled from the store ``key``) so the osv_id de-duplication
    does not collapse manual records that share a null id.
    """
    df = sdf.copy()
    if "key" in df.columns:
        if "osv_id" not in df.columns:
            df["osv_id"] = None
        df["osv_id"] = df["osv_id"].fillna(df["key"])
    df = df.drop(columns=[c for c in ("id", "key", "created", "updated") if c in df.columns])
    if "cvss_score" in df.columns:
        df = df.rename(columns={"cvss_score": "manual_score"})
    return df


def _best_code_pairs() -> pd.DataFrame:
    """One representative vulnerable/fixed pair per CVE from the mined fix commits:
    function level first, then hunk, then the most changed lines. Test files,
    tangled commits and fix commits that fail the release-date check are left
    out. Only the chosen pairs' code is read (not every stored pair)."""
    cols = ["cve_id", "code_before", "code_after", "filename"]
    try:
        from app.backend.pipeline import codestore
        pairs = pd.DataFrame(codestore.best_pair_candidates(), columns=["id", "cve_id", "granularity", "changed_lines", "file_path", "function_name"])
    except Exception as e:
        print(f"no code pairs could be read ({type(e).__name__}: {e}); building without them")
        return pd.DataFrame(columns=cols)
    if not len(pairs):
        return pd.DataFrame(columns=cols)
    rank = pairs.granularity.map(codestore.GRANULARITY_RANK).fillna(0)
    best = (pairs.assign(_g=rank).sort_values(["_g", "changed_lines"], ascending=False)
            .drop_duplicates("cve_id"))
    ids = best.id.tolist()
    code = {i: (r["code_before"], r["code_after"]) for i, r in codestore.pair_code_of(ids, ("code_before", "code_after")).items()}
    best = best.assign(code_before=best.id.map(lambda i: code.get(i, (None, None))[0]),
                       code_after=best.id.map(lambda i: code.get(i, (None, None))[1]),
                       filename=best.file_path + best.function_name.map(
                           lambda f: f"::{f}" if isinstance(f, str) and f else ""))
    return best[cols]


def load_inputs():
    from app.backend.pipeline import store as working_store

    sdf = working_store.to_dataframe()
    if len(sdf):
        osv = _store_to_osv_frame(sdf)
        print(f"loaded {len(osv)} records from the working store")
    else:
        osv = pd.read_parquet(INTERIM / "osv_pypi.parquet")
        print(f"working store empty; loaded {len(osv)} rows from osv_pypi.parquet")

    nvd_path = INTERIM / "nvd.parquet"
    if nvd_path.exists():
        nvd = pd.read_parquet(nvd_path)
        have_nvd = True
    else:
        print("NOTE: nvd.parquet missing; building from OSV CVSS vectors only.")
        nvd = pd.DataFrame(columns=["cve_id", "nvd_published", "nvd_status",
                                    "cvss_score", "cvss_severity", "cwe",
                                    "nvd_description"])
        have_nvd = False

    code = _best_code_pairs()
    have_code = bool(len(code))
    if not have_code:
        print("NOTE: no mined code pairs yet (run 'Mine code'); proceeding without code.")

    return osv, nvd, code, have_nvd, have_code


GHSA_FIRST = ("GHSA-", "PYSEC-", "GO-", "RUSTSEC-", "OSV-")


def _one_row_per_cve(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Collapse the advisories of one CVE (a GHSA and a PYSEC entry, ...) into one
    row. The kept row is chosen by an explicit priority: a CVSS score from NVD,
    then the GitHub advisory, then a CWE, then the longest text; the other
    advisory ids are kept in ``aliases``."""
    group = df.cve_id.where(df.cve_id.notna(), df.osv_id)
    prefix = df.osv_id.fillna("").map(
        lambda o: next((i for i, p in enumerate(GHSA_FIRST) if str(o).startswith(p)), len(GHSA_FIRST)))
    ranked = df.assign(_g=group, _nvd=(df.score_source == "nvd").astype(int), _src=-prefix,
                       _cwe=df.cwe.notna().astype(int), _len=df.text.str.len())
    ranked = ranked.sort_values(["_g", "_nvd", "_src", "_cwe", "_len"],
                                ascending=[True, False, False, False, False])
    aliases = ranked.groupby("_g").osv_id.agg(lambda s: ";".join(sorted({str(x) for x in s if x})))
    out = ranked.drop_duplicates("_g").copy()
    out["aliases"] = out._g.map(aliases)
    return out.drop(columns=["_g", "_nvd", "_src", "_cwe", "_len"]), len(df) - len(out)


def main(freeze=False, val_share: float = splitter.VAL_SHARE, train_all: bool = False):
    PROC.mkdir(parents=True, exist_ok=True)
    osv, nvd, code, have_nvd, have_code = load_inputs()

    n_osv_rows = len(osv)
    # Drop withdrawn advisories (retracted by the upstream database).
    if "withdrawn" in osv.columns:
        osv = osv[osv.withdrawn.isna()]
    n_after_withdrawn = len(osv)

    # Merge NVD on CVE id where a CVE alias exists. GHSA-only advisories keep
    # their OSV fields.
    osv_cve = osv.dropna(subset=["cve_id"])
    osv_nocve = osv[osv.cve_id.isna()].copy()
    df = osv_cve.merge(nvd, on="cve_id", how="left")
    # Re-attach GHSA-only rows so they are not lost.
    df = pd.concat([df, osv_nocve], ignore_index=True)

    # --- score and its provenance (the NVD score, then the OSV v3 vector, the v4 vector, a typed score) ---
    build_common.fill_scores(df, build_common.DATASET_SOURCES)

    df["label"] = df.cvss_score.map(build_common.label)
    before_label = len(df)
    df = df.dropna(subset=["label"])
    print(f"dropped {before_label - len(df)} records with no usable severity score")

    # --- CWE and its provenance -------------------------------------------
    if "cwe" not in df.columns:
        df["cwe"] = None
    df["cwe_source"] = None
    df.loc[df.cwe.notna(), "cwe_source"] = "nvd"
    if "osv_cwe" in df.columns:
        need_cwe = df.cwe.isna() & df.osv_cwe.notna()
        df.loc[need_cwe, "cwe"] = df.loc[need_cwe, "osv_cwe"]
        df.loc[need_cwe, "cwe_source"] = "osv"

    # --- attach code: best mined vulnerable/fixed pair per CVE ------------
    # Code already on a record (e.g. entered manually) wins; otherwise fill from
    # the mined fix commits. Assigning by map avoids a merge-suffix collision.
    for col in ("code_before", "code_after", "filename"):
        if col not in df.columns:
            df[col] = None
    if len(code):
        best = code.set_index("cve_id")
        for col in ("code_before", "code_after", "filename"):
            df[col] = df[col].where(df[col].notna(), df.cve_id.map(best[col]))

    # Text field consumed by every arm.
    nvd_desc = df["nvd_description"] if "nvd_description" in df.columns else pd.Series([None] * len(df))
    df["text"] = (df.summary.fillna("") + "\n\n"
                  + nvd_desc.fillna(df.details.fillna("")))

    # --- one row per CVE ---------------------------------------------------
    # (one row per advisory used to keep a CVE's GHSA and PYSEC entries as two
    # rows - often with different labels - and a text hash dropped distinct CVEs
    # that share boilerplate text)
    df = df.drop_duplicates("osv_id") if "osv_id" in df.columns else df
    df, n_dedup_removed = _one_row_per_cve(df)
    print(f"one row per CVE: {n_dedup_removed} duplicate advisories merged into their CVE")
    norm = df.text.str.lower().str.replace(r"\s+", " ", regex=True)
    n_same_text = int(norm.duplicated(keep=False).sum())

    # --- dates (information only; the split does not use them) --------------
    pub_src = df.get("nvd_published")
    if pub_src is None:
        pub_src = pd.Series([None] * len(df), index=df.index)
    df["pub"] = pd.to_datetime(pub_src.fillna(df.published),
                               errors="coerce", utc=True, format="mixed")

    # --- split: train + validation (whole packages), no test ---------------
    df["split"] = splitter.assign(df, val_share=val_share, train_all=train_all)

    # --- write ------------------------------------------------------------
    out = PROC / f"dataset_{versions.output_tag(freeze, 'dataset', root=PROC)}.parquet"
    fsio.write_parquet(out, df)
    if freeze:
        versions.freeze(out, PROC)

    # --- stats ------------------------------------------------------------
    split_counts = df.split.value_counts().to_dict()
    class_by_split = (df.groupby("split").label.value_counts()
                        .unstack(fill_value=0).to_dict(orient="index"))
    stats = {
        "mode": "freeze" if freeze else "preview",
        "output": str(out),
        "split": splitter.describe(val_share, train_all),
        "osv_rows_in": int(n_osv_rows),
        "after_withdrawn_filter": int(n_after_withdrawn),
        "records_final": int(len(df)),
        "duplicate_advisories_merged": int(n_dedup_removed),
        "records_sharing_text_with_another_cve": n_same_text,
        "have_nvd": have_nvd,
        "have_code": have_code,
        "records_with_cve": int(df.cve_id.notna().sum()),
        "records_with_code": int(df.code_before.notna().sum()),
        "records_with_cwe": int(df.cwe.notna().sum()),
        "split_counts": split_counts,
        "class_distribution_per_split": class_by_split,
        "score_source": df.score_source.value_counts().to_dict(),
        "cwe_source": df.cwe_source.value_counts(dropna=False).to_dict(),
    }
    stats_path = out.with_name("dataset_stats.json")
    fsio.write_json(stats_path, stats, indent=2, default=str)

    print(f"\n=== {out.name} ===")
    print(f"split: {splitter.RULE}")
    print(df.split.value_counts().to_string())
    print("\nclass distribution per split:")
    print(df.groupby("split").label.value_counts(normalize=True).round(3).to_string())
    print(f"\nrecords with code attached: {stats['records_with_code']}")
    print(f"records with a CWE: {stats['records_with_cwe']}")
    print("score source:", stats["score_source"])
    print("cwe source:", stats["cwe_source"])
    print(f"\nwritten to {out}")
    print(f"stats written to {stats_path}")
    if not freeze:
        print("\nThis is a PREVIEW. Review the numbers, add NVD/CVEFixes where "
              "possible, then re-run with --freeze to create the next unused dataset version.")
    return df


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true",
                    help="write the next unused dataset version and freeze it")
    ap.add_argument("--val-share", type=float, default=splitter.VAL_SHARE)
    ap.add_argument("--train-all", action="store_true", help="no validation rows")
    a = ap.parse_args()
    main(freeze=a.freeze, val_share=a.val_share, train_all=a.train_all)
