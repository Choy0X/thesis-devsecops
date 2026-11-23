"""Deterministic, source-bound admission. No model invocation or human approval.

Rules cover only their demonstrated source pattern. Documented evidence gaps can
produce missing-fact examples; unsupported mechanisms remain unlabelled. A fix
link alone never establishes a safe target.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

POLICY = 'automatic-evidence/1'
_TARGETS = ('label_state', 'violated_invariant', 'attacker_control', 'preconditions', 'evidence_spans',
            'condition_scope', 'decisive_safeguard', 'fixed_review', 'behavioral_intent', 'missing_facts')
_HARD_GATE = {'fix_link_verified', 'link_evidence', 'parent_recorded', 'commit_kind', 'formatting_only',
              'quarantined', 'parse', 'production_code', 'attributed'}


def _json(value, default):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def decision_fingerprint(record):
    return {'before_sha256': hashlib.sha256((record.get('vulnerable_code') or '').encode()).hexdigest(),
            'after_sha256': hashlib.sha256((record.get('fixed_code') or '').encode()).hexdigest(),
            'parent': record.get('relevant_parent_commit'), 'fix': record.get('fix_commit')}


def source_fingerprint(record):
    return _digest({**decision_fingerprint(record), 'before_context': _json(record.get('context_before'), {}),
                    'after_context': _json(record.get('context_after'), {}), 'pair': record.get('raw_pair_key')})


def _target_fingerprint(record):
    return _digest({key: _json(record.get(key), None) if key in ('evidence_spans', 'fixed_review') else record.get(key) for key in _TARGETS})


def receipt_problems(record):
    receipt = _json(record.get('admission_provenance'), {})
    if (not isinstance(receipt, dict) or receipt.get('policy') != POLICY
            or receipt.get('outcome') not in ('admitted', 'insufficient_context')
            or receipt.get('basis') not in ('source_rule', 'structured_decision', 'missing_facts', 'verified_execution')):
        return ['missing_automatic_admission_receipt']
    problems = []
    if receipt.get('source_sha256') != source_fingerprint(record):
        problems.append('automatic_source_fingerprint_mismatch')
    if receipt.get('target_sha256') != _target_fingerprint(record):
        problems.append('automatic_target_fingerprint_mismatch')
    if record.get('admission_outcome') != receipt.get('outcome'):
        problems.append('automatic_outcome_mismatch')
    return problems


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _text(node, data):
    return data[node.start_byte:node.end_byte].decode() if node is not None else ''


def _semantic(node, data, replacement=None):
    if replacement and node.id == replacement[0].id:
        return _semantic(replacement[1], data)
    if node.type == 'comment':
        return None
    children = [v for child in node.children if (v := _semantic(child, data, replacement)) is not None]
    return (node.type, tuple(children)) if children else (node.type, _text(node, data))


def _prototype_filter(row, parser=None):
    """Own-key copy whose sole semantic edit filters all prototype meta keys.

    Requiring an AST-equivalent before tree after removing that one filter keeps
    unused filters, unrelated changes and comment/string lookalikes out.
    """
    if '1321' not in str(row.get('cwe_ids')) or row.get('language') not in ('javascript', 'typescript', 'js', 'ts'):
        return None
    before, after = row.get('vulnerable_code') or '', row.get('fixed_code') or ''
    if max(len(before), len(after)) > 100000:
        return None
    from app.backend.pipeline import evidence_ast
    language = 'typescript' if row.get('language') in ('typescript', 'ts') else 'javascript'
    # Proof parsing must use the installed pinned offline grammar. Mining's
    # optional grammar cache can be absent without making valid source vanish.
    parsed = [(parser or evidence_ast.parse)(language, code) for code in (before, after)]
    if any(tree is None or tree[0].has_error for tree in parsed):
        return None
    (br, bd), (ar, ad) = parsed
    # This rule establishes only a straight-line own-property copy. Additional
    # branches, calls, rebindings or nested scopes need other evidence.
    before_functions = [n for n in _walk(br) if n.type in ('function_declaration', 'function_expression', 'arrow_function')]
    if len(before_functions) != 1 or len([n for n in br.named_children if n.type != 'comment']) != 1:
        return None
    before_function = before_functions[0]
    before_body = before_function.child_by_field_name('body')
    before_parameters = before_function.child_by_field_name('parameters')
    if before_body is None or before_parameters is None or _text(before_function.child_by_field_name('name'), bd) == 'Object':
        return None
    parameter_names = {_text(n, bd) for n in _walk(before_parameters) if n.type == 'identifier'}
    if 'Object' in parameter_names:
        return None
    statements = [n for n in before_body.named_children if n.type != 'comment']
    if (len(statements) != 3 or statements[0].type != 'lexical_declaration'
            or statements[1].type not in ('for_in_statement', 'for_statement') or statements[2].type != 'return_statement'):
        return None
    if len([n for n in statements[0].named_children if n.type != 'comment']) != 1:
        return None
    loop_body = statements[1].child_by_field_name('body')
    body_statements = [n for n in loop_body.named_children if n.type != 'comment'] if loop_body is not None else []
    expected_body = ['expression_statement'] if statements[1].type == 'for_in_statement' else ['lexical_declaration', 'expression_statement']
    if [n.type for n in body_statements] != expected_body:
        return None
    if expected_body[0] == 'lexical_declaration' and len([n for n in body_statements[0].named_children if n.type != 'comment']) != 1:
        return None
    for call in _walk(ar):
        if call.type != 'call_expression':
            continue
        member = call.child_by_field_name('function')
        if member is None or member.type != 'member_expression' or _text(member.child_by_field_name('property'), ad) != 'filter':
            continue
        original = member.child_by_field_name('object')
        args = call.child_by_field_name('arguments')
        if original is None or original.type != 'call_expression' or args is None or len(args.named_children) != 1:
            continue
        function = original.child_by_field_name('function')
        if _text(function, ad) != 'Object.keys':
            continue
        original_args = original.child_by_field_name('arguments')
        if original_args is None or len(original_args.named_children) != 1 or original_args.named_children[0].type != 'identifier':
            continue
        source = _text(original_args.named_children[0], ad)
        callback = args.named_children[0]
        body = callback.child_by_field_name('body')
        if callback.type != 'arrow_function' or body is None or body.type != 'unary_expression':
            continue
        parameter = callback.child_by_field_name('parameter') or callback.child_by_field_name('parameters')
        params = [n for n in _walk(parameter) if n.type == 'identifier'] if parameter is not None else []
        if len(params) != 1:
            continue
        key = _text(params[0], ad)
        expression = body.child_by_field_name('argument')
        if expression is None or expression.type != 'call_expression' or not _text(body, ad).lstrip().startswith('!'):
            continue
        includes = expression.child_by_field_name('function')
        array = includes.child_by_field_name('object') if includes is not None and includes.type == 'member_expression' else None
        includes_args = expression.child_by_field_name('arguments')
        if (array is None or array.type != 'array' or _text(includes.child_by_field_name('property'), ad) != 'includes'
                or includes_args is None or [_text(n, ad) for n in includes_args.named_children] != [key]):
            continue
        if { _text(n, ad).strip("\"'") for n in array.named_children } != {'__proto__', 'constructor', 'prototype'}:
            continue
        declaration = call.parent
        if declaration is None or declaration.type != 'variable_declarator' or declaration.child_by_field_name('value') != call:
            continue
        name = declaration.child_by_field_name('name')
        if name is None or name.type != 'identifier':
            continue
        keys = _text(name, ad)
        if keys in parameter_names:
            continue
        if _semantic(br, bd) != _semantic(ar, ad, (call, original)):
            continue
        # A direct for-of or bounded indexed traversal of the filtered array.
        loops = [n for n in _walk(ar) if n.type == 'for_in_statement'
                 and _text(n.child_by_field_name('right'), ad) == keys]
        indexed = False
        if not loops:
            for candidate in _walk(ar):
                if candidate.type != 'for_statement':
                    continue
                condition = _text(candidate.child_by_field_name('condition'), ad)
                match = re.fullmatch(r'\s*([A-Za-z_$][\w$]*)\s*<\s*' + re.escape(keys) + r'\.length\s*', condition)
                if not match:
                    continue
                index = match[1]
                initializer = _text(candidate.child_by_field_name('initializer'), ad)
                increment = re.sub(r'\s+', '', _text(candidate.child_by_field_name('increment'), ad))
                if (not re.fullmatch(r'(?:let|var)\s+' + re.escape(index) + r'\s*=\s*0\s*;?', initializer)
                        or increment not in (index + '++', '++' + index)):
                    continue
                body_node = candidate.child_by_field_name('body')
                bindings = [n for n in _walk(body_node) if n.type == 'variable_declarator'
                            and _text(n.child_by_field_name('name'), ad) == key
                            and re.fullmatch(re.escape(keys) + r'\s*\[\s*' + re.escape(index) + r'\s*\]', _text(n.child_by_field_name('value'), ad))]
                if len(bindings) == 1:
                    loops.append(candidate)
                    indexed = True
        if len(loops) != 1:
            continue
        loop = loops[0]
        if not indexed:
            left = loop.child_by_field_name('left')
            if (' of ' not in _text(loop, ad)
                    or _text(left, ad).removeprefix('const ').removeprefix('let ').strip() != key):
                continue
        assignments = [n for n in _walk(ar) if n.type == 'assignment_expression']
        selected = []
        for assignment in assignments:
            target = assignment.child_by_field_name('left')
            if target is not None and target.type == 'subscript_expression' and _text(target.child_by_field_name('index'), ad) == key:
                if not (loop.start_byte <= assignment.start_byte < assignment.end_byte <= loop.end_byte):
                    selected = []; break
                destination = target.child_by_field_name('object')
                value = assignment.child_by_field_name('right')
                if (destination is None or destination.type != 'identifier' or _text(destination, ad) not in parameter_names
                        or _text(destination, ad) == source
                        or value is None or value.type != 'subscript_expression'
                        or _text(value.child_by_field_name('object'), ad) != source
                        or _text(value.child_by_field_name('index'), ad) != key):
                    selected = []; break
                selected.append(assignment)
        if len(selected) != 1 or len(assignments) != 1:
            continue
        # No mutation or alternate use of the filtered keys array is allowed.
        uses = [n for n in _walk(ar) if n.type == 'identifier' and _text(n, ad) == keys]
        if len(uses) != (3 if indexed else 2):
            continue
        quote = _text(selected[0], ad) + (';' if ad[selected[0].end_byte:selected[0].end_byte + 1] == b';' else '')
        if quote not in before:
            continue
        parameters = [n for n in _walk(ar) if n.type == 'formal_parameters' and n.start_byte < call.start_byte]
        if not parameters or source not in {_text(n, ad) for n in _walk(parameters[0]) if n.type == 'identifier'}:
            continue
        guard_quote = _text(declaration, ad)
        scope = f'This own-property copy through {source}, limited to prototype meta-property writes.'
        invariant = f'Attacker-supplied {source} keys must not write __proto__, constructor or prototype into the destination.'
        return {'label_state': 'vulnerability_supported', 'violated_invariant': invariant,
                'attacker_control': f'The {source} argument provides the own property names copied by this function.',
                'preconditions': [f'The caller supplies attacker-controlled own properties in {source}.',
                                  'The destination is a separate object with ordinary JavaScript object prototype behavior.',
                                  'Object.keys has its standard JavaScript behavior.'],
                'evidence_spans': json.dumps([{'quote': quote}]), 'condition_scope': scope,
                'behavioral_intent': f'Copy ordinary own properties from {source} while excluding prototype meta-property keys.',
                'fixed_review': json.dumps({'scope': scope, 'safeguard': 'The copied key list filters __proto__, constructor and prototype before the property-write loop.',
                                            'evidence': [{'quote': guard_quote}], 'admission_policy': POLICY}),
                'evidence_summary': invariant, 'label_basis': 'automatic_source_rule:prototype_key_filter/1'}
    return None


def _source_problems(row, gate):
    problems = []
    if not isinstance(row.get('vulnerable_code'), str) or not row['vulnerable_code'].strip():
        problems.append('missing_source_code')
    if row.get('data_quality_status') in ('excluded', 'quarantined') or row.get('quarantine_reasons'):
        problems.append('quarantined' if row.get('data_quality_status') == 'quarantined' or row.get('quarantine_reasons') else 'source_excluded')
    problems.extend(p for p in gate.get('failed', []) if p in _HARD_GATE)
    if row.get('provenance_status') != 'verified':
        problems.append('unverified_fix_source')
    for side, revision in [('before', row.get('relevant_parent_commit')), ('after', row.get('fix_commit'))]:
        context = _json(row.get('context_' + side), {})
        if not isinstance(context, dict):
            problems.append('malformed_context:' + side); continue
        snapshot = context.get('snapshot') or {}
        if snapshot and (snapshot.get('revision') != revision or snapshot.get('side') != side):
            problems.append('mixed_snapshot:' + side)
        if 'target_source_mismatch' in context.get('omissions', []):
            problems.append('target_source_mismatch:' + side)
    return sorted(set(problems))


def _suggestions(row, missing):
    reasons = list(missing)
    for side in ('before', 'after'):
        context = _json(row.get('context_' + side), {})
        if not isinstance(context, dict):
            continue
        reasons += [side + ': ' + str(x) for x in context.get('omissions', [])]
    explanation = row.get('explanation') or ''
    quality = 'missing' if not explanation else 'weak' if missing else 'partial' if reasons or len(explanation) < 200 else 'strong'
    if quality in ('weak', 'partial') and not reasons:
        reasons.append('Additional source-grounded context could clarify the advisory and affected code.')
    return {'quality': quality, 'reasons': list(dict.fromkeys(reasons)), 'optional': True}


def _missing_source_facts(row):
    """Only documented before-context gaps justify an abstention target.

    A missing rule or a stale draft is an automation limitation, not proof that
    the student cannot answer from the code it sees.
    """
    context = _json(row.get('context_before'), {})
    if not isinstance(context, dict):
        return []
    facts = []
    if (context.get('snapshot') or {}).get('available') is False:
        facts.append('Referenced repository definitions and callers needed to establish input origin and safeguards are unavailable for the shown code.')
    for omission in context.get('omissions', []):
        if not isinstance(omission, str):
            continue
        text = omission.lower()
        if any(word in text for word in ('unresolved_import', 'missing_import', 'unresolved_callee', 'missing_callee', 'missing_symbol', 'definition_unavailable')):
            facts.append(('Referenced definition required by the shown code is missing: ' + omission)[:300])
        elif 'budget' in text and any(word in text for word in ('caller', 'callee', 'import', 'definition')):
            facts.append(('Relevant source context was omitted by the selected budget: ' + omission)[:300])
    return list(dict.fromkeys(facts))[:20]


def _decision_fresh(row, decision, gate):
    fields = decision.get('fields') or {}
    bound = fields.get('_automatic_source')
    if bound is not None:
        return bound == decision_fingerprint(row)
    exchange = fields.get('_review_exchange') or {}
    if exchange:
        if exchange.get('source_changed') or not exchange.get('complete'):
            return False
        source = gate.get('source') or {}
        if not source:
            return False
        from app.backend.pipeline import review_exchange
        return (exchange.get('queue_source_sha') == review_exchange.raw_fingerprint(source)
                and review_exchange.fresh_context_matches(source, exchange, {}))
    # Historical human decisions have no imported draft fingerprint. Their exact
    # source quotations and current snapshot consistency are still revalidated.
    return decision.get('review_status') == 'human_reviewed'


def admit_record(record, *, decision=None, gate=None):
    from app.backend.pipeline import security_quality
    out = copy.deepcopy(record)
    gate = gate.as_dict() if hasattr(gate, 'as_dict') else (gate or {})
    problems = _source_problems(out, gate)
    out.update(admission_policy=POLICY, admission_reasons=problems)
    basis = 'missing_facts'
    if problems:
        out.update(admission_outcome='excluded', label_state='unreviewed', review_status='unreviewed')
        missing = []
    else:
        rule = _prototype_filter(out)
        missing = []
        fields = (decision or {}).get('fields') or {}
        if decision and decision.get('review_status') != 'unreviewed':
            if not _decision_fresh(out, decision, gate):
                missing.append('The existing security decision is not bound to the current source and context.')
            else:
                candidate = {**out, **{k: v for k, v in fields.items() if k in _TARGETS}, 'label_state': decision.get('label_state')}
                for field in ('evidence_spans', 'fixed_review'):
                    if isinstance(candidate.get(field), (list, dict)):
                        candidate[field] = json.dumps(candidate[field])
                issues = security_quality.admission_problems(candidate, admission_policy=POLICY, check_receipt=False)
                if issues:
                    missing.append('The existing security decision needs valid source evidence: ' + ', '.join(issues))
                elif rule and candidate['label_state'] != rule['label_state']:
                    missing.append('The existing decision conflicts with the source-grounded security rule.')
                else:
                    out = candidate
                    out['review_status'] = decision['review_status']
                    basis = 'structured_decision'
        elif rule:
            out.update(rule, review_status='auto_checked')
            basis = 'source_rule'
        else:
            missing = _missing_source_facts(out)
            if not missing:
                problems.append('unresolved_security_evidence')
        if missing:
            if decision and decision.get('review_status') != 'unreviewed':
                # Invalid, stale or conflicting adjudications do not teach the
                # student to abstain on otherwise answerable source code.
                problems.extend(missing)
                missing = []
            else:
                out.update(label_state='insufficient_context', review_status='auto_checked', fixed_review=None,
                           violated_invariant=None, attacker_control=None, preconditions=[], evidence_spans=None,
                           decisive_safeguard=None, behavioral_intent=None, missing_facts=missing,
                           label_basis='automatic_missing_facts')
                basis = 'missing_facts'
        elif out.get('label_state') == 'insufficient_context':
            missing = list(out.get('missing_facts') or [])
        if problems:
            out.update(label_state='unreviewed', review_status='unreviewed', admission_outcome='excluded',
                       fixed_review=None, missing_facts=[], admission_reasons=problems)
        else:
            out['admission_outcome'] = 'insufficient_context' if out['label_state'] == 'insufficient_context' else 'admitted'
            out['admission_reasons'] = missing
    out['context_suggestions'] = json.dumps(_suggestions(out, missing), sort_keys=True)
    receipt_ids, receipt_errors = [], []
    for receipt in gate.get('receipts', []):
        from app.backend.pipeline.verify import receipts as validator
        if not isinstance(receipt, dict) or receipt.get('case_id') != out.get('record_id'):
            receipt_errors.append('receipt_not_bound_to_record'); continue
        try:
            issues = validator.validate(receipt)
        except (TypeError, ValueError, KeyError):
            issues = ['malformed_receipt']
        patch = out.get('diff') or ''
        if (issues or receipt.get('verdict') != 'verified' or not patch
                or receipt.get('subject_sha256') != hashlib.sha256(patch.encode()).hexdigest()):
            receipt_errors.extend(issues or ['receipt_not_bound_to_exact_patch']); continue
        try:
            out = validator.attach(out, receipt)
            receipt_ids.append(receipt['receipt_id'])
        except ValueError as problem:
            receipt_errors.append(str(problem))
    out['admission_provenance'] = json.dumps({'policy': POLICY, 'outcome': out['admission_outcome'], 'basis': basis,
        'source_sha256': source_fingerprint(out), 'target_sha256': _target_fingerprint(out),
        'validated_receipt_ids': receipt_ids, 'receipt_problems': receipt_errors}, sort_keys=True)
    return out
