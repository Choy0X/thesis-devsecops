"""What the dataset builders share: the severity label, where a CVSS score comes from, and the output folder.

The three builders write different things (the flat classification dataset, the code-pair dataset with its detection set, and the
reviewed-dataset folder), but they all label a CVE the same way. Each keeps its own names for the score sources, because those names
are stored in files already written and a reader must keep understanding them; the order and the rule are decided here, once.

Where a score comes from, in order: the NVD score of the CVE, the OSV CVSS v3 vector, the OSV CVSS v4 vector, and last a score typed
on the record.
"""
from __future__ import annotations

import math

import pandas as pd

from app.backend.pipeline import versions
from app.backend.pipeline.ingest.score import score_from_vector

PROC = versions.PROC
SEVERITIES = ("Critical", "Warning", "Recommendation")

# The stored name of each source, by builder.
DATASET_SOURCES = {"nvd": "nvd", "v3": "osv_v3_vector", "v4": "osv_v4_vector", "record": "manual"}     # build_dataset (score_source)
CODE_SOURCES = {"nvd": "nvd", "v3": "cvss_v3", "v4": "cvss_v4", "record": "record"}                      # code pairs and reviewed records


def label(score) -> str | None:
    """Recommendation below 6.0, Warning below 9.0, Critical from 9.0; None without a score."""
    if score is None or pd.isna(score):
        return None
    if score < 6.0:
        return "Recommendation"
    if score < 9.0:
        return "Warning"
    return "Critical"


# GitHub's qualitative label stands in for a score only where every score of its band falls on one side of a boundary of ``label``: LOW (0.1 to 3.9), HIGH (7.0 to 8.9) and CRITICAL (9.0 to 10).
# MODERATE (4.0 to 6.9) straddles the 6.0 boundary between Recommendation and Warning, so it decides nothing.
OSV_LABEL_SEVERITY = {"LOW": "Recommendation", "HIGH": "Warning", "CRITICAL": "Critical"}
OSV_LABEL_SOURCE = "osv_label"                                                                          # the severity_source of a category that came from the label alone


def label_severity(osv_label) -> str | None:
    """The severity category GitHub's label settles on its own, or None (MODERATE, an unknown word, nothing)."""
    if not isinstance(osv_label, str):
        return None
    return OSV_LABEL_SEVERITY.get(osv_label.strip().upper())


def number(value) -> float | None:
    """A float, or None for a missing, NaN or non-numeric value."""
    if value is None:
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def severity_score(*, nvd=None, v3=None, v4=None, record=None) -> tuple[float | None, str | None]:
    """``(score, source)`` for one CVE from the first usable input; ``source`` is ``nvd``, ``v3``, ``v4`` or ``record`` (map it with the
    builder's own names), and ``(None, None)`` when nothing is usable. ``v3`` and ``v4`` are CVSS vector strings."""
    n = number(nvd)
    if n is not None:
        return n, "nvd"
    for source, vector in (("v3", v3), ("v4", v4)):
        score = score_from_vector(vector)
        if score is not None:
            return float(score), source
    r = number(record)
    if r is not None:
        return r, "record"
    return None, None


def fill_scores(df: pd.DataFrame, names: dict[str, str] = DATASET_SOURCES) -> pd.DataFrame:
    """The same rule applied to a whole table, in place: ``cvss_score`` (the NVD score where there is one) is filled from ``cvss_vector``,
    then ``cvss_v4_vector``, then ``manual_score``, and ``score_source`` records which. The columns not present are skipped."""
    if "cvss_score" not in df.columns:
        df["cvss_score"] = pd.NA
    df["score_source"] = None
    df.loc[df.cvss_score.notna(), "score_source"] = names["nvd"]

    need = df.cvss_score.isna() & df.cvss_vector.notna()
    df.loc[need, "cvss_score"] = df.loc[need, "cvss_vector"].map(score_from_vector)
    df.loc[need & df.cvss_score.notna(), "score_source"] = names["v3"]

    if "cvss_v4_vector" in df.columns:
        need4 = df.cvss_score.isna() & df.cvss_v4_vector.notna()
        df.loc[need4, "cvss_score"] = df.loc[need4, "cvss_v4_vector"].map(score_from_vector)
        df.loc[need4 & df.cvss_score.notna(), "score_source"] = names["v4"]

    if "manual_score" in df.columns:
        needm = df.cvss_score.isna() & df.manual_score.notna()
        df.loc[needm, "cvss_score"] = pd.to_numeric(df.loc[needm, "manual_score"], errors="coerce")
        df.loc[needm & df.cvss_score.notna(), "score_source"] = names["record"]
    return df
