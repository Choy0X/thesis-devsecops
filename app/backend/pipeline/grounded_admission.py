"""Training admission for the grounded security corpus (security-grounded-corpus/1).

mined_sft.py admits every mined unit. That teaches the model that every unit of a fix commit is vulnerable, although attribution of the
advisory to the unit is strong for only a quarter of them. These gates keep production units whose change the evidence ties to the advisory.
Tier A also requires a moderate or strong commit link; tier B accepts any link.
"""
from __future__ import annotations

POLICY = 'security-grounded-corpus/1'
STRONG_ATTRIBUTION = ('sole_changed_unit', 'advisory_names_unit', 'advisory_names_file')
KNOWN_LICENCE = ('permissive', 'usable', 'copyleft')


def _flags(pair):
    return pair.get('flags') or {}


def _fix_verified(pair):
    """The link's fix check says verified, or the source has no release data to check against and the advisory declares the commit (grounded_quality.provenance_of)."""
    from app.backend.pipeline import grounded_quality
    return grounded_quality.provenance_of(pair) == 'verified'


_NOISE_SEGMENTS = {'integration', 'examples', 'example', 'docs', 'doc', 'benchmark', 'benchmarks', 'bench', 'e2e', 'fixtures', 'fixture',
                   'dist', 'build', 'vendor', 'node_modules', 'playground', 'demo', 'demos', 'samples', 'sample', 'scripts', 'tools'}


def production_path(path):
    """False for integration projects, examples, docs, benchmarks, generated output and type declarations."""
    path = (path or '').replace('\\', '/').lower()
    parts = path.split('/')
    if any(part in _NOISE_SEGMENTS for part in parts[:-1]):
        return False
    return not path.endswith(('.d.ts', '.min.js', '.min.mjs', '.bundle.js'))


# (funnel name, refusal code, predicate(pair, record) -> keep)
GATES = (
    ('not_test_file', 'test_file', lambda p, r: not _flags(p).get('is_test')),
    ('production_path', 'noise_path', lambda p, r: production_path((p.get('unit') or {}).get('file'))),
    ('not_tangled', 'tangled', lambda p, r: not _flags(p).get('tangled')),
    ('not_quarantined', 'quarantined', lambda p, r: not _flags(p).get('quarantine')),
    ('quality_ok', 'quality_not_ok', lambda p, r: r.get('data_quality_status') == 'ok'),
    ('provenance_verified', 'provenance_not_verified', lambda p, r: r.get('provenance_status') == 'verified'),
    ('licence_known', 'licence_unknown', lambda p, r: r.get('data_usage_status') in KNOWN_LICENCE),
    ('attribution_strong', 'weak_attribution', lambda p, r: (p.get('attribution') or {}).get('basis') in STRONG_ATTRIBUTION),
    ('nontrivial', 'no_change', lambda p, r: ((p.get('extraction') or {}).get('changed_lines') or 0) >= 1),
    ('fix_verified', 'fix_not_verified', lambda p, r: _fix_verified(p)),
)
GATES_IN_ORDER_WITH_ALL = (('all', None),) + tuple((name, code) for name, code, _ in GATES)


def classify(pair, record):
    """Return (tier, None) for an admitted unit or (None, refusal_code) naming the first failing gate."""
    for _, code, keep in GATES:
        if not keep(pair, record):
            return None, code
    level = (pair.get('link') or {}).get('evidence_level')
    return ('A' if level in ('moderate', 'strong') else 'B'), None


def flow_candidate(pair, record):
    """True for a pair that fails ONLY the attribution gate: every gate before it passed and the later ones (a real change, a verified fix) pass too. Such a
    pair is admitted as tier F when the flow analysis proves the weakness and the fix removes it (grounded_units.load_units)."""
    if classify(pair, record) != (None, 'weak_attribution'):
        return False
    return all(keep(pair, record) for name, _, keep in GATES if name in ('nontrivial', 'fix_verified'))


def funnel(items):
    """Cumulative units left after each gate, for (pair, record) items, in gate order."""
    items = list(items)
    rows = [('all', len(items))]
    alive = items
    for name, _, keep in GATES:
        alive = [(p, r) for p, r in alive if keep(p, r)]
        rows.append((name, len(alive)))
    return rows
