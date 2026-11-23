"""Quality fields of a code pair, derived from the mining store itself.

The grounded admission gates read three fields per pair (data quality, provenance, licence usage) and the builder reads an issue group. They used to come from a
frozen export (dataset_v29 records.parquet), which was built before later fix checks, re-splits and licence lookups: it labelled 2,476 pairs of verified fixes
"unknown", and it knows nothing of pairs mined afterwards. The store holds the facts, so this module computes the same fields from them:

* data_quality_status: "excluded" for a test or tangled pair, "quarantined" when the pair carries a quarantine reason, else "ok" (build_reviewed's rule, minus the old
  graph quarantine, which the current split manifests no longer produce);
* provenance_status: the fix check of the pair's link ("verified", "suspect", "unknown");
* data_usage_status: the repository licence usage ("permissive", "copyleft", "unknown");
* issue_group_id: the leakage group of the pair's advisory in the newest leakage graph, else a group of the advisory itself, else of the pair.

The result is a dict record_id -> fields, the shape grounded_units already consumes. Read-only on the store.
"""
from __future__ import annotations

import sqlite3

from app.backend.pipeline import ecosystems, grounded_store


def _docs(reader, collection):
    """The documents of a collection, or none when the store has no such collection (a young store has no leakage groups yet)."""
    try:
        yield from reader.docs(collection)
    except sqlite3.OperationalError:
        return


def repository_identity(url):
    """host/owner/name in lower case, the key of the repositories collection."""
    text = (url or '').split('://')[-1].strip('/').lower()
    return text[:-4] if text.endswith('.git') else text


def newest_graph_groups(reader):
    """{advisory key: group id} of the newest leakage graph version in the store ({} when the store holds none)."""
    versions = {}
    for doc in _docs(reader, 'leakage_groups'):
        versions.setdefault(doc.get('graph_version'), {})[doc.get('node_id')] = doc.get('group_id')
    if not versions:
        return {}
    newest = max(versions, key=lambda v: (len(str(v)), str(v)))                 # v7 after v6 ... v10 after v9
    return versions[newest]


def quality_of(flags):
    flags = flags or {}
    if flags.get('is_test') or flags.get('tangled'):
        return 'excluded'
    return 'quarantined' if flags.get('quarantine') else 'ok'


def provenance_of(pair):
    """The fix check of the pair's link ("verified", "suspect", "unknown"). A package-less source (the Linux kernel: git ranges, no packages) has no release dates to check a commit
    against, so its check always ends "unknown". For such a source the advisory's own declaration is the proof: the link counts as verified when the check was "unknown" only
    for want of release data and the link evidence is strong (an OSV GIT range or fix reference, or a message that names the advisory). A suspect verdict is never overridden."""
    link = pair.get('link') or {}
    verdict = link.get('fix_check') or 'unknown'
    if verdict == 'unknown' and link.get('evidence_level') == 'strong':
        adapter = ecosystems.get((pair.get('subject') or {}).get('ecosystem'))
        if adapter is not None and adapter.package_less:
            return 'verified'
    return verdict


def _advisory_key(reader, advisory):
    if advisory is None:
        return None
    try:
        return (reader.get('advisories', advisory) or {}).get('key')
    except sqlite3.OperationalError:
        return None


def derive(reader):
    """{record_id: {'record_id', 'issue_group_id', 'data_quality_status', 'provenance_status', 'data_usage_status'}} for every code pair of the store."""
    usage = {doc['_id']: (doc.get('license') or {}).get('usage') for doc in _docs(reader, 'repositories')}
    groups = newest_graph_groups(reader)
    keys = {}
    out = {}
    for pair in reader.docs('code_pairs'):
        record_id = pair.get('record_id')
        if record_id is None:
            continue
        advisory = (pair.get('subject') or {}).get('advisory_id')
        if advisory not in keys:
            keys[advisory] = _advisory_key(reader, advisory)
        key = keys[advisory]
        group = groups.get(key) or (f'adv:{key}' if key else f'pair:{record_id}')
        out[record_id] = {'record_id': record_id, 'issue_group_id': group, 'data_quality_status': quality_of(pair.get('flags')),
                          'provenance_status': provenance_of(pair),
                          'data_usage_status': usage.get(repository_identity(pair.get('repo_url'))) or 'unknown'}
    return out


def compare(derived, reference):
    """Per field, how many shared records agree, and the most common disagreements (want, got): the evidence that a derived source may replace a frozen export."""
    import collections
    fields = ('issue_group_id', 'data_quality_status', 'provenance_status', 'data_usage_status')
    shared = [rid for rid in reference if rid in derived]
    agree = {f: sum(reference[r].get(f) == derived[r].get(f) for r in shared) for f in fields}
    differences = {f: collections.Counter((reference[r].get(f), derived[r].get(f)) for r in shared if reference[r].get(f) != derived[r].get(f)).most_common(6)
                   for f in fields if f != 'issue_group_id'}
    return {'shared': len(shared), 'agree': agree, 'differences': differences}


def from_store(path):
    with grounded_store.open_store(path) as reader:
        return derive(reader)
