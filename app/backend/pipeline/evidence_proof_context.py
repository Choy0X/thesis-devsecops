"""Transient exact-source inputs for proofs, separate from model context budgets."""
from __future__ import annotations

import copy
import hashlib
import json

from . import evidence_ast, evidence_exports, evidence_module_witness, evidence_recursive, evidence_rules

SCHEMA = 'exact-proof-context/1'


def _provider(row, revision):
    from . import evidence_recovery, security_snapshots
    return security_snapshots.snapshot_for(evidence_recovery._source_row(row), revision)


def prepare(record, *, provider=None):
    provider = provider or _provider
    language = record.get('language')
    parsed = [evidence_ast.parse(language, record.get(key)) for key in ('vulnerable_code', 'fixed_code')]
    spec = None
    if all(parsed):
        matches = evidence_recursive.matching_forms(*parsed, language)
        if len(matches) > 1:
            return {'schema': SCHEMA, 'state': 'unsupported', 'cause': 'ambiguous_mechanism_form'}
        spec = matches[0] if matches else None
    out, bindings, helpers = copy.deepcopy(record), [], {}
    for side, revision in (('before', record.get('relevant_parent_commit')), ('after', record.get('fix_commit'))):
        witness = evidence_module_witness.resolve(record, side, provider=provider)
        if witness['state'] != 'available':
            return {'schema': SCHEMA, 'state': 'unsupported', 'cause': side + ':' + witness['cause']}
        bindings.append(witness['binding'])
        context = json.loads(record.get('context_' + side) or '{}')
        wanted = (spec or {}).get('helpers_' + side, (spec or {}).get('helpers', []))
        modules = {witness['binding']['path']: witness['module_text']}
        helper_items = []
        helpers[side] = wanted
        for helper in wanted:
            path = helper['path']
            if path not in modules:
                try:
                    snapshot = provider(record, revision)
                    modules[path] = snapshot.read(path) if snapshot.available and snapshot.revision == revision else None
                except (OSError, ValueError, TimeoutError):
                    modules[path] = None
            source = modules[path]
            if not isinstance(source, str):
                return {'schema': SCHEMA, 'state': 'unsupported', 'cause': side + ':helper_module_unavailable'}
            declaration = evidence_exports.local_definition(source, language, helper['name'])
            if not declaration:
                # local_definition is deliberately strict about exports and
                # initialization. Existing selected declarations still require
                # exact source binding in the closure verifier below.
                matching = [item.get('text') for item in context.get('items', [])
                            if item.get('name') == helper['name'] and item.get('path') == path
                            and item.get('kind') in ('callee', 'module_symbol')]
                declaration = matching[0] if len(set(matching)) == 1 else None
            if not isinstance(declaration, str) or source.count(declaration) != 1:
                return {'schema': SCHEMA, 'state': 'unsupported', 'cause': side + ':helper_declaration_unresolved'}
            helper_items.append({'kind':'module_symbol', 'name':helper['name'], 'path':path, 'text':declaration})
        # These full modules exist only in the transient proof copy. The
        # original selected context budget and its omissions are never edited.
        items = [{'kind':'module_scope', 'path':path, 'text':text} for path, text in modules.items()]
        items.append({'kind':'imports', 'path':witness['binding']['path'], 'text':witness['module_text']})
        snapshot = copy.deepcopy(context.get('snapshot') or {})
        snapshot['files'] = {**snapshot.get('files', {}), **{
            path: hashlib.sha256(text.encode()).hexdigest() for path, text in modules.items()}}
        out['context_' + side] = json.dumps({**context, 'items':items + helper_items, 'snapshot':snapshot})
    return {'schema': SCHEMA, 'state': 'available', 'record': out, 'helpers':helpers, 'bindings':bindings}
