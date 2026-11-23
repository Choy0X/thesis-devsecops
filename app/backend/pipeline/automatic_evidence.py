"""Automatic evidence policy 2. Drafts and imported JSON cannot establish proof."""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import pathlib
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar

from . import automatic_admission as legacy, evidence_ast, evidence_rules
from .explanation_quality import context_suggestions

POLICY = 'automatic-evidence/2'
CURRENT_POLICY = 'automatic-evidence/3'
SOURCE_FOUNDATION_POLICY = 'backend-source-foundation/1'
_SOURCE_ROOT = ContextVar('automatic_evidence_source_root', default=None)
_SOURCE_SCHEMA = 'npm-exact-source-binding/2'
_SOURCE_BASIS = 'backend_exact_git_parent_tree_blob_package_permission_unit'
_ACQUIRED_SCHEMA = 'npm-acquired-source-binding/1'
_ACQUIRED_BASIS = 'backend_acquired_parent_snapshots_root_package_permission_unit'
_FIELDS = (*legacy._TARGETS, 'supported_impact', 'evidence_summary', 'label_basis')


@functools.lru_cache(maxsize=8)
def _registry_hash(path, modified, size):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def _registry():
    from . import evidence_shell,evidence_recursive,evidence_exports,evidence_source_scope,npm_case_collector,npm_package_scope,evidence_source_supersession,npm_evidence_sources
    from .verify import automatic, closure_diagnostics, guard_facts
    files = {}
    for name, module in (('rules', evidence_rules), ('shell_rules', evidence_shell), ('recursive_rules',evidence_recursive), ('export_rules',evidence_exports), ('source_scope', evidence_source_scope), ('guard_facts', guard_facts), ('legacy_rule', legacy), ('closure_diagnostics', closure_diagnostics), ('closure_adapter', automatic)):
        path = pathlib.Path(module.__file__)
        stat = path.stat()
        files[name] = _registry_hash(str(path), stat.st_mtime_ns, stat.st_size)
    path=evidence_recursive.FORMS
    stat=path.stat()
    files['recursive_forms']=_registry_hash(str(path),stat.st_mtime_ns,stat.st_size)
    path = pathlib.Path(npm_case_collector.__file__)
    stat = path.stat()
    files['collector_source_binding'] = _registry_hash(str(path), stat.st_mtime_ns, stat.st_size)
    path = pathlib.Path(npm_package_scope.__file__)
    stat = path.stat()
    files['collector_package_scope'] = _registry_hash(str(path), stat.st_mtime_ns, stat.st_size)
    path = pathlib.Path(evidence_source_supersession.__file__)
    stat = path.stat()
    files['source_supersession'] = _registry_hash(str(path), stat.st_mtime_ns, stat.st_size)
    path = pathlib.Path(npm_evidence_sources.__file__)
    stat = path.stat()
    files['acquired_source_binding'] = _registry_hash(str(path), stat.st_mtime_ns, stat.st_size)
    path = pathlib.Path(__file__)
    stat = path.stat()
    files['source_foundation_implementation'] = _registry_hash(str(path), stat.st_mtime_ns, stat.st_size)
    return {'version': evidence_rules.VERSION, 'source_foundation_policy': SOURCE_FOUNDATION_POLICY, **files}


@contextmanager
def source_scope(root):
    """Select one backend source ledger without mutating global workspace paths."""
    token = _SOURCE_ROOT.set(pathlib.Path(root).resolve() if root is not None else None)
    try:
        yield
    finally:
        _SOURCE_ROOT.reset(token)


def source_foundation(record, *, root=None):
    """Re-derive source authenticity, never trust an imported provenance claim.

    This is not G0 release plausibility, advisory attribution or a security label.
    Known suspect legacy links cannot enter the unknown-link collector route.
    """
    if record.get('provenance_status') != 'unknown':
        return None
    from . import npm_case_collector
    lookup = getattr(npm_case_collector, 'source_binding_for', None)
    selected_root = root if root is not None else _SOURCE_ROOT.get()
    fact = lookup(record, root=selected_root) if lookup is not None else None
    if fact is None:
        fact = _acquired_foundation(record, root=selected_root)
    if (not isinstance(fact, dict) or (fact.get('schema'), fact.get('basis')) not in (
                (_SOURCE_SCHEMA, _SOURCE_BASIS), (_ACQUIRED_SCHEMA, _ACQUIRED_BASIS))
            or fact.get('status') != 'verified' or fact.get('advisory_fix_status') != 'unknown'
            or fact.get('record_id') != record.get('record_id')
            or any(not isinstance(fact.get(key), str) or re.fullmatch(r'[0-9a-f]{64}', fact[key]) is None
                   for key in ('artifact_sha256', 'source_sha256', 'binding_sha256'))):
        return None
    try:
        expected = hashlib.sha256(json.dumps({key: value for key, value in fact.items() if key != 'binding_sha256'},
            sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        return None
    return copy.deepcopy(fact) if fact['binding_sha256'] == expected else None


def _acquired_foundation(record, *, root=None):
    """Authenticate a narrow root-file pair through backend acquisition authority.

    A committed production-fetch attempt attests its checked parent relationship.
    Its exact transports, member, snapshot bytes, root package and pinned notices
    are revalidated here. Nested ownership and callback-only artifacts remain out.
    Imported context and self-consistent artifact JSON cannot issue this authority.
    """
    from . import npm_evidence_sources as sources
    try:
        if record.get('ecosystem') != 'npm' or record.get('provenance_status') != 'unknown':
            return None
        if record.get('data_usage_status') not in (None, 'unknown', 'usable'):
            return None
        package = record.get('package_identity')
        if not isinstance(package, str) or not package.startswith('npm:') or not package[4:]:
            return None
        identity = {'repository_url': sources._repository(record.get('repository_identity') or record.get('repo_url')),
                    'before_revision': record.get('relevant_parent_commit'), 'after_revision': record.get('fix_commit'),
                    'before_path': sources._path(record.get('file_path_before')),
                    'after_path': sources._path(record.get('file_path_after'))}
        if (any('/' in identity[side + '_path'] for side in ('before', 'after'))
                or identity['before_revision'] == identity['after_revision']
                or any(not isinstance(identity[side + '_revision'], str)
                       or not sources._REF.fullmatch(identity[side + '_revision']) for side in ('before', 'after'))):
            return None
        root = sources._root(root)
        ledger = root / '.npm_evidence_sources.sqlite3'
        if not ledger.is_file():
            return None
        source_id = sources._digest({'policy': sources.POLICY, **identity})
        with sqlite3.connect(ledger.as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            work = db.execute("SELECT * FROM source_work WHERE source_id=? AND state='acquired' AND acquisition_policy=?",
                              (source_id, sources.POLICY)).fetchone()
            if work is None or json.loads(work['identity']) != identity:
                return None
            member = db.execute('SELECT fingerprint,body FROM source_records WHERE source_id=? AND record_id=?',
                                (source_id, record.get('record_id'))).fetchone()
            if member is None:
                return None
            original = json.loads(member['body'])
            member_fields = ('record_id', 'raw_pair_key', 'ecosystem', 'language', 'vulnerable_code', 'fixed_code',
                             'package_identity', 'relevant_parent_commit', 'fix_commit', 'file_path_before', 'file_path_after')
            if (sources._digest(original) != member['fingerprint']
                    or any(original.get(key) != record.get(key) for key in member_fields)):
                return None
            artifact_sha = work['artifact_sha256']
            envelope = sources._artifact(root, artifact_sha)
            if (not isinstance(envelope, dict) or envelope.get('schema') != 'npm-source-artifact/1'
                    or envelope.get('policy') != sources.POLICY or envelope.get('source_id') != source_id
                    or envelope.get('input_fingerprint') != work['input_fingerprint']
                    or envelope.get('admission_status') != 'not_evaluated'):
                return None
            issued = db.execute("SELECT 1 FROM source_attempts WHERE source_id=? AND fingerprint=? AND state='acquired' "
                                "AND json_valid(result) AND json_extract(result,'$.artifact_sha256')=? LIMIT 1",
                                (source_id, work['input_fingerprint'], artifact_sha)).fetchone()
            if issued is None:
                return None
            payload = envelope['payload']
            bounds = {'should_stop': lambda: False, 'deadline': time.monotonic() + 60,
                      'max_blob_bytes': 1 << 20, 'max_files': 64, 'max_regressions': 8,
                      'wave_bytes': sources.MAX_ARTIFACT_BYTES, 'remaining_bytes': sources.MAX_ARTIFACT_BYTES}
            sources._validate({**identity, 'records': [record]}, payload, bounds)
            ancestry = (payload.get('provenance') or {}).get('metadata') or {}
            blob_origins = (payload.get('provenance') or {}).get('blobs') or {}
            bindings = {}
            for side, code_key in (('before', 'vulnerable_code'), ('after', 'fixed_code')):
                revision, path = identity[side + '_revision'], identity[side + '_path']
                origin = ancestry.get(side) or {}
                expected_metadata = (identity['repository_url'] + '/commit/' + revision if origin.get('transport') == 'git_snapshot'
                    else 'https://api.github.com/repos/' + identity['repository_url'].removeprefix('https://github.com/') + '/git/commits/' + revision)
                if (origin.get('transport') not in ('git_snapshot', 'github_api') or origin.get('revision') != revision
                        or origin.get('source_url') != expected_metadata):
                    return None
                files = payload['snapshots'][side]['files']
                manifest = json.loads(files['package.json'])
                if not isinstance(manifest, dict) or manifest.get('name') != package[4:]:
                    return None
                required = {path, 'package.json', *(notice for notice in sources._NOTICES if files.get(notice))}
                for logical in required:
                    text = files[logical]
                    detail = envelope['files'][side][logical]
                    blob = blob_origins.get(side, {}).get(logical) or {}
                    if blob.get('transport') == 'git_snapshot':
                        if blob.get('revision') != revision or blob.get('path') != logical:
                            return None
                    elif blob.get('transport') == 'github_raw':
                        from urllib.parse import quote
                        expected_url = 'https://raw.githubusercontent.com/' + identity['repository_url'].removeprefix('https://github.com/') + '/' + revision + '/' + quote(logical, safe='/')
                        if blob.get('source_url') != expected_url:
                            return None
                    else:
                        return None
                    raw = text.encode()
                    indexed = db.execute('SELECT * FROM exact_source_files WHERE repository=? AND revision=? AND path=?',
                        (identity['repository_url'].removeprefix('https://').lower(), revision, logical)).fetchone()
                    # This global index deduplicates by repository/revision/path.
                    # Another acquired task may repoint identical shared bytes;
                    # authority still comes from this task's own fenced artifact.
                    if (indexed is None or indexed['content_sha256'] != hashlib.sha256(raw).hexdigest() or indexed['bytes'] != len(raw)
                            or detail.get('sha256') != indexed['content_sha256'] or detail.get('bytes') != len(raw)
                            or detail.get('acquisition') != blob):
                        return None
                raw, target = files[path].encode(), record[code_key].encode()
                context = legacy._json(record.get('context_' + side) or '{}', {})
                snapshot = context.get('snapshot') or {}
                module_sha, target_sha = hashlib.sha256(raw).hexdigest(), hashlib.sha256(target).hexdigest()
                if (any(snapshot.get(key) is not None and snapshot[key] != value for key, value in
                        (('revision', revision), ('side', side), ('path', path), ('file_sha256', module_sha), ('target_sha256', target_sha)))
                        or (snapshot.get('files') or {}).get(path, module_sha) != module_sha
                        or 'target_source_mismatch' in context.get('omissions', [])):
                    return None
                span = legacy._json(record.get('byte_span_' + side), None)
                if span is None:
                    if raw.count(target) != 1:
                        return None
                    start = raw.index(target); span = [start, start + len(target)]
                if (not isinstance(span, list) or len(span) != 2 or any(type(value) is not int for value in span)
                        or not 0 <= span[0] <= span[1] <= len(raw) or raw[span[0]:span[1]] != target):
                    return None
                blobs = legacy._json(record.get('file_blob_hashes'), {}) or {}
                git_blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
                if blobs.get(side) is not None and blobs[side] != git_blob:
                    return None
                bindings[side] = {'revision': revision, 'path': path, 'file_sha256': module_sha,
                                  'target_sha256': target_sha, 'byte_span': span, 'permission': payload['permissions'][side]}
        fact = {'schema': _ACQUIRED_SCHEMA, 'status': 'verified', 'basis': _ACQUIRED_BASIS,
                'record_id': record['record_id'], 'artifact_sha256': artifact_sha, 'source_id': source_id,
                'source_sha256': legacy._digest({'source': source_fingerprint(record), 'package': package, 'bindings': bindings}),
                'advisory_fix_status': 'unknown', 'transport_ancestry': ancestry, 'snapshots': bindings}
        return {**fact, 'binding_sha256': hashlib.sha256(json.dumps(fact, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return None


def _collector_source(record):
    return str(record.get('collector_version') or '').startswith('npm-targeted-case-collector/')


def _source_integrity(record, gate, *, root=None):
    gate = gate.as_dict() if hasattr(gate, 'as_dict') else (gate or {})
    problems = legacy._source_problems(record, gate)
    if record.get('source_revalidation'):
        from . import evidence_source_supersession
        problems.extend(evidence_source_supersession.validate(record, root=root if root is not None else _SOURCE_ROOT.get()))
    if any(not re.fullmatch(r'[0-9a-fA-F]{40}', str(record.get(key) or '')) for key in ('relevant_parent_commit', 'fix_commit')):
        problems.append('invalid_source_revisions')
    if _collector_source(record) and record.get('provenance_status') != 'unknown':
        problems.append('collector_source_provenance_mismatch')
    return problems


def _source_state(record, gate, *, root=None):
    problems = _source_integrity(record, gate, root=root)
    foundation = source_foundation(record, root=root) if 'unverified_fix_source' in problems else None
    return problems, foundation


def analysis_source_problems(record, gate=None, *, root=None):
    """Permit bounded analysis of authenticated collector bytes, not supervision."""
    problems, foundation = _source_state(record, gate, root=root)
    return [problem for problem in problems if problem != 'unverified_fix_source' or foundation is None]


def _proof_metadata(facts=None, executed=None):
    facts = facts or {}
    return {'mechanism': facts.get('mechanism'), 'rule_id': facts.get('rule_id'),
            'proof_method': facts.get('method'), 'repair_valid': bool(facts.get('repair_valid')),
            'trusted_execution_id': (executed or {}).get('receipt_id')}


def _json(value, fallback):
    return legacy._json(value, fallback)


def source_fingerprint(record):
    value = {'code_and_context': legacy.source_fingerprint(record),
        **{k: record.get(k) for k in ('repository_identity', 'ecosystem', 'cwe_ids', 'source_record_hashes',
                                     'provenance_status', 'collector_version', 'file_path_before', 'file_path_after', 'file_blob_hashes')}}
    if record.get('source_revalidation') is not None:
        value['source_revalidation'] = record['source_revalidation']
    return legacy._digest(value)


def target_fingerprint(record):
    return legacy._digest({key: _json(record.get(key), None) if key in ('evidence_spans', 'fixed_review') else record.get(key)
                           for key in _FIELDS})


def _source_rule(record):
    facts = evidence_rules.examine(record)
    if facts:
        return facts
    old = legacy._prototype_filter(record, parser=evidence_ast.parse)
    if old:
        return {**old, 'method': 'static_closed_world', 'mechanism': 'prototype_pollution',
                'rule_id': 'prototype-key-filter/1', 'repair_valid': True,
                'evidence_spans': _json(old['evidence_spans'], []), 'fixed_review': _json(old['fixed_review'], {})}
    from .verify import guard_facts
    return guard_facts.examine(record, 'prototype_pollution')


def _execution(record):
    from .verify.automatic import trusted_receipt_for
    receipt = trusted_receipt_for(record)
    if not receipt or receipt.get('verdict') != 'verified':
        return None, None
    facts = receipt.get('facts') or {}
    effect = facts.get('effect') or {}
    if facts.get('repair_valid') is not True or not all(effect.get(k) is True for k in ('before_unsafe', 'after_safe', 'benign_preserved')):
        return None, None
    return {**facts, 'method': 'trusted_execution', 'rule_id': 'runtime-' + str(facts.get('mechanism')),
            'repair_valid': True}, receipt


def _target(facts):
    invariant = facts.get('violated_invariant')
    safeguard = facts.get('decisive_safeguard') or (facts.get('fixed_review') or {}).get('safeguard')
    fixed = facts.get('fixed_review')
    if not fixed:
        fixed = {'scope': facts.get('condition_scope') or facts.get('scope'), 'safeguard': safeguard,
                 'evidence': facts.get('fixed_evidence') or []}
    return {'label_state': 'vulnerability_supported', 'violated_invariant': invariant,
            'attacker_control': facts.get('attacker_control'), 'preconditions': facts.get('preconditions') or [],
            'evidence_spans': json.dumps(facts.get('evidence_spans') or []),
            'condition_scope': facts.get('condition_scope') or facts.get('scope'),
            'decisive_safeguard': safeguard, 'fixed_review': json.dumps(fixed),
            'behavioral_intent': facts.get('behavioral_intent'), 'missing_facts': [],
            'supported_impact': facts.get('supported_impact'), 'evidence_summary': invariant,
            'label_basis': 'automatic_mechanism:' + facts['rule_id']}


def _missing_target(missing):
    target = {key: [] if key in ('preconditions', 'missing_facts') else None for key in _FIELDS}
    return {**target, 'label_state': 'insufficient_context', 'missing_facts': missing,
            'label_basis': 'automatic_decisive_missing_facts'}


def receipt_problems(record, *, root=None):
    if record.get('admission_policy') == 'automatic-evidence/3':
        from .automatic_evidence_v3 import receipt_problems as validate_v3
        return validate_v3(record, root=root)
    receipt = _json(record.get('admission_provenance'), {})
    if not isinstance(receipt, dict) or receipt.get('policy') != POLICY:
        return ['missing_automatic_admission_receipt']
    problems = []
    if receipt.get('source_sha256') != source_fingerprint(record):
        problems.append('automatic_source_fingerprint_mismatch')
    if receipt.get('target_sha256') != target_fingerprint(record):
        problems.append('automatic_target_fingerprint_mismatch')
    if record.get('admission_outcome') != receipt.get('outcome'):
        problems.append('automatic_outcome_mismatch')
    if record.get('review_status') != 'auto_checked':
        problems.append('automatic_review_status_mismatch')
    if receipt.get('grammar') != evidence_ast.grammar_signature() or receipt.get('rule_registry') != _registry():
        problems.append('automatic_proof_implementation_mismatch')
    foundation = source_foundation(record, root=root)
    if foundation and foundation.get('schema') == _ACQUIRED_SCHEMA:
        foundation = None  # This historical reader has no policy-3 module proof.
    if record.get('provenance_status') == 'unknown' or _collector_source(record):
        gate = {'failed': [reason for reason in record.get('admission_reasons', []) if reason in legacy._HARD_GATE]}
        integrity = _source_integrity(record, gate, root=root)
        if (foundation is None or receipt.get('source_foundation') != foundation
                or receipt.get('revalidated_source_checks') != ['unverified_fix_source']
                or receipt.get('repair_valid') is not True
                or set(integrity) - {'unverified_fix_source', 'attributed', 'link_evidence'}):
            problems.append('automatic_source_foundation_mismatch')
    elif receipt.get('source_foundation') is not None or receipt.get('revalidated_source_checks') not in (None, []):
        problems.append('automatic_source_foundation_mismatch')
    expected_outcome = {'source_rule': 'admitted', 'trusted_execution': 'admitted',
                        'decisive_missing_facts': 'insufficient_context'}.get(receipt.get('basis'))
    if expected_outcome is not None and receipt.get('outcome') != expected_outcome:
        problems.append('automatic_proof_outcome_mismatch')
    if receipt.get('basis') == 'source_rule':
        facts = _source_rule(record)
        if not facts or any(record.get(k) != value for k, value in _target(facts).items()):
            problems.append('automatic_rule_target_mismatch')
        if any(receipt.get(k) != value for k, value in _proof_metadata(facts).items()):
            problems.append('automatic_rule_provenance_mismatch')
    elif receipt.get('basis') == 'trusted_execution':
        facts, trusted = _execution(record)
        if not facts or not trusted or trusted.get('receipt_id') != receipt.get('trusted_execution_id'):
            problems.append('execution_not_issued_by_trusted_verifier')
        elif any(record.get(k) != value for k, value in _target(facts).items()):
            problems.append('automatic_execution_target_mismatch')
        if any(receipt.get(k) != value for k, value in _proof_metadata(facts, trusted).items()):
            problems.append('automatic_execution_provenance_mismatch')
    elif receipt.get('basis') == 'decisive_missing_facts':
        missing = _decisive_missing(record)
        if not missing or any(record.get(k) != value for k, value in _missing_target(missing).items()):
            problems.append('missing_facts_not_mechanism_grounded')
        if any(receipt.get(k) != value for k, value in _proof_metadata().items()):
            problems.append('automatic_missing_facts_provenance_mismatch')
    else:
        problems.append('unknown_automatic_proof_basis')
    return problems


def _decisive_missing(record):
    from . import evidence_recursive
    source_gaps=evidence_recursive.missing_facts(record)
    if source_gaps:return source_gaps
    from .verify import automatic
    diagnose = getattr(automatic, 'diagnose_case', None)
    return list((diagnose(record) or {}).get('missing_facts') or []) if diagnose else []


def admit_record(record, *, decision=None, gate=None, root=None, policy=None):
    if policy == 'automatic-evidence/3':
        from .automatic_evidence_v3 import admit_record as admit_v3
        return admit_v3(record, gate=gate, root=root)
    if policy not in (None, POLICY):
        raise ValueError('unsupported automatic admission policy')
    out = copy.deepcopy(record)
    gate = gate.as_dict() if hasattr(gate, 'as_dict') else (gate or {})
    problems, foundation = _source_state(out, gate, root=root)
    if foundation and foundation.get('schema') == _ACQUIRED_SCHEMA:
        foundation = None  # New authority requires the policy-3 proof pipeline.
    facts, executed = None, None
    analysis_waivers = {'attributed', 'link_evidence'} | ({'unverified_fix_source'} if foundation else set())
    if not set(problems) - analysis_waivers:
        facts = _source_rule(out)
        if facts is None:
            facts, executed = _execution(out)
    # A closed operation proof or independently observed unit-level effect is a
    # stronger attribution witness than a name/filename heuristic. It does not
    # supersede parse, production, snapshot, quarantine or fix identity checks.
    revalidated = sorted(set(problems) & {'attributed', 'link_evidence'}) if facts else []
    revalidated_source = ['unverified_fix_source'] if foundation and facts and facts.get('repair_valid') is True else []
    problems = [p for p in problems if p not in revalidated and p not in revalidated_source]
    for key in _FIELDS:
        out[key] = [] if key in ('preconditions', 'missing_facts') else None
    from .schemas import reviewed
    for field in reviewed.FIELDS:
        if field.group == 'verification' and field.name != 'provenance_status':
            out[field.name] = [] if field.type == 'list' else None
    out.update(admission_policy=POLICY, admission_outcome='excluded', label_state='unreviewed', review_status='unreviewed')
    basis, missing = 'none', []
    if not problems and facts:
        from .security_quality import admission_problems
        out.update(_target(facts), review_status='auto_checked', admission_outcome='admitted')
        issues = admission_problems(out, admission_policy=POLICY, check_receipt=False)
        if issues:
            problems.extend(issues)
            out.update(label_state='unreviewed', review_status='unreviewed', admission_outcome='excluded')
        else:
            basis = 'trusted_execution' if executed else 'source_rule'
            if executed:
                out['verification_receipt_ids'] = [executed['receipt_id']]
                binding = executed.get('binding') or {}
                out['environment_digest'] = binding.get('image_id')
                out['dependency_lock_hash'] = binding.get('dependency_sha256')
                out['test_artifact_hashes'] = [binding['harness_sha256']] if binding.get('harness_sha256') else []
    elif not problems:
        missing = _decisive_missing(out)
        if missing:
            out.update(_missing_target(missing), review_status='auto_checked', admission_outcome='insufficient_context')
            basis = 'decisive_missing_facts'
        else:
            problems.append('unsupported_security_mechanism')
    out['admission_reasons'] = sorted(set(problems)) or missing
    out['context_suggestions'] = json.dumps(context_suggestions(out, decisive_missing=missing), sort_keys=True)
    out['admission_provenance'] = json.dumps({'policy': POLICY, 'outcome': out['admission_outcome'], 'basis': basis,
        'source_sha256': source_fingerprint(out), 'target_sha256': target_fingerprint(out),
        **_proof_metadata(facts, executed), 'rule_registry': _registry(),
        'grammar': evidence_ast.grammar_signature(), 'revalidated_heuristics': revalidated,
        'source_foundation': foundation if revalidated_source else None,
        'revalidated_source_checks': revalidated_source}, sort_keys=True)
    return out
