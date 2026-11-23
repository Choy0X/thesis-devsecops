"""Per-response diagnostics for raw security answers. Nothing is executed and nothing is repaired.

The primary measurement stays ``security_serialization.parse_response`` (strict JSON, then the response contract) applied to the raw
text. These diagnostics only explain it, in separate layers:

* ``json_syntax``: the kind of syntax failure (invalid escape, duplicate key, trailing content, fence, truncation, non-finite number);
* ``schema``: the response contract, evaluated only when the JSON is valid;
* ``evidence``: every quote against the authenticated source bytes;
* ``patch``: exact in-memory applicability, with a reason code, and whether the result equals the authored fixed source;
* ``tests``: static checks of embedded test code (Python syntax by ``ast``, fake-adapter arity against the fixed implementation call,
  a double-quote-escaped SQL expectation). JavaScript is not parsed here; the container verifier owns that.

Behavioural verification is a separate capability (``behavior_verification``).
"""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json

SCHEMA = 'security-response-diagnostics/1'
ADAPTER_METHODS = ('execute', 'get', 'run', 'exec', 'execFile')


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _arity(function):
    args = function.args
    positional = list(args.posonlyargs) + list(args.args)
    if positional and positional[0].arg in ('self', 'cls'):
        positional = positional[1:]
    required = len(positional) - len(args.defaults)
    return [max(0, required), 99 if args.vararg else len(positional)]


def fake_signature_report(test_code, fixed_source, *, language):
    """Static arity check of a fake adapter against the way the fixed implementation calls it. Executes nothing."""
    if language != 'python':
        return {'state': 'not_analyzed', 'detail': 'static signature analysis covers Python only'}
    try:
        test_tree, source_tree = ast.parse(test_code), ast.parse(fixed_source)
    except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
        return {'state': 'syntax_error', 'detail': f'{type(error).__name__}: {str(error)[:200]}'}
    fakes = {}
    for node in ast.walk(test_tree):
        if isinstance(node, ast.FunctionDef) and node.name in ADAPTER_METHODS:
            fakes[node.name] = _arity(node)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(value, ast.Lambda):
                    fakes[key.value] = _arity(value)
    adapters = set()
    for node in ast.walk(source_tree):
        if isinstance(node, ast.FunctionDef) and node.args.args:
            adapters.add(node.args.args[-1].arg)      # the injected adapter is the last parameter
    calls = [(node.func.attr, len(node.args)) for node in ast.walk(source_tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name) and node.func.value.id in adapters]
    if not calls or not fakes:
        return {'state': 'not_analyzed', 'detail': 'no adapter call or no fake adapter found'}
    method, passed = calls[0]
    accepted = fakes.get(method)
    escaped = any(isinstance(n, ast.Constant) and isinstance(n.value, str) and "''" in n.value for n in ast.walk(test_tree))
    if accepted is None:
        return {'state': 'signature_mismatch', 'detail': f'fake defines no {method}', 'fake_accepts': None,
                'implementation_passes': passed, 'expects_escaped_sql_instead_of_binding': False}
    return {'state': 'compatible' if accepted[0] <= passed <= accepted[1] else 'signature_mismatch', 'method': method,
            'fake_accepts': accepted, 'implementation_passes': passed, 'expects_escaped_sql_instead_of_binding': escaped}


def _json_kind(text, problems):
    if not text.startswith('{'):
        return 'not_object_start'
    message = ' '.join(problems)
    for needle, kind in (('duplicate JSON object key', 'duplicate_key'), ('Extra data', 'trailing_content'), ('escape', 'invalid_escape'),
                         ('non-finite', 'non_finite_number'), ('control character', 'control_character'),
                         ('Unterminated', 'truncated_or_malformed'), ('Expecting', 'truncated_or_malformed')):
        if needle in message:
            return kind
    return 'other'


PATCH_CODES = (('context does not exactly match', 'context_mismatch'), ('Unsafe or unsupported patch path', 'unauthorized_path'),
               ('authenticated allowed existing source', 'unauthorized_path'), ('Malformed hunk header', 'malformed_hunk_header'),
               ('Hunk body exceeds', 'hunk_count_mismatch'), ('Incomplete or unsupported hunk body', 'hunk_count_mismatch'),
               ('offset disagrees', 'hunk_offset'), ('Overlapping or out-of-range', 'hunk_offset'), ('no source change', 'no_change'),
               ('No source change', 'no_change'), ('Expected unified diff file headers', 'missing_headers'),
               ('Missing new-file header', 'missing_headers'), ('Missing or oversized', 'missing_patch'), ('no hunks', 'no_hunks'))


def _patch(answer, sources, allowed_paths, reference_fixed):
    from training.finetune import repair_edits
    from training.finetune.repair_validation import assess_patch
    if answer.get('status') != 'finding' or not answer.get('patch'):
        return {'state': 'not_attempted'}
    if repair_edits.looks_like_edits(answer['patch']):
        path = next(iter(allowed_paths), None)
        if path is None or path not in sources:
            return {'state': 'rejected', 'code': 'unauthorized_path', 'detail': 'no authenticated source for the edit list'}
        verdict = repair_edits.assess(answer['patch'], sources[path], path)
        if verdict['state'] != 'applies_exactly':
            return {'state': 'rejected', 'code': verdict['code'], 'detail': verdict['detail'], 'relocated_exactly': verdict['relocated_exactly']}
        answer = {**answer, 'patch': verdict['patch']}                  # the converter's diff goes through the same exact applier as every other patch
    result = assess_patch(answer['patch'], sources, allowed_paths=set(allowed_paths))
    if result['state'] != 'applies_exactly':
        detail = result.get('detail', '')
        code = next((code for needle, code in PATCH_CODES if needle in detail), 'other')
        return {'state': 'rejected', 'code': code, 'detail': detail}
    out = {'state': 'applies_exactly', 'changed_paths': result['changed_paths'], 'modified_sha256': result['modified_sha256']}
    if reference_fixed is not None and len(result['changed_paths']) == 1:
        out['matches_reference_fix'] = result['modified_sha256'][result['changed_paths'][0]] == _sha(reference_fixed)
    return out


def _evidence(answer, sources):
    quotes = [item['quote'] for item in answer.get('evidence', []) if isinstance(item, dict) and isinstance(item.get('quote'), str)]
    if not quotes:
        return {'state': 'no_evidence', 'quotes': 0, 'grounded': 0, 'ungrounded': []}
    corpus = list(sources.values())
    missing = [q for q in quotes if not any(q in text for text in corpus)]
    state = 'grounded' if not missing else 'ungrounded' if len(missing) == len(quotes) else 'partly_grounded'
    return {'state': state, 'quotes': len(quotes), 'grounded': len(quotes) - len(missing), 'ungrounded': missing}


def _tests(answer, reference_fixed, language):
    per_test, flags = [], []
    for item in answer.get('tests', []):
        code = item.get('test_code', '') if isinstance(item, dict) else ''
        entry = {'syntax': 'not_checked', 'fake_signature': 'not_analyzed', 'expects_escaped_sql_instead_of_binding': False}
        if language == 'python':
            try:
                tree = ast.parse(code)
                entry['syntax'] = 'ok'
                if any((isinstance(n, ast.Attribute) and n.attr in ('getsource', 'getsourcelines', 'getsourcefile'))
                       or (isinstance(n, ast.Name) and n.id == '__file__') for n in ast.walk(tree)):
                    flags.append('introspects_source')       # such a test can pass by reading the code instead of exercising it
            except (SyntaxError, ValueError, RecursionError, MemoryError) as error:
                entry['syntax'], entry['detail'] = 'error', f'{type(error).__name__}: {str(error)[:200]}'
                flags.append('syntax_error')
        if language == 'python' and reference_fixed is not None and entry['syntax'] == 'ok':
            report = fake_signature_report(code, reference_fixed, language=language)
            entry['fake_signature'] = report['state']
            entry['expects_escaped_sql_instead_of_binding'] = bool(report.get('expects_escaped_sql_instead_of_binding'))
            if report['state'] == 'signature_mismatch':
                flags.append('signature_mismatch')
            if entry['expects_escaped_sql_instead_of_binding']:
                flags.append('expects_escaped_sql')
        per_test.append(entry)
    return {'state': 'static_flags' if flags else 'static_ok', 'count': len(per_test), 'per_test': per_test, 'flags': sorted(set(flags))}


def diagnose_response(task, text, *, sources, allowed_paths, reference_fixed=None, language=None):
    """Explain the primary strict result for one raw answer. ``sources`` maps path to authenticated source text."""
    from training.finetune.security_serialization import parse_response
    answer, problems = parse_response(task, text)
    result = {'schema_id': SCHEMA, 'task': task, 'raw_sha256': _sha(text), 'raw_chars': len(text),
              'primary': {'state': 'valid' if answer is not None else 'malformed', 'problems': problems}}
    json_ok = text.startswith('{') and not any(p.startswith(('the answer must start', 'not one strict JSON')) for p in problems)
    if not json_ok:
        result['json_syntax'] = {'state': 'invalid', 'kind': _json_kind(text, problems)}
        result.update(schema={'state': 'not_evaluated'}, evidence={'state': 'not_evaluated'}, patch={'state': 'not_evaluated'},
                      tests={'state': 'not_evaluated'})
        return result
    result['json_syntax'] = {'state': 'valid', 'kind': None}
    result['schema'] = {'state': 'valid' if answer is not None else 'invalid', 'problems': problems if answer is None else []}
    if answer is None:
        result.update(evidence={'state': 'not_evaluated'}, patch={'state': 'not_evaluated'}, tests={'state': 'not_evaluated'})
        return result
    if language is None:
        paths = list(allowed_paths) or list(sources)
        language = 'python' if paths and paths[0].endswith('.py') else 'javascript' if paths and paths[0].endswith('.js') else None
    result['evidence'] = _evidence(answer, sources)
    result['patch'] = _patch(answer, sources, allowed_paths, reference_fixed) if task == 'repair' else {'state': 'not_applicable'}
    result['tests'] = _tests(answer, reference_fixed, language) if task == 'regression_test_design' else {'state': 'not_applicable'}
    return result


def summarize(diagnostics):
    """Aggregate diagnostics by case id; a ``None`` entry is a planned case that produced no generation and stays in the denominator."""
    items = list(diagnostics.values())
    done = [d for d in items if d is not None]
    return {'planned': len(items), 'generated': len(done), 'not_generated': len(items) - len(done),
            'json_valid': sum(d['json_syntax']['state'] == 'valid' for d in done),
            'json_invalid_kinds': dict(Counter(d['json_syntax']['kind'] for d in done if d['json_syntax']['state'] == 'invalid')),
            'schema_valid': sum(d['schema']['state'] == 'valid' for d in done),
            'schema_invalid': sum(d['schema']['state'] == 'invalid' for d in done),
            'evidence': dict(Counter(d['evidence']['state'] for d in done)),
            'patch_applies_exactly': sum(d['patch']['state'] == 'applies_exactly' for d in done),
            'patch_rejected': sum(d['patch']['state'] == 'rejected' for d in done),
            'patch_rejection_codes': dict(Counter(d['patch']['code'] for d in done if d['patch']['state'] == 'rejected')),
            'patch_not_attempted': sum(d['patch']['state'] == 'not_attempted' for d in done),
            'tests_static_ok': sum(d['tests']['state'] == 'static_ok' for d in done),
            'tests_flagged': sum(d['tests']['state'] == 'static_flags' for d in done),
            'tests_flags': dict(Counter(flag for d in done if d['tests']['state'] == 'static_flags' for flag in d['tests']['flags']))}
