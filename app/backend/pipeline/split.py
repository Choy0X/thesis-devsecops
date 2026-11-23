"""One train / validation split for every dataset the app builds.

All mined data trains the model. A small validation slice is held out only for
the training algorithm itself: early stopping, choosing the decision threshold
and checking the false-positive rate on safe code. There is no test split; the
owner compares the original and the fine-tuned model separately.

Rules (identical for the severity-classification dataset, the code-pair dataset
and the dependency rows, so a CVE is in the same split everywhere):

* **Validation = whole packages.** ``val`` holds the packages whose stable hash
  ``sha1("<ecosystem>:<canonical package>")`` falls below ``val_share`` (default 5%). A
  package is entirely in one split, so validation measures code the model has
  not seen from that project - not a near-copy of a training row. The choice is
  deterministic (the same on every build), never random.
* **One split per CVE.** All rows of a CVE (its GHSA / PYSEC advisories, all its
  code pairs) follow the CVE's first package in sorted order. This rule wins over
  the package rule: a CVE listing several packages can put one of them in both
  splits (4 of ~6,000 packages in the PyPI dump).
* **One rule everywhere.** The hash, the share and the package key are those of the split
  manifest (``splitmanifest.bucket``, ``splitmanifest.VAL_SHARE`` and the ecosystem
  adapters' ``package_key``); this module only applies them to a table, one CVE at a time,
  so the same package gets the same split whichever builder reads it.
* **Train on everything** (``train_all``): validation is folded into train, for
  the final model once its settings have been chosen on validation.
"""
from __future__ import annotations

import pandas as pd

from app.backend.pipeline import ecosystems, splitmanifest

VAL_SHARE = splitmanifest.VAL_SHARE
RULE = "val = packages with sha1('<ecosystem>:<canonical package>') / 2^160 < val_share; one split per CVE"


def package_key(ecosystem, package) -> str | None:
    """'<ecosystem>:<first canonical package>' (records list several packages as 'a, b'). The
    canonical form is the ecosystem adapter's, for example a Go module without its major-version
    suffix, so a project is one key however a record spells it."""
    if not isinstance(package, str) or not package.strip():
        return None
    return ecosystems.package_key(ecosystem if isinstance(ecosystem, str) else None, package)


def assign(df: pd.DataFrame, cve_col: str = "cve_id", osv_col: str = "osv_id",
           eco_col: str = "ecosystem", pkg_col: str = "package",
           val_share: float = VAL_SHARE, train_all: bool = False) -> pd.Series:
    """'train' / 'val' for every row of ``df`` (index-aligned)."""
    if not len(df):
        return pd.Series([], dtype=object, index=df.index)
    if train_all or not val_share:
        return pd.Series("train", index=df.index)
    group = df[cve_col].where(df[cve_col].notna(), df[osv_col]) if cve_col in df else df[osv_col]
    group = group.fillna(pd.Series(df.index.astype(str), index=df.index))
    keys = [package_key(e, p) for e, p in zip(df.get(eco_col, pd.Series(None, index=df.index)),
                                               df.get(pkg_col, pd.Series(None, index=df.index)))]
    keys = pd.Series(keys, index=df.index).fillna("cve:" + group.astype(str))
    # one key per CVE group: its smallest package key
    gkey = keys.groupby(group).transform("min")
    is_val = gkey.map(lambda k: splitmanifest.base_split(k, val_share) == "val")
    return pd.Series(["val" if v else "train" for v in is_val], index=df.index)


def describe(val_share: float = VAL_SHARE, train_all: bool = False) -> dict:
    return {"splits": ["train"] if train_all or not val_share else ["train", "val"],
            "val_share": 0.0 if train_all else float(val_share), "train_all": bool(train_all),
            "rule": RULE}
