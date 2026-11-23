"""Pull NVD CVE records by publication-date window.

Querying by date range costs a few hundred requests instead of several thousand
single-CVE lookups. An API key raises the rate limit substantially; without one
the harvest takes many hours.
"""
import os
import json
import time
import pathlib
from datetime import datetime, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv
from app.backend import paths
from app.backend.pipeline import fsio

load_dotenv()

KEY = os.getenv("NVD_API_KEY")
API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
RAW = pathlib.Path("data/raw")
INTERIM = paths.INTERIM

HEADERS = {"apiKey": KEY} if KEY else {}
DELAY = 0.7 if KEY else 6.5  # seconds between requests


def fetch_window(start: datetime, end: datetime) -> list:
    """One publication-date window, paged to completion."""
    out, idx = [], 0
    while True:
        params = {
            "pubStartDate": start.strftime("%Y-%m-%dT00:00:00.000"),
            "pubEndDate": end.strftime("%Y-%m-%dT00:00:00.000"),
            "resultsPerPage": 2000,
            "startIndex": idx,
        }
        for attempt in range(5):
            r = requests.get(API, params=params, headers=HEADERS, timeout=90)
            if r.status_code == 200:
                break
            print("  retry", r.status_code)
            time.sleep(10 * (attempt + 1))
        else:
            raise RuntimeError(f"NVD request failed for {start:%Y-%m-%d}")

        data = r.json()
        out.extend(data.get("vulnerabilities", []))
        total = data.get("totalResults", 0)
        idx += data.get("resultsPerPage", 0)
        time.sleep(DELAY)
        if idx >= total:
            return out


def harvest(since: str = "2016-01-01"):
    """NVD CVEs published since ``since``. The harvest is cached in nvd_raw.json
    together with its start date: a cache that starts on or before ``since`` is
    reused (flatten() drops the older CVEs); one that starts later does not cover
    the request and is harvested again."""
    RAW.mkdir(parents=True, exist_ok=True)
    cache = RAW / "nvd_raw.json"
    cache_since = RAW / "nvd_raw_since.txt"
    if cache.exists():
        cached_from = cache_since.read_text().strip()[:10] if cache_since.exists() else None
        if cached_from is None:                  # cached before the start date was kept:
            from app.backend.pipeline.jsonstream import iter_json_array   # read it once
            dates = [str((it.get("cve") or {}).get("published") or "")[:10]
                     for it in iter_json_array(cache)]
            dates = [d for d in dates if d]
            if dates:
                cached_from = min(dates)
                fsio.write_text(cache_since, cached_from + "\n")
        if cached_from and cached_from <= str(since)[:10]:
            print(f"cached: {cache} (published since {cached_from})")
            from app.backend.pipeline.jsonstream import iter_json_array
            return iter_json_array(cache)        # streamed: the harvest can be GBs
        print(f"the cached NVD harvest starts {cached_from or 'at an unknown date'}, "
              f"after {str(since)[:10]}: harvesting again")

    start = datetime.fromisoformat(since)
    end_all = datetime.utcnow()
    items = []
    while start < end_all:
        end = min(start + timedelta(days=119), end_all)
        print(f"{start:%Y-%m-%d} -> {end:%Y-%m-%d}")
        items.extend(fetch_window(start, end))
        start = end

    def dump(part):                                    # streamed into a .part file: the harvest can be 800 MB
        with open(part, "w", encoding="utf-8") as fh:
            json.dump(items, fh)
    fsio.write_with(cache, dump)
    fsio.write_text(cache_since, str(since)[:10] + "\n")
    print("total records:", len(items))
    return items


def flatten(items, since: str | None = None) -> pd.DataFrame:
    """nvd.parquet from the harvest; with ``since``, only CVEs published on or
    after that date."""
    INTERIM.mkdir(parents=True, exist_ok=True)
    rows = []
    skipped = 0
    for it in items:
        cve = it["cve"]
        if since and str(cve.get("published") or "")[:10] < str(since)[:10]:
            skipped += 1
            continue
        metrics = cve.get("metrics", {})
        m, version = [], None
        for key, ver in (("cvssMetricV31", "3.1"), ("cvssMetricV30", "3.0"), ("cvssMetricV2", "2.0")):
            if metrics.get(key):
                m, version = metrics[key], ver
                break
        score = severity = None
        if m:
            d = m[0].get("cvssData", {})
            score = d.get("baseScore")
            severity = d.get("baseSeverity") or m[0].get("baseSeverity")

        cwes = []
        for w in cve.get("weaknesses", []):
            for d in w.get("description", []):
                if str(d.get("value", "")).startswith("CWE-"):
                    cwes.append(d["value"])

        desc = next((d["value"] for d in cve.get("descriptions", [])
                     if d.get("lang") == "en"), None)

        # Reference URLs (incl. "Patch"-tagged fix commits) for code mining.
        refs = [{"url": r.get("url"), "tags": r.get("tags") or []}
                for r in cve.get("references", []) if r.get("url")]

        rows.append({
            "cve_id": cve["id"],
            "nvd_published": cve.get("published"),
            "nvd_status": cve.get("vulnStatus"),
            "cvss_score": score,
            "cvss_version": version if score is not None else None,   # 3.1 / 3.0 / 2.0
            "cvss_severity": severity,
            "cwe": ";".join(sorted(set(cwes))) or None,
            "nvd_description": desc,
            "nvd_references": json.dumps(refs),
        })

    df = pd.DataFrame(rows).drop_duplicates("cve_id")
    fsio.write_parquet(INTERIM / "nvd.parquet", df)
    print(f"{len(df)} CVEs" + (f" published since {str(since)[:10]} ({skipped:,} older left out)"
                               if since else ""))
    print(f"{df.cvss_score.notna().mean():.1%} carry a CVSS score")
    print("status distribution:")
    print(df.nvd_status.value_counts().head(10))
    return df


if __name__ == "__main__":
    flatten(harvest())
