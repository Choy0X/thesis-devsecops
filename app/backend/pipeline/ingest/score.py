"""Compute a CVSS base score from a vector string.

Used where NVD has not enriched a record (the enrichment backlog) but OSV
supplies a CVSS vector. The proportion of labels derived this way is reported
in data/README.md and in Chapter 6.
"""
from cvss import CVSS3, CVSS4


def score_from_vector(vector):
    if not isinstance(vector, str):
        return None
    v = vector.strip()
    try:
        if v.startswith("CVSS:3"):
            return float(CVSS3(v).base_score)
        if v.startswith("CVSS:4"):
            return float(CVSS4(v).base_score)
    except Exception:  # ignored: an unparseable vector has no score
        return None
    return None
