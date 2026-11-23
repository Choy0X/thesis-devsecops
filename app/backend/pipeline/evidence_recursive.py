"""Closed recursive source proofs with complete, pinned helper semantics.

Each accepted form has an induction argument: no recursive destination can be
a shared inherited function; reserved keys cannot expose Object.prototype;
ordinary plain JSON properties retain their behavior. Unknown program forms,
helper bodies and bindings have no proof. These are not test-result imports.
"""
from __future__ import annotations

import functools
import json
import pathlib
import posixpath

FORMS = pathlib.Path(__file__).with_name('evidence_recursive_forms.json')


@functools.lru_cache(maxsize=2)
def _forms(modified,size):
    return json.loads(FORMS.read_text(encoding='utf-8'))


def local_alpha_match(parsed, template, language, previous=None):
    """Match runtime locals by declarations, preserving every lexical capture.

    Top-level/self/dependency names, global names, property names, literals and
    type syntax remain exact. A shared injective spelling map is conservative
    across nested scopes and is reused for both versions of a pair.
    """
    from . import evidence_ast, evidence_rules as r
    from .verify.closure_diagnostics import _binding_scan
    if parsed is None:
        return None
    model = evidence_ast.parse(language, template)
    if model is None:
        return None
    actual_tree, actual_raw = parsed
    expected_tree, expected_raw = model
    scans = [_binding_scan(raw.decode(), language) for raw in (expected_raw, actual_raw)]
    if any(scan is None for scan in scans):
        return None
    indexes = []
    for declarations, references, root, raw in scans:
        index = {}
        for name, scope, node in references:
            owner = scope
            while owner is not None and name not in owner.names:
                owner = owner.parent
            definitions = [] if owner is None else owner.declarations.get(name, [])
            index[(node.start_byte, node.end_byte, node.type)] = {
                'name': name, 'local': owner is not None and owner is not root and bool(definitions),
                'definitions': [(d.start_byte, d.end_byte, d.type) for d in definitions],
                'implicit_scope': owner.span if owner is not None and not definitions else None}
        indexes.append(index)
    expected_index, actual_index = indexes
    bindings, nodes, reference_pairs = dict(previous or {}), {}, []
    pending, visited = [(expected_tree, actual_tree, 0)], 0
    while pending:
        expected, actual, depth = pending.pop()
        visited += 1
        if visited > 12000 or depth > 96 or expected.type != actual.type:
            return None
        expected_key = (expected.start_byte, expected.end_byte, expected.type)
        actual_key = (actual.start_byte, actual.end_byte, actual.type)
        nodes[expected_key] = actual
        ec = [node for node in expected.children if node.type != 'comment']
        ac = [node for node in actual.children if node.type != 'comment']
        if len(ec) != len(ac):
            return None
        if not ec:
            a, e = actual_index.get(actual_key), expected_index.get(expected_key)
            if (e is None) != (a is None):
                return None
            if e is not None:
                if e['local'] != a['local']:
                    return None
                if e['local']:
                    source, destination = e['name'], a['name']
                    if source in bindings and bindings[source] != destination:
                        return None
                    if source not in bindings and destination in bindings.values():
                        return None
                    bindings[source] = destination
                elif r.text(expected, expected_raw) != r.text(actual, actual_raw):
                    return None
                reference_pairs.append((e, a))
            elif r.text(expected, expected_raw) != r.text(actual, actual_raw):
                return None
        pending.extend((e, a, depth + 1) for e, a in zip(ec, ac))
    for expected, actual in reference_pairs:
        mapped = [nodes.get(key) for key in expected['definitions']]
        if any(node is None for node in mapped):
            return None
        if [(node.start_byte, node.end_byte, node.type) for node in mapped] != actual['definitions']:
            return None
        scope = expected['implicit_scope']
        if scope is not None:
            candidates = [node for key, node in nodes.items() if key[:2] == tuple(scope)]
            if not any((node.start_byte, node.end_byte) == actual['implicit_scope'] for node in candidates):
                return None
        elif actual['implicit_scope'] is not None:
            return None
    return {'bindings': bindings, 'nodes': nodes}


def match_form_pair(before, after, spec, language):
    first = local_alpha_match(before, spec['before'], language)
    second = local_alpha_match(after, spec['after'], language, first['bindings']) if first is not None else None
    return {'before': first, 'after': second} if second is not None else None


def matching_forms(before, after, language):
    stat = FORMS.stat()
    return [spec for spec in _forms(stat.st_mtime_ns, stat.st_size)
            if match_form_pair(before, after, spec, language) is not None]


def _link(items, source, destination, name, language):
    from . import evidence_ast,evidence_rules as r
    matches=[]
    scopes=[i for i in items if i.get('kind')=='module_scope' and i.get('path')==source]
    if len(scopes)>1:return False
    statements=scopes or [i for i in items if i.get('kind')=='imports' and i.get('path')==source]
    for item in statements:
        parsed=evidence_ast.parse(language,item.get('text'))
        if parsed is None:
            continue
        tree,data=parsed
        for statement in r.children(tree):
            if statement.type!='import_statement' or any(n.type=='type' for n in statement.children):
                continue
            module=r.text(statement.child_by_field_name('source'),data)
            for node in r.walk(statement):
                if node.type!='import_specifier':continue
                imported=r.text(node.child_by_field_name('name'),data)
                local=r.text(node.child_by_field_name('alias'),data) or imported
                if local!=name:continue
                path=posixpath.normpath(posixpath.join(posixpath.dirname(source),module[1:-1])) if module else ''
                matches.append(bool(module and module[1:-1].startswith('./') and destination in (path,path+'.ts',path+'.js')
                                    and imported==name and node.child_by_field_name('alias') is None
                                    and not any(n.type=='type' for n in node.children)))
    return bool(matches) and set(matches)=={True}


def _calls(source,name,language):
    from . import evidence_ast,evidence_rules as r
    parsed=evidence_ast.parse(language,source)
    return parsed is not None and any(n.type=='call_expression' and r.text(n.child_by_field_name('function'),parsed[1])==name
                                     for n in r.walk(parsed[0]))


def _exact_module_form(row,side,spec,context):
    """Match the complete pinned module, including helper/self bindings.

    A missing module is recoverable. A supplied but different module is not
    missing evidence and cannot inherit the proof from helper-name snippets.
    """
    expected=spec.get('module_'+side)
    if expected is None:return True
    from . import evidence_ast,evidence_rules as r
    import hashlib
    path=row.get('file_path_'+side)
    scopes=[i for i in context.get('items',[]) if i.get('kind')=='module_scope' and i.get('path')==path]
    if not scopes:return None
    if len(scopes)!=1:return False
    source=scopes[0].get('text')
    if not isinstance(source,str) or (context.get('snapshot') or {}).get('files',{}).get(path)!=hashlib.sha256(source.encode()).hexdigest():return False
    unit=row.get('vulnerable_code' if side=='before' else 'fixed_code') or ''
    parsed=evidence_ast.parse(row.get('language'),source)
    return bool(unit.strip() and unit.strip() in source and parsed is not None
                and r._template_bindings(parsed,expected,row.get('language'),set()) is not None)


def _closure(row,side,spec):
    from . import evidence_ast,evidence_rules as r,evidence_exports
    import hashlib
    value=row.get('context_'+side)
    try: context=json.loads(value) if isinstance(value,str) else value or {}
    except ValueError:return False
    if _exact_module_form(row,side,spec,context) is not True:return False
    items=context.get('items') or []
    files=(context.get('snapshot') or {}).get('files',{})
    for item in items:
        if item.get('kind')!='module_scope':continue
        source=item.get('text')
        if not isinstance(source,str) or files.get(item.get('path'))!=hashlib.sha256(source.encode()).hexdigest():return False
        if item.get('path')==row.get('file_path_'+side) and (row.get('vulnerable_code' if side=='before' else 'fixed_code') or '').strip() not in source:
            return False
    # Same-file helpers are direct declarations. Cross-file helpers also need
    # the actual source import edge, including the transitive tag conversion.
    helpers=spec.get('helpers_'+side,spec.get('helpers',[]))
    for helper in helpers:
        matching=[i for i in items if i.get('kind') in ('callee','module_symbol')
                  and i.get('name')==helper['name'] and i.get('path')==helper['path']]
        if not matching or len({i.get('text') for i in matching})!=1:
            return False
        parsed=evidence_ast.parse(row.get('language'),matching[0].get('text'))
        if parsed is None or r._template_bindings(parsed,helper['text'],row.get('language'),set()) is None:
            return False
        if helper['path']!=row.get('file_path_'+side):
            scopes=[i for i in items if i.get('kind')=='module_scope' and i.get('path')==helper['path']]
            if len(scopes)!=1:return False
            scope=scopes[0].get('text')
            if not isinstance(scope,str) or (context.get('snapshot') or {}).get('files',{}).get(helper['path'])!=hashlib.sha256(scope.encode()).hexdigest():
                return False
            declaration=evidence_exports.local_definition(scope,row.get('language'),helper['name'])
            parsed=evidence_ast.parse(row.get('language'),declaration)
            if parsed is None or r._template_bindings(parsed,helper['text'],row.get('language'),set()) is None:
                return False
            unit=spec['before' if side=='before' else 'after']
            sources={row.get('file_path_'+side)} if _calls(unit,helper['name'],row.get('language')) else set()
            sources |= {h['path'] for h in helpers if h['path']!=helper['path'] and _calls(h['text'],helper['name'],row.get('language'))}
            if not sources or not all(_link(items,path,helper['path'],helper['name'],row.get('language')) for path in sources):
                return False
    return True


def examine(row,before,after):
    from . import evidence_rules as r
    if not r._has_cwe(row,'CWE-1321'):
        return None
    stat=FORMS.stat()
    for spec in _forms(stat.st_mtime_ns,stat.st_size):
        matched = match_form_pair(before, after, spec, row.get('language'))
        if matched is None or not all(_closure(row,side,spec) for side in ('before','after')):
            continue
        br,bd=before;ar,ad=after
        def select(side, predicate):
            tree, raw = r._parsed_template(spec[side], row.get('language'))
            node = next(n for n in r.walk(tree) if predicate(n, raw))
            return matched[side]['nodes'][(node.start_byte, node.end_byte, node.type)]
        if spec['rule']=='source-bound-path-tokenizer/1':
            write=select('before', lambda n,d: n.type=='call_expression' and r.text(n,d)=='tokens.push(arrayIndexes[0])')
            guard=select('after', lambda n,d: n.type=='if_statement' and 'FORBIDDEN_KEYS' in r.text(n.child_by_field_name('condition'),d))
            scope='Prototype pollution through this path tokenizer when consumed by the exact same-module setValue implementation included in the source context; unrelated consumers and custom objects are outside this scope.'
            return {'method':'static_closed_world','mechanism':'prototype_pollution','rule_id':spec['rule'],
                    'violated_invariant':'Untrusted path tokens consumed by the source-bound setter must not expose inherited prototypes for mutation.',
                    'attacker_control':'The path string selects property-name tokens consumed by setValue; its value parameter supplies plain JSON data.',
                    'preconditions':['The included same-module setValue calls this tokenizer on an attacker-supplied primitive path string and a finite plain JSON destination.',
                                     'The exact SPLIT_REG_EXP and setter definitions apply, with standard String, Number, Array and Set behavior.',
                                     'The destination has ordinary data properties and no own aliases to shared prototypes; no proxies, getters or modified intrinsics.'],
                    'evidence_spans':[{'quote':r.text(write,bd)}],'condition_scope':scope,
                    'supported_impact':'A __proto__.property token sequence causes the included setter to follow Object.prototype and assign or delete its property.',
                    'behavioral_intent':'Preserve ordinary named and numeric path tokens while rejecting meta-property paths before any destination mutation.',
                    'fixed_review':{'scope':scope,'safeguard':'The exact forbidden-key Set rejects __proto__, constructor and prototype before emitting string tokens. Bracket index tokens become numbers; the setter replaces function-valued inherited properties rather than traversing them.',
                                    'evidence':[{'quote':r.text(guard,ad)}]},'repair_valid':True}
        if spec['rule']=='object-tag-recursive-merge/1':
            rule='object-tag-recursive-merge/1'
            guard=select('after', lambda n,d: n.type=='if_statement' and "key === '__proto__'" in r.text(n.child_by_field_name('condition'),d))
            write=select('before', lambda n,d: n.type=='call_expression' and r.text(n.child_by_field_name('function'),d)=='deepMerge' and 'target[key]' in r.text(n,d))
            safeguard='Every own string key is filtered before recursive access. The source-bound Object tag predicate rejects functions, so recursion cannot mutate inherited Object methods.'
        elif spec['rule']=='plain-record-recursive-clone/1':
            rule='plain-record-recursive-clone/1'
            guard=select('after', lambda n,d: n.type=='if_statement' and "name === '__proto__'" in r.text(n.child_by_field_name('condition'),d))
            write=select('before', lambda n,d: n.type=='assignment_expression' and r.text(n.child_by_field_name('left'),d)=='target[name]')
            safeguard='Every enumerated key skips __proto__. The source-bound plain-record predicate rejects functions; recursive non-record targets are replaced with fresh records and arrays are cloned.'
        elif spec['rule']=='guarded-json-override/1':
            write=select('before', lambda n,d: n.type=='call_expression' and r.text(n,d)=='override(src, val)')
            guard=select('after', lambda n,d: n.type=='if_statement' and "key == '__proto__'" in r.text(n.child_by_field_name('condition'),d))
            scope='Prototype pollution through the exact source-bound override function on separate finite acyclic plain JSON records and arrays. Special values, accessors, proxies, aliases to shared prototypes and later consumers are outside this scope.'
            return {'method':'static_closed_world','mechanism':'prototype_pollution','rule_id':spec['rule'],
                    'violated_invariant':'An untrusted own JSON key must not select an inherited destination prototype as the target of a recursive merge.',
                    'attacker_control':'The supplied source records control own JSON property names and nested JSON values.',
                    'preconditions':['At least a destination and a source are supplied; they are separate finite acyclic plain JSON records or arrays with ordinary data properties, standard prototypes and no aliases to shared prototypes.',
                                     'Object.keys, Array iteration and the Buffer, Date and RegExp intrinsics are unmodified. JSON values are not instances of those special classes, so the exact source-bound special-value predicate returns false.',
                                     'Self-recursion resolves to this exact override function and the source-bound helper definitions included on the corresponding snapshot side.',
                                     'The vulnerable source contains an own __proto__ object key; benign equivalence covers ordinary JSON keys excluding __proto__, constructor and prototype.'],
                    'evidence_spans':[{'quote':r.text(write,bd)}],'condition_scope':scope,
                    'supported_impact':'The vulnerable operation can recurse into the inherited Object.prototype and assign attacker-selected properties there.',
                    'behavioral_intent':'Merge the same ordinary JSON records and array elements recursively while preserving object prototypes.',
                    'fixed_review':{'scope':scope,'safeguard':'Each Object.keys callback returns for __proto__, constructor and prototype before destination lookup, source lookup, recursion or assignment. Array iteration visits numeric own indices. The remaining exact recursive operation preserves the JSON-input invariant at every depth.',
                                    'evidence':[{'quote':r.text(guard,ad)}]},'repair_valid':True}
        else:
            rule='spread-own-defaults/1'
            guard=select('after', lambda n,d: n.type=='object' and any(c.type=='spread_element' for c in n.named_children))
            write=select('before', lambda n,d: n.type=='call_expression' and r.text(n.child_by_field_name('function'),d)=='Object.assign')
            safeguard='Object spread defines own properties without invoking the inherited __proto__ setter. The merge blocks meta keys; its source-bound plain-object predicate rejects inherited functions before recursion.'
        scope='Prototype pollution while merging finite acyclic plain JSON records with standard built-in prototypes; custom objects, proxies, accessors, symbols and user merge callbacks are outside this scope.'
        return {'method':'static_closed_world','mechanism':'prototype_pollution','rule_id':rule,
                'violated_invariant':'Untrusted own JSON property names must not modify global prototypes or become the prototype of the result.',
                'attacker_control':'The supplied source/default records contain attacker-selected own JSON property names and values.',
                'preconditions':['Inputs are finite acyclic JSON records or JSON arrays with ordinary data properties; they contain no preexisting references to shared prototypes.',
                                 'Standard Object/Array intrinsics and the exact source-bound helper/import definitions apply.',
                                 'For defu, ' + matched['after']['bindings'].get('merger', 'merger') + ' is undefined and ' + matched['after']['bindings'].get('namespace', 'namespace') + ' uses its default; no custom callback participates.',
                                 'An input contains an own __proto__ object key; ordinary benign keys exclude the reserved meta keys.'],
                'evidence_spans':[{'quote':r.text(write,bd)}],'condition_scope':scope,
                'supported_impact':'An own __proto__ entry can alter the merged result prototype or reach Object.prototype through the vulnerable operation; the scope does not certify downstream use.',
                'behavioral_intent':'Merge the same ordinary JSON records while preserving object prototypes and avoiding shared inherited-state mutation.',
                'fixed_review':{'scope':scope,'safeguard':safeguard,'evidence':[{'quote':r.text(guard,ad)}]},'repair_valid':True}
    return None


def missing_facts(row):
    """Exact recognized source forms may request missing repository facts.

    A supplied but different helper is unsupported semantics, not weak prose or
    a license to replace evidence. Recovery still resolves every request from
    the actual import graph at the pinned revision and within the same budget.
    """
    from . import evidence_ast,evidence_rules as r
    before=evidence_ast.parse(row.get('language'),row.get('vulnerable_code'))
    after=evidence_ast.parse(row.get('language'),row.get('fixed_code'))
    if before is None or after is None or not r._has_cwe(row,'CWE-1321'):return []
    stat=FORMS.stat()
    for spec in _forms(stat.st_mtime_ns,stat.st_size):
        if match_form_pair(before, after, spec, row.get('language')) is None:continue
        gaps=[]
        for side in ('before','after'):
            value=row.get('context_'+side)
            try:context=json.loads(value) if isinstance(value,str) else value or {}
            except ValueError:return []
            if not isinstance(context,dict):return []
            scope_state=_exact_module_form(row,side,spec,context)
            if scope_state is False:return []
            if scope_state is None:gaps.append(side+':scope:module')
            items=context.get('items') or []
            helpers=spec.get('helpers_'+side,spec.get('helpers',[]))
            for helper in helpers:
                matches=[i for i in items if i.get('kind') in ('callee','module_symbol') and i.get('name')==helper['name'] and i.get('path')==helper['path']]
                scope_missing=helper['path']!=row.get('file_path_'+side) and not any(i.get('kind')=='module_scope' and i.get('path')==helper['path'] for i in items)
                if matches:
                    parsed=evidence_ast.parse(row.get('language'),matches[0].get('text'))
                    if len({i.get('text') for i in matches})!=1 or parsed is None or r._template_bindings(parsed,helper['text'],row.get('language'),set()) is None:
                        return []
                    if not scope_missing:continue
                main=row.get('file_path_'+side)
                if main!=spec['path']:return []
                origins={main} if helper['path']==main or _calls(spec['before' if side=='before' else 'after'],helper['name'],row.get('language')) else set()
                origins|={h['path'] for h in helpers if h['path']!=helper['path'] and _calls(h['text'],helper['name'],row.get('language'))
                          and any(i.get('name')==h['name'] and i.get('path')==h['path'] for i in items)}
                if len(origins)==1:
                    origin=next(iter(origins))
                    gaps.append(side+':definition:'+helper['name']+(':'+origin if origin!=main else ''))
        return sorted(set(gaps))
    return []
