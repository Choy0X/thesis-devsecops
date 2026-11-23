"""Assemble grounded units from the mining store.

A unit is one admitted function or hunk pair with everything mining captured that the v29 export dropped: the fix commit message, the advisory
text, the full file at the vulnerable revision (for clean neighbours) and the test changes of the same commit (unexecuted candidates).
"""
from __future__ import annotations

import collections
import re

from app.backend.pipeline import ecosystems
from app.backend.pipeline import grounded_admission as admission
from app.backend.pipeline import grounded_exec, grounded_recovery


UNIT_GRANULARITIES = ('function', 'hunk')


def _scan(reader, records, flow=False, recover=False):
    """One pass over code_pairs: classify record-backed pairs, remember file pairs and test pairs per commit."""
    candidates, file_pairs, test_pairs, pairs = [], {}, collections.defaultdict(list), []
    for pair in reader.docs('code_pairs'):
        unit = pair.get('unit') or {}
        commit = pair.get('commit_key')
        if unit.get('granularity') == 'file':
            file_pairs[(commit, unit.get('file'))] = pair['_id']
        if (pair.get('flags') or {}).get('is_test') and ((pair.get('extraction') or {}).get('changed_lines') or 0) > 0:
            test_pairs[commit].append((pair['_id'], unit.get('file')))
        record = records.get(pair.get('record_id'))
        if record is None or unit.get('granularity') not in UNIT_GRANULARITIES:
            continue                       # a file-level pair is only the source of a file's text and header, never a unit
        pairs.append((pair, record))
        tier, _ = admission.classify(pair, record)
        if not tier and recover:
            tier, _ = grounded_recovery.classify_recovery(pair, record)        # 'A'/'B' by the licence policy, 'R?' when an evidence tier must decide
        if tier:
            candidates.append((pair, record, tier))
        elif flow and admission.flow_candidate(pair, record):
            candidates.append((pair, record, 'F?'))         # attribution is weak; admitted below only if the flow proof holds
    return candidates, file_pairs, test_pairs, pairs


def admission_report(reader, records):
    """Funnel, tier counts and refusal reasons for every record-backed pair, without loading any code text."""
    _, _, _, pairs = _scan(reader, records)
    refused, tiers = collections.Counter(), collections.Counter()
    for pair, record in pairs:
        tier, reason = admission.classify(pair, record)
        if tier:
            tiers[tier] += 1
        else:
            refused[reason] += 1
    return {'funnel': admission.funnel(pairs), 'tiers': {'A': tiers['A'], 'B': tiers['B']}, 'refused': dict(refused)}


RECOVERY_LANGUAGES = ('python', 'javascript', 'typescript')
FLOW_PROOF_DISTANCE = 6
_GUARD_CALL = re.compile(r'\b\w*(?:escape|sanitiz|valid|verify|assert|check|allow|safe|quote|basename|encode)\w*\s*\(', re.I)


def flow_proof(before, after, language, cwes):
    """(class, flow) when the first supported class of the advisory has a flow in the vulnerable code that the fix removes AND the change sits at that flow (within a few
    lines of the sink, or adds a sanitiser or validator call); else None. This is what lets a weakly attributed pair stand as evidence: the code itself shows the fix."""
    from app.backend.pipeline import grounded_flow, grounded_views
    cwe = grounded_views.known_cwe({'cwe': cwes})
    if not cwe or not before or not after or len(before) > grounded_views.MAX_CODE_CHARS:
        return None
    flow = grounded_flow.confirmed_flow(before, after, language, cwe)
    if flow is None:
        return None
    positions = grounded_views._fix_positions(before, after)
    sink_index = flow['sink_line_no'] - 1
    near = bool(positions) and min(abs(sink_index - p) for p in positions) <= FLOW_PROOF_DISTANCE
    added, _ = grounded_views._changed_lines(before, after)
    guards = any(_GUARD_CALL.search(line) for line in added)
    return (cwe, flow) if near or guards else None


def _recovery_tier(pair, code, record, tiers):
    """(tier, basis, message evidence) for a pair that fails only soft gates, by the first evidence that holds among the asked-for tiers: F (flow proof), M (the commit
    message names the unit), D (the fix adds a guard), L? (a candidate for the LLM attribution pass); None when none is asked for or holds."""
    language = (pair.get('unit') or {}).get('language')
    cwes = list((pair.get('subject') or {}).get('cwe') or [])
    if language not in RECOVERY_LANGUAGES:
        return None                                             # the flow checks and the view builders only exist for these languages
    if 'F' in tiers and flow_proof(code.get('before') or '', code.get('after') or '', language, cwes) is not None:
        return 'F', 'flow_proof', {}
    if 'M' in tiers and grounded_recovery.message_tier(pair):
        return 'M', 'message_named', {'message_evidence': {'basis': pair['attribution']['basis']}}
    diff = grounded_recovery.diff_tier(pair, code) if 'D' in tiers else None
    if diff is not None:
        return 'D', 'diff_grounded', {'diff_grounded': diff, 'origin': 'recovered_D'}
    if 'L?' in tiers:
        return 'L?', 'llm_candidate', {}
    return None


def _read_class_from_text(unit):
    """A package-less source (the Linux kernel) lists no CWE: name the class from the words of the fix (grounded_textclass) and say so (`class_basis: text_rule`). Every other
    unit keeps the classes of its advisory (`class_basis: advisory`), unchanged."""
    adapter = ecosystems.get(unit.get('ecosystem'))
    if unit.get('cwe') or adapter is None or not adapter.package_less:
        return
    from app.backend.pipeline import grounded_textclass
    advisory = unit.get('advisory') or {}
    found = grounded_textclass.classes_from_text(unit.get('commit_message'), advisory.get('summary'), advisory.get('details'))
    if found:
        unit['cwe'] = found
        unit['class_basis'] = 'text_rule'


def load_units(reader, records, *, tiers=('A', 'B'), limit=None, recover=False, only_ids=None):
    """Admitted units in store order. `records` maps record_id to its quality fields and issue_group_id (grounded_quality.derive, or the old v29 export).

    Tier "F" (flow proof) admits a pair that fails only the attribution gate when flow_proof holds; it is labelled `admission: flow_proof` and carries the tier F."""
    candidates, file_pairs, test_pairs, _ = _scan(reader, records, flow='F' in tiers, recover=recover)
    messages = {doc['commit_key']: ((doc.get('git') or {}).get('message') or '') for doc in reader.docs('fix_commits')}
    advisories = {}
    file_texts = {}
    out = []
    for pair, record, tier in candidates:
        if tier == 'F?':
            if 'F' not in tiers:
                continue
        elif tier != 'R?' and tier not in tiers:
            continue
        if only_ids is not None and pair['pair_key'] not in only_ids:
            continue
        code = reader.get('pair_code', pair['_id'])
        if code is None:
            continue
        admission_basis = 'attribution'
        extra = {}
        if tier in ('A', 'B') and admission.classify(pair, record)[0] is None:
            admission_basis = 'licence_policy'                       # only the licence gate failed; unknown licences are admitted by the owner's policy
        if tier == 'R?':
            chosen = _recovery_tier(pair, code, record, tiers)
            if chosen is None:
                continue
            tier, admission_basis, extra = chosen
        if tier == 'F?':
            language = (pair.get('unit') or {}).get('language')
            if flow_proof(code.get('before') or '', code.get('after') or '', language, list((pair.get('subject') or {}).get('cwe') or [])) is None:
                continue
            tier, admission_basis = 'F', 'flow_proof'
        unit = pair.get('unit') or {}
        subject = pair.get('subject') or {}
        commit = pair.get('commit_key')
        advisory_id = subject.get('advisory_id')
        if advisory_id not in advisories:
            doc = reader.get('advisories', advisory_id) or {}
            advisories[advisory_id] = {'key': doc.get('key'), 'summary': doc.get('summary') or '', 'details': doc.get('details') or ''}
        slim = tier == 'L?' and only_ids is None          # a candidate for the LLM pass: the file text and the tests are loaded only for the units that are picked
        file_text = None
        file_id = file_pairs.get((commit, unit.get('file')))
        if file_id is not None and not slim:
            if file_id not in file_texts:                                  # units of one file share one copy of its text
                file_code = reader.get('pair_code', file_id)
                file_texts[file_id] = None if file_code is None else file_code.get('before')
            file_text = file_texts[file_id]
        tests = []
        for test_id, test_file in ([] if slim else test_pairs.get(commit, [])):
            test_code = reader.get('pair_code', test_id)
            if test_code and (test_code.get('after') or '').strip():
                tests.append({'file': test_file, 'after': test_code['after'], 'diff': test_code.get('diff') or ''})
        out.append({
            'unit_id': pair['pair_key'], 'pair_id': pair['_id'], 'record_id': pair['record_id'], 'tier': tier,
            'commit_key': commit, 'repo_url': pair.get('repo_url'),
            'package': ecosystems.project_key(subject.get('ecosystem'), subject.get('package'), pair.get('repo_url'), unit.get('file')),        # a package-less source has no package: its project is the key
            'cve_id': subject.get('cve_id'), 'cwe': list(subject.get('cwe') or []), 'ecosystem': subject.get('ecosystem'),
            'file': unit.get('file'), 'language': unit.get('language'), 'granularity': unit.get('granularity'), 'span': unit.get('span'),
            'before': code.get('before') or '', 'after': code.get('after') or '', 'diff': code.get('diff') or '',
            'file_before': file_text, 'commit_message': messages.get(commit, ''), 'advisory': advisories[advisory_id],
            'attribution': (pair.get('attribution') or {}).get('basis'), 'link_level': (pair.get('link') or {}).get('evidence_level'),
            'issue_group_id': record.get('issue_group_id'), 'tests': tests,
            'exec_context': grounded_exec.import_header(file_text, unit.get('language')), 'admission': admission_basis,
            'licence_status': record.get('data_usage_status'),
        })
        out[-1].update(extra)
        _read_class_from_text(out[-1])
        if slim:
            out[-1]['slim'] = True
        if limit and len(out) >= limit:
            break
    if recover:
        from app.backend.pipeline import grounded_guard
        narrow = {u['unit_id'] for u in grounded_guard.limit_commit_breadth(out)}           # a commit that changes many functions singles out none of them
        out = [u for u in out if u['tier'] in ('A', 'B', 'F') or u['unit_id'] in narrow]
    return out


_TEST_UTILITY = re.compile(r'(^|/)(assert\w*|snapshot\w*|mocks?|fixtures?|testing|test[-_]?utils?|__\w+__|e2e|stubs?|helpers?)(/|\.|$)', re.I)
TWIN_CLASS_SHARE = 0.4          # no weakness class may be more than this share of the twins (prototype-pollution guards are very common)


def _normal_hash(code):
    import hashlib
    return hashlib.sha256(' '.join((code or '').split()).encode()).hexdigest()


def load_twin_units(reader, *, exclude=(), limit=None, languages=('javascript', 'typescript', 'python'), cache=None):
    """Vulnerable twins made by removing one protection from real production functions (see grounded_mutate).

    The original function is the fixed side (`after`), the edited function the vulnerable side (`before`). Every unit says `origin: sanitiser_removal`. Code that
    equals a real unit's code (`exclude`: normalised hashes) is skipped, identical functions are used once, and no class may exceed TWIN_CLASS_SHARE of the twins.
    """
    import collections
    import hashlib
    from app.backend.pipeline import grounded_mutate
    seen, made = set(exclude), []
    file_pairs = {}
    for pair in reader.docs('code_pairs'):
        file_unit = pair.get('unit') or {}
        if file_unit.get('granularity') == 'file':
            file_pairs[(pair.get('commit_key'), file_unit.get('file'))] = pair['_id']
    headers = {}
    for pair in reader.docs('code_pairs'):
        unit = pair.get('unit') or {}
        if unit.get('granularity') != 'function' or unit.get('language') not in languages:
            continue
        if (pair.get('flags') or {}).get('is_test') or not admission.production_path(unit.get('file') or '') or _TEST_UTILITY.search(unit.get('file') or ''):
            continue
        subject = pair.get('subject') or {}
        if not subject.get('package'):
            continue
        code = reader.get('pair_code', pair['_id'])
        if not code:
            continue
        for side in ('before', 'after'):
            text = code.get(side) or ''
            if not text.strip() or len(text) > 4000:
                continue
            key = _normal_hash(text)
            if key in seen:
                continue
            seen.add(key)
            file_id = file_pairs.get((pair.get('commit_key'), unit.get('file')))
            if (file_id, side) not in headers:
                file_code = reader.get('pair_code', file_id) if file_id is not None else None
                headers[(file_id, side)] = grounded_exec.import_header((file_code or {}).get(side), unit['language'])
            for twin in (cache.twins(text, unit['language']) if cache is not None else grounded_mutate.make_twins(text, unit['language'])):
                uid = 'syn:' + hashlib.sha256(f"{pair['pair_key']}|{side}|{twin['cwe']}".encode()).hexdigest()[:20]
                made.append({
                    'unit_id': uid, 'pair_id': pair['_id'], 'record_id': uid, 'tier': 'S', 'commit_key': 'syn:' + str(unit.get('file')),
                    'repo_url': pair.get('repo_url'), 'package': subject.get('package'), 'cve_id': '', 'cwe': [twin['cwe']], 'ecosystem': subject.get('ecosystem'),
                    'file': unit.get('file'), 'language': unit['language'], 'granularity': 'function', 'span': unit.get('span'),
                    'before': twin['mutated'], 'after': twin['original'], 'diff': '', 'file_before': None, 'commit_message': '', 'advisory': {},
                    'attribution': None, 'link_level': None, 'issue_group_id': f"syn:{subject.get('package')}:{unit.get('file')}", 'tests': [],
                    'origin': 'unsafe_substitution' if twin['kind'] == 'substitution' else 'sanitiser_removal', 'exec_context': headers[(file_id, side)], 'removed': twin['removed'], 'edit_kind': twin['kind'], 'flow_reason': twin['flow']['reason']})
    if cache is not None:
        cache.save()
    made.sort(key=lambda u: hashlib.sha256(u['unit_id'].encode()).hexdigest())
    if len(made) > 3:
        ceiling = max(1, int(len(made) * TWIN_CLASS_SHARE))
        per_class, balanced = collections.Counter(), []
        for unit in made:
            cwe = unit['cwe'][0]
            if per_class[cwe] < ceiling:
                per_class[cwe] += 1
                balanced.append(unit)
        made = balanced
    return made[:limit] if limit else made
