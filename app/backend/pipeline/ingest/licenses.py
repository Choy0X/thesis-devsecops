"""Repository licenses: recorded per repository, never assumed.

Mined pairs are third-party code under many licenses. The miner asks GitHub once per repository
(``GET /repos/{owner}/{repo}/license``) and keeps the SPDX id and a coarse status in the
``repositories`` table:

* ``permissive``  MIT, Apache-2.0, BSD, ISC, ... : may be redistributed with the notice;
* ``copyleft``    GPL, AGPL, LGPL, MPL, EPL, ...: conditions apply to redistribution;
* ``unknown``     no license found, ``NOASSERTION`` or an unrecognised one.

This records facts for a later decision; it is not legal advice and it decides nothing about
training. Only ``permissive`` counts as redistributable: a dataset export carries the status so
that restricted or unknown examples can stay unshared, with the reason on record.
"""
from __future__ import annotations

from app.backend.pipeline import codestore, ecosystems

_PERMISSIVE = {
    "mit", "mit-0", "apache-2.0", "bsd-1-clause", "bsd-2-clause", "bsd-3-clause", "bsd-3-clause-clear",
    "0bsd", "isc", "unlicense", "cc0-1.0", "zlib", "bsl-1.0", "postgresql", "python-2.0", "psf-2.0",
    "blueoak-1.0.0", "x11", "artistic-2.0", "wtfpl", "upl-1.0", "ncsa", "libpng-2.0", "cc-by-4.0", "cc-by-3.0"}
_COPYLEFT_PREFIX = ("gpl", "agpl", "lgpl", "mpl", "epl", "eupl", "cddl", "osl", "sspl", "cc-by-sa", "cecill",
                    "ms-rl", "apsl")


def status(spdx: str | None) -> str:
    s = (spdx or "").strip().lower()
    if s in _PERMISSIVE:
        return "permissive"
    if s and s.startswith(_COPYLEFT_PREFIX):
        return "copyleft"
    return "unknown"


def redistributable(spdx: str | None) -> bool:
    return status(spdx) == "permissive"


def for_repository(fetcher, host: str, owner: str, repo: str) -> tuple[str | None, str]:
    """(SPDX id, status) of a repository: from the store when seen, else one GitHub request."""
    from app.backend.pipeline.ingest import codemine
    identity = ecosystems.repository_identity(f"https://{host}/{owner}/{repo}")
    if not identity:
        return None, "unknown"
    known = codestore.repository(identity)
    if known:
        return known["license_spdx"], known["data_usage_status"]
    if host != "github.com":
        return None, "unknown"
    facts = getattr(fetcher.s, 'repository_facts', None)
    fact = facts.get(owner, repo, 'license') if facts is not None else None
    if fact is not None and fact.valid:
        spdx = fact.value
    else:
        r = fetcher.s.get(f"{codemine.GITHUB_API}/repos/{owner}/{repo}/license", timeout=(15, 60))
        limited = codemine._rate_limit(r)
        if limited:
            raise limited
        if facts is not None:
            facts.record_rest(owner, repo, 'license', r)
        spdx = None
        if r.status_code == 200:
            spdx = ((r.json() or {}).get("license") or {}).get("spdx_id")
    st = status(spdx)
    codestore.save_repository(identity, host, owner, repo, spdx, st)
    return spdx, st
