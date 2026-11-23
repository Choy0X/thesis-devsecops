"""Indexed evidence funnel and resumable, append-only exact-source recovery.

The index contains metadata only. Source bodies are streamed in bounded batches
only to build an index or execute explicit recovery work. Repairs never edit raw
sources, and completed background attempts are not themselves security proof.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import posixpath
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager

from . import versions

_LOCK = threading.RLock()
_MANIFESTS = {}


def _root(root=None):
    return pathlib.Path(root if root is not None else versions.PROC).resolve()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _object(value, fallback=None):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


@contextmanager
def _connect(root=None):
    root = _root(root)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / '.evidence_recovery.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=30000')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS indexes (identity TEXT PRIMARY KEY, summary TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evidence_rows (identity TEXT NOT NULL, ordinal INTEGER NOT NULL, record_id TEXT, pair_key TEXT, body TEXT NOT NULL, PRIMARY KEY(identity,ordinal));
        CREATE TABLE IF NOT EXISTS repairs (repair_id TEXT PRIMARY KEY, pair_key TEXT, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, tag TEXT, action TEXT, status TEXT, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS checkpoints (input_key TEXT NOT NULL, record_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(input_key,record_id));
    ''')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _implementation_fingerprint():
    from . import automatic_admission, automatic_evidence, build_context_cache, context_builder, evidence_ast, evidence_rules, evidence_shell, evidence_recursive, evidence_exports, evidence_source_scope, frozen_evidence_build, funcsplit, security_snapshots
    from .verify import automatic, closure_diagnostics, guard_facts
    from .verify import proof_ledger
    from . import npm_evidence_sources, npm_package_scope, data_qualification, security_quality, validation_exposure, source_fingerprints
    from . import automatic_evidence_v3, evidence_module_witness, evidence_binding_proof, evidence_proof_context, evidence_source_supersession
    from .views import core as view_core, catalog as view_catalog
    from .views import rendering as view_rendering
    from . import evidence_recursive_filter
    modules = (automatic_admission, automatic_evidence, build_context_cache, context_builder, evidence_ast, evidence_rules, evidence_shell, evidence_recursive, evidence_exports, evidence_source_scope, frozen_evidence_build, funcsplit, security_snapshots, automatic, proof_ledger, closure_diagnostics, guard_facts, npm_evidence_sources, npm_package_scope, data_qualification, security_quality, validation_exposure,
               automatic_evidence_v3, evidence_module_witness, evidence_binding_proof, evidence_proof_context, evidence_source_supersession,
               view_core, view_catalog, view_rendering, evidence_recursive_filter, source_fingerprints)
    return {'policy': automatic_evidence.CURRENT_POLICY, 'grammar': evidence_ast.grammar_signature(), 'recursive_forms':hashlib.sha256(evidence_recursive.FORMS.read_bytes()).hexdigest(),
            'context': build_context_cache.signature(),
            'recovery': hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
            'implementation': {m.__name__: hashlib.sha256(pathlib.Path(m.__file__).read_bytes()).hexdigest() for m in modules}}


def evidence_fingerprint(root=None):
    """Only policy implementation, grammar, authenticated proof and repair content."""
    from .verify.proof_ledger import ProofLedger
    ledger = ProofLedger()
    proofs = sorted(_digest({'binding': p.get('binding'), 'facts': p.get('facts'), 'observations': p.get('observations'),
                              'schema': p.get('schema'), 'verdict': p.get('verdict')}) for p in ledger.completed({}))
    path = _root(root) / '.evidence_recovery.sqlite3'
    repairs = []
    if path.exists():
        with _connect(root) as db:
            repairs = [row[0] for row in db.execute('SELECT repair_id FROM repairs ORDER BY repair_id')]
    from . import validation_exposure
    return _digest({**_implementation_fingerprint(), 'proofs': proofs, 'repairs': repairs,
                    'validator_development': {'version': validation_exposure.VERSION,
                                              'records': sorted(validation_exposure.RECORD_IDS)}})


def save_repair(record, changes, *, root=None):
    """Persist a deterministic repair against the complete original unit binding."""
    from .verify.automatic import source_binding
    if set(changes) - {'context_before', 'context_after'}:
        raise ValueError('only exact-source context overlays can be repaired')
    repair = {'schema': 'evidence-context-repair/2', 'pair_key': record.get('raw_pair_key'),
              'source_binding': source_binding(record), 'changes': copy.deepcopy(changes)}
    repair['repair_id'] = _digest(repair)
    with _connect(root) as db:
        db.execute('INSERT OR IGNORE INTO repairs VALUES (?,?,?)', (repair['repair_id'], repair['pair_key'], _json(repair)))
    return repair


def apply_repairs(record, repairs):
    from .verify.automatic import source_binding
    out = copy.deepcopy(record)
    candidates = repairs.get(record.get('raw_pair_key')) if isinstance(repairs, dict) else None
    candidates = candidates if isinstance(candidates,list) else [candidates] if candidates else []
    used=set()
    # Prefer the latest applicable full overlay, then follow any source-bound
    # continuation. A stale unrelated version cannot hide an earlier valid one.
    for _ in range(min(len(candidates),64)):
        binding=source_binding(out)
        applicable=[r for r in candidates if isinstance(r,dict) and id(r) not in used and r.get('source_binding')==binding]
        if not applicable:break
        for repair in applicable:
            used.add(id(repair))
            changes=repair.get('changes') or {}
            if set(changes)-{'context_before','context_after'}:
                raise ValueError('invalid source repair fields')
            out.update(copy.deepcopy(changes))
    return out


def repair_groups(repairs):
    result={}
    for repair in repairs:
        result.setdefault(repair['pair_key'],[]).append(repair)
    return result


def refresh_source(source, root=None):
    """Capture a new immutable envelope; the earlier snapshot is never modified."""
    from .verify.proof_ledger import ProofLedger
    # Frozen inputs are shared read-only. Copy the changed envelope only; a real
    # captured source can exceed 1 GB and must not be duplicated for metadata.
    out = dict(source)
    with _connect(root) as db:
        repairs = [json.loads(row[0]) for row in db.execute('SELECT body FROM repairs ORDER BY rowid')]
    by_pair = repair_groups(repairs)
    receipts = copy.deepcopy(out.get('verification_receipts') or {})
    for receipt in ProofLedger().completed({}):
        rid = (receipt.get('binding') or {}).get('record_id')
        if rid:
            old = receipts.setdefault(rid, [])
            if not any(p.get('receipt_id') == receipt.get('receipt_id') for p in old):
                old.append(receipt)
    out['verification_receipts'] = receipts
    out['derived_evidence'] = {'schema': 'derived-evidence/2', 'evidence_fingerprint': evidence_fingerprint(root), 'repairs': by_pair}
    return out


def _artifact(tag, root=None):
    from . import build_catalog
    row = build_catalog.get(tag, root=root)
    if row:
        tag = row['tag']
    if not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', tag):
        raise ValueError('bad build tag')
    directory = _root(root) / ('dataset_' + tag)
    manifest = directory / 'manifest.json'
    if not manifest.is_file():
        raise ValueError('build has no published evidence artifact')
    stat = manifest.stat()
    key = (str(manifest), stat.st_size, stat.st_mtime_ns)
    cached = _MANIFESTS.get(key)
    if cached is None:
        raw = manifest.read_bytes()
        cached = (hashlib.sha256(raw).hexdigest(), json.loads(raw))
        _MANIFESTS[key] = cached
    return directory, cached[0], cached[1]


def _records(directory, *, metadata=False):
    import pyarrow.parquet as pq
    path = directory / 'records.parquet'
    if not path.exists():
        return
    file = pq.ParquetFile(path)
    try:
        from .evidence_exclusion_audit import METADATA_FIELDS
        wanted = [*METADATA_FIELDS, 'verification_status']
        columns = [n for n in wanted if n in file.schema_arrow.names] if metadata else None
        # The generator stays suspended through source analysis and isolated
        # execution. Retaining 128 full source/context rows here prevents cache
        # cleanup from reclaiming them at that execution handoff.
        for batch in file.iter_batches(batch_size=128 if metadata else 1, columns=columns):
            rows = batch.to_pylist()
            del batch  # Native Arrow buffers are not needed after conversion.
            yield from rows
            del rows
    finally:
        file.close()


def _assert_source_available(directory, manifest):
    expected = (manifest.get('counts') or {}).get('records', (manifest.get('files', {}).get('records.parquet') or {}).get('rows', 0))
    if not (directory / 'records.parquet').is_file() and expected:
        raise ValueError('source records are unavailable; evidence cannot be indexed')


def _index_records(directory, identity, manifest, *, root=None):
    from .evidence_exclusion_audit import Accumulator
    _assert_source_available(directory, manifest)
    counts, reasons = Counter(), Counter()
    audit = Accumulator()
    with _connect(root) as db:
        if db.execute('SELECT 1 FROM indexes WHERE identity=?', (identity,)).fetchone():
            return
        for ordinal, record in enumerate(_records(directory, metadata=True)):
            outcome = record.get('admission_outcome') or ('insufficient_context' if record.get('label_state') == 'insufficient_context' else 'excluded')
            why = _object(record.get('admission_reasons'), [])
            why = why if isinstance(why, list) else []
            provenance = _object(record.get('admission_provenance'), {}) or {}
            item = {'record_id': record.get('record_id'), 'pair_key': record.get('raw_pair_key'), 'outcome': outcome,
                    'reasons': why, 'mechanism': provenance.get('mechanism'), 'proof_status': provenance.get('proof_method') or 'unverified',
                    'diagnostic': audit.add(record)}
            db.execute('INSERT INTO evidence_rows VALUES (?,?,?,?,?)', (identity, ordinal, item['record_id'], item['pair_key'], _json(item)))
            counts['records'] += 1
            counts[outcome] += 1
            reasons.update(why)
        counts.setdefault('records', 0)
        summary = {'counts': dict(counts), 'reason_counts': dict(reasons), 'admission_policy': manifest.get('admission_policy'),
                   'exclusion_audit': audit.report(qualification=(manifest.get('security_readiness') or {}).get('data_qualification')),
                   'coverage': {'indexed': counts['records'], 'total': (manifest.get('counts') or {}).get('records', counts['records'])}}
        db.execute('INSERT INTO indexes VALUES (?,?)', (identity, _json(summary)))


def evidence_page(tag, *, limit=100, offset=0, root=None):
    from . import evidence_exclusion_audit as audit
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    directory, identity, manifest = _artifact(tag, root)
    identity = _digest({'manifest': identity, 'audit_schema': audit.SCHEMA,
                        'audit_implementation': hashlib.sha256(pathlib.Path(audit.__file__).read_bytes()).hexdigest()})
    with _LOCK:
        with _connect(root) as db:
            ready = db.execute('SELECT summary FROM indexes WHERE identity=?', (identity,)).fetchone()
        if ready is None:
            _index_records(directory, identity, manifest, root=root)
        with _connect(root) as db:
            summary = json.loads(db.execute('SELECT summary FROM indexes WHERE identity=?', (identity,)).fetchone()[0])
            items = [json.loads(r[0]) for r in db.execute('SELECT body FROM evidence_rows WHERE identity=? ORDER BY ordinal LIMIT ? OFFSET ?', (identity, limit, offset))]
    return {'items': items, 'total': summary['counts']['records'], 'limit': limit, 'offset': offset, 'summary': summary}


def catalog_state(tag, root=None, *, _row=None, _fingerprint=None):
    from . import build_catalog
    from app.backend import jobs
    row = _row if _row is not None else build_catalog.get(tag, root=root)
    fingerprint = _fingerprint if _fingerprint is not None else evidence_fingerprint(root)
    job = next((j for j in jobs.running('evidence_recovery') if j.params.get('tag') == tag and pathlib.Path(j.params.get('build_root') or _root()).resolve() == _root(root)), None)
    artifact = (_root(root) / ('dataset_' + str((row or {}).get('tag') or tag)) / 'manifest.json').is_file()
    captured = bool(row and row.get('origin') == 'profile' and row.get('source'))
    unchanged = (row or {}).get('evidence_fingerprint') == fingerprint
    from training.finetune import quality_gate
    full_run = quality_gate.assess_manifest({'quality_profile': 'strict',
        'admission_policy': (row or {}).get('admission_policy'),
        'security_readiness': (row or {}).get('readiness')})
    ready_but_blocked = (row or {}).get('state') == 'ready' and not full_run['allowed']
    return {'evidence_actions': {
        'repair': {'available': artifact and job is None, 'reason': 'Evidence work is running.' if job else None if artifact else 'Publish a dataset artifact before repairing evidence.'},
        'verify': {'available': artifact and job is None, 'reason': 'Evidence work is running.' if job else None if artifact else 'No source artifact is available.'},
        'rebuild': {'available': captured and not unchanged and job is None and (ready_but_blocked or row.get('state') not in ('ready', 'queued', 'building')),
                    'reason': 'Evidence work is running.' if job else 'This historical artifact has no captured profile source.' if not captured else 'Evidence and policy are unchanged.' if unchanged else None}},
        'evidence_job_id': job.id if job else None, 'evidence_fingerprint': fingerprint,
        'evidence_artifact_available': artifact}


def _source_row(record):
    identity = record.get('repository_identity') or ''
    return {'repo_url': identity if '://' in identity else 'https://' + identity,
            'file_path': record.get('file_path_after'), 'old_path': record.get('file_path_before'),
            'parent_commit': record.get('relevant_parent_commit'), 'fix_commit': record.get('fix_commit'),
            'language': record.get('language'), 'function_name': record.get('symbol')}


def _exact_items(text, path, language, kind, name):
    """Read direct declarations only; a nested namesake cannot repair a binding."""
    from .verify import automatic
    from . import evidence_ast
    parsed = evidence_ast.parse_snapshot('typescript' if language in ('ts', 'typescript') else 'javascript', text)
    if not parsed:
        return []
    tree, raw = parsed
    found = []
    for node in tree.named_children:
        if node.type == 'export_statement':
            node = node.child_by_field_name('declaration')
            if node is None:
                continue
        value = automatic._text(node, raw)
        names = []
        if node.type == 'function_declaration':
            names = [automatic._text(node.child_by_field_name('name'), raw)]
        elif node.type in ('lexical_declaration', 'variable_declaration'):
            names = [automatic._text(child.child_by_field_name('name'), raw) for child in node.named_children if child.type == 'variable_declarator']
        if kind == 'binding':
            if node.type == 'import_statement' or (node.type in ('lexical_declaration', 'variable_declaration') and 'require(' in value.replace(' ', '')):
                candidate = {'kind': 'imports', 'path': path, 'name': None, 'text': value, 'reason': 'exact missing mechanism binding'}
                bindings = automatic._import_bindings({'file_path_before': path, 'language': language, 'context_before': {'items': [candidate]}}, 'before')
                if name in bindings:
                    found.append(candidate)
        elif kind == 'definition' and name in names:
            found.append({'kind': 'callee', 'path': path, 'name': name, 'text': value, 'reason': 'exact missing mechanism definition'})
    # Ambiguous direct declarations are not evidence of a particular binding.
    return found if len(found) == 1 else []


def _relative_definition(snapshot,path,language,name,*,seen=None,used=None,exported=False):
    """Follow only unambiguous relative named ES imports at the exact revision."""
    from . import evidence_ast,evidence_rules,evidence_exports,evidence_source_scope,security_snapshots
    seen = set() if seen is None else seen
    used = {} if used is None else used
    if (path,name) in seen or len(seen)>=8:
        return [],used,'relative_definition_cycle_or_budget'
    seen.add((path,name))
    source=snapshot.read(path)
    if source is None or len(source.encode())>security_snapshots.MAX_BLOB_BYTES:
        return [],used,'exact_dependency_unavailable'
    used[path]=source
    direct=_exact_items(source,path,language,'definition',name)
    scope={'kind':'module_scope','path':path,'name':None,'text':source,'reason':'exact named export and module binding scope'}
    if exported:
        binding=evidence_exports.bindings(source,language).get(name)
        recovered_definition = evidence_source_scope.exported_definition(source, language, name)
        if recovered_definition is not None:
            direct=[{'kind':'module_symbol','path':path,'name':name,'text':recovered_definition,
                     'reason':'Exact named export source; module semantics require independent validation.'}]
        elif not binding or binding.get('local')!=name:
            return [],used,'unsupported_export_binding'
        elif not binding.get('module'):
            definition=evidence_exports.local_definition(source,language,name)
            if definition is None:return [],used,'unsupported_export_binding'
            direct=[{'kind':'module_symbol','path':path,'name':name,'text':definition,'reason':'exact named export definition'}]
        else:direct=[]
    if direct:
        return ([scope,*direct] if exported else direct),used,None
    parsed=evidence_ast.parse_snapshot(language,source)
    if parsed is None:
        return [],used,'exact_dependency_parse_failure'
    tree,data=parsed
    references=[]
    for statement in evidence_rules.children(tree):
        if not exported and statement.type in ('lexical_declaration', 'variable_declaration'):
            from .verify import automatic
            snippet = evidence_rules.text(statement, data)
            edge = automatic._import_bindings({'language': language, 'file_path_before': path,
                'context_before': {'items': [{'kind': 'imports', 'path': path, 'text': snippet}]}}, 'before').get(name)
            if edge and edge[0].startswith(('./', '../')) and edge[1] != 'namespace':
                references.append((edge[0], edge[1], statement))
        if statement.type not in ('import_statement','export_statement'):
            continue
        if exported and statement.type!='export_statement':continue
        module=statement.child_by_field_name('source')
        raw=evidence_rules.text(module,data)
        if not re.fullmatch(r"['\"]\.{1,2}/[A-Za-z0-9_./@-]+['\"]",raw):
            continue
        for node in evidence_rules.walk(statement):
            if node.type not in ('import_specifier','export_specifier'):
                continue
            imported=evidence_rules.text(node.child_by_field_name('name'),data)
            local=evidence_rules.text(node.child_by_field_name('alias'),data) or imported
            if local==name and not any(n.type=='type' for n in statement.children) and not any(n.type=='type' for n in node.children):
                references.append((raw[1:-1],imported,statement))
    if len(references)!=1:
        return [],used,'exact_definition_unavailable'
    module,imported,statement=references[0]
    base=posixpath.normpath(posixpath.join(posixpath.dirname(path),module))
    if base.startswith(('../','/')) or ':' in base:
        return [],used,'unsafe_relative_definition'
    ext=posixpath.splitext(base)[1]
    candidates=[base] if ext else [base+s for s in ('.ts','.tsx','.js','.mjs','.cjs','/index.ts','/index.js')]
    if ext=='.js':
        candidates += [base[:-3]+'.ts',base[:-3]+'.tsx']
    present=[p for p in candidates if snapshot.read(p) is not None]
    if len(present)!=1:
        return [],used,'ambiguous_relative_definition' if present else 'exact_dependency_unavailable'
    items,used,reason=_relative_definition(snapshot,present[0],language,imported,seen=seen,used=used,exported=True)
    if reason:
        return [],used,reason
    link={'kind':'imports','path':path,'name':None,'text':evidence_rules.text(statement,data),
          'reason':'exact relative definition binding'}
    return ([scope,link,*items] if exported else [link,*items]),used,None


def _recover_missing_once(record, missing_facts=None):
    """Acquire only decisive missing facts from the two exact cached revisions.

    This routine has no network or LLM path. A missing repository, target mismatch
    or exhausted original context budget is retained as an explicit reason.
    """
    from . import context_builder, security_snapshots
    from .verify.automatic import diagnose_case
    from .evidence_recursive import missing_facts as source_missing
    working = copy.deepcopy(record)
    diagnosis = diagnose_case(working)
    missing = (source_missing(working) or diagnosis.get('missing_facts') or []) if missing_facts is None else missing_facts
    if not missing:
        return working, {'status': 'unchanged', 'reasons': [], 'requested_missing_facts': [], 'mechanism': diagnosis.get('mechanism')}
    reasons, changed, snapshots = [], False, {}
    # Each request names a concrete side, binding or definition. No caller search,
    # full context rehydration, source truncation, or broader collection is used.
    for fact in missing:
        parts = fact.split(':')
        if (len(parts) < 3 or parts[0] not in ('before', 'after') or parts[1] not in ('binding', 'definition', 'environment', 'scope')
                or parts[1] == 'scope' and (len(parts) != 3 or parts[2] != 'module')):
            reasons.append('unsupported_missing_fact:' + fact)
            continue
        side, kind, name = parts[:3]
        revision = record.get('relevant_parent_commit' if side == 'before' else 'fix_commit')
        path = record.get('file_path_' + side)
        code = record.get('vulnerable_code' if side == 'before' else 'fixed_code') or ''
        if not re.fullmatch(r'[0-9a-fA-F]{40,64}', str(revision or '')) or not path:
            reasons.append('exact_revision_or_path_missing:' + side)
            continue
        if side not in snapshots:
            snap = security_snapshots.snapshot_for(_source_row(record), revision)
            text = snap.read(path) if snap.available and snap.revision == revision else None
            snapshots[side] = (snap,text)
        snap,text = snapshots[side]
        if text is None:
            reasons.append('exact_source_unavailable:' + side + ':' + path)
            continue
        if len(text.encode()) > security_snapshots.MAX_BLOB_BYTES:
            reasons.append('source_file_budget_exceeded:' + side + ':' + path)
            continue
        if not code.strip() or code.strip() not in text:
            reasons.append('target_source_mismatch:' + side)
            continue
        candidates = ([{'kind':'module_scope','path':path,'name':None,'text':text,
                        'reason':'Exact immutable module scope establishes helper and recursive bindings.'}]
                      if kind == 'scope' else _exact_items(text, path, record.get('language'), kind, name))
        files={path:text}
        from . import evidence_source_scope
        if kind == 'environment':
            candidates, environment_files, failure = evidence_source_scope.environment_item(snap, path, name)
            if failure:
                reasons.append(failure + ':' + side + ':' + name)
                continue
            files.update(environment_files)
        if not candidates and kind == 'definition':
            scoped = evidence_source_scope.lexical_item(text, code, path, record.get('language'), name)
            if scoped:
                candidates = [scoped]
        if not candidates and kind=='definition':
            # A path hint must already be part of the retained source context.
            # It cannot name an unrelated repository file or another snapshot.
            context=_object(working.get('context_'+side),{}) or {}
            hint=parts[3] if len(parts)>3 else path
            allowed={path}|{i.get('path') for i in context.get('items',[]) if i.get('kind') in ('callee','module_symbol')}
            if hint not in allowed:
                reasons.append('unlinked_relative_definition:'+side+':'+name);continue
            candidates,files,failure=_relative_definition(snap,hint,record.get('language'),name)
            files[path]=text
            if failure:
                reasons.append(failure+':'+side+':'+name);continue
        if not candidates:
            reasons.append('exact_definition_unavailable:' + fact)
            continue
        bundle = copy.deepcopy(_object(working.get('context_' + side), {}) or {})
        items = bundle.setdefault('items', [])
        module_paths={i['path'] for i in candidates if i['kind'] in ('module_symbol','module_scope')}
        reclassified=False
        for item in items:
            if item.get('kind')=='callee' and item.get('path') in module_paths and item.get('path')!=path:
                item['kind']='module_symbol';reclassified=True
        addition = [item for item in candidates if not any(old.get('kind') == item['kind'] and old.get('text') == item['text'] and old.get('path') == item['path'] for old in items)]
        budget_values = bundle.get('budget') or {}
        allowed = context_builder.Budget.__dataclass_fields__
        budget = context_builder.Budget(**{k: int(v) for k, v in budget_values.items() if k in allowed})
        total = sum(len(i.get('text', '')) for i in [*items, *addition])
        by_kind=Counter()
        for item in [*items,*addition]:by_kind[item.get('kind')]+=len(item.get('text',''))
        if (total > budget.total or by_kind['imports']>budget.imports or by_kind['callee']>budget.callees
                or by_kind['module_symbol']+by_kind['module_scope']>budget.modules
                or by_kind['runtime_environment']>budget.config or by_kind['lexical_scope']>budget.enclosing):
            reasons.append('original_context_budget_exceeded:' + fact)
            continue
        if addition or reclassified:
            items.extend(addition)
            snapshot = bundle.setdefault('snapshot', {})
            snapshot.update(revision=revision, side=side, path=path, available=True,
                            target_sha256=hashlib.sha256(code.encode()).hexdigest(), file_sha256=hashlib.sha256(text.encode()).hexdigest())
            snapshot.setdefault('files', {}).update({p:hashlib.sha256(s.encode()).hexdigest() for p,s in files.items()})
            bundle['stats'] = {'items': len(items), 'chars': total, 'kinds': dict(Counter(i.get('kind') for i in items))}
            working['context_' + side] = _json(bundle)
            changed = True
    return working, {'status': 'repaired' if changed else 'unavailable', 'reasons': sorted(set(reasons)),
                     'requested_missing_facts': list(missing), 'mechanism': diagnosis.get('mechanism')}


def recover_facts(record, missing_facts):
    """Recover explicitly named proof dependencies with the same exact-source limits.

    This only adds source-bound context. A source rule must still establish the
    decision; obtaining a declaration never approves its semantics.
    """
    return _recover_missing_once(record, list(missing_facts))


def recover_imports(record):
    """Replace damaged aggregate import snippets from their exact source file.

    Original frozen records remain unchanged. Only a versioned context overlay
    is returned, and the selected context budget is never increased.
    """
    from . import context_builder, evidence_ast, evidence_rules, security_snapshots
    working, reasons, changed = copy.deepcopy(record), [], False
    for side in ('before', 'after'):
        revision = record.get('relevant_parent_commit' if side == 'before' else 'fix_commit')
        path, code = record.get('file_path_' + side), record.get('vulnerable_code' if side == 'before' else 'fixed_code')
        if not re.fullmatch(r'[0-9a-fA-F]{40}', str(revision or '')) or not path or not code:
            reasons.append('exact_revision_or_path_missing:' + side); continue
        snap = security_snapshots.snapshot_for(_source_row(record), revision)
        text = snap.read(path) if snap.available and snap.revision == revision else None
        if text is None:
            reasons.append('exact_source_unavailable:' + side + ':' + path); continue
        if len(text.encode()) > security_snapshots.MAX_BLOB_BYTES or code.strip() not in text:
            reasons.append('target_source_mismatch:' + side); continue
        parsed = evidence_ast.parse_snapshot(record.get('language'), text)
        if parsed is None:
            reasons.append('exact_source_parse_failure:' + side); continue
        root, raw = parsed
        additions = []
        for node in evidence_rules.children(root):
            statement = evidence_rules.text(node, raw)
            if node.type == 'import_statement' or (node.type in ('lexical_declaration', 'variable_declaration')
                                                  and 'require(' in statement.replace(' ', '')):
                additions.append({'kind':'imports','path':path,'name':None,'text':statement,
                                  'reason':'exact source import reconstruction'})
        bundle = copy.deepcopy(_object(working.get('context_' + side), {}) or {})
        old = bundle.get('items') or []
        items = [i for i in old if not (i.get('kind') == 'imports' and i.get('path') == path)] + additions
        allowed = context_builder.Budget.__dataclass_fields__
        budget = context_builder.Budget(**{k:int(v) for k,v in (bundle.get('budget') or {}).items() if k in allowed})
        total = sum(len(i.get('text','')) for i in items)
        if total > budget.total or sum(len(i.get('text','')) for i in items if i.get('kind') == 'imports') > budget.imports:
            reasons.append('original_context_budget_exceeded:' + side + ':imports'); continue
        if items != old:
            bundle['items'] = items
            snapshot = bundle.setdefault('snapshot', {})
            snapshot.update(revision=revision,side=side,path=path,available=True,
                            target_sha256=hashlib.sha256(code.encode()).hexdigest(),file_sha256=hashlib.sha256(text.encode()).hexdigest())
            snapshot.setdefault('files',{})[path] = hashlib.sha256(text.encode()).hexdigest()
            bundle['stats'] = {'items':len(items),'chars':total,'kinds':dict(Counter(i.get('kind') for i in items))}
            working['context_' + side] = _json(bundle); changed = True
    return working, {'status':'repaired' if changed else 'unavailable' if reasons else 'unchanged','reasons':sorted(set(reasons))}


def recover_record(record):
    """Close transitive missing facts within the original budget, without fetches."""
    working, changed = copy.deepcopy(record), False
    for _ in range(30):
        updated, result = _recover_missing_once(working)
        if updated == working:
            return working, {**result, 'status': 'repaired' if changed else result['status'],
                             'retryable': result['status'] == 'unavailable',
                             'remaining_missing_facts': list(result.get('requested_missing_facts') or [])}
        working, changed = updated, True
    return working, {'status': 'repaired', 'retryable': True, 'reasons': ['mechanism_closure_iteration_budget_exceeded']}


def acquire_missing_source(record, missing_facts):
    """Explicit worker-only acquisition of missing npm target blobs at exact pins.

    Fetching never chooses another repository, revision, caller or dependency.
    The recovered bytes must still pass source matching and context budget checks.
    """
    from . import security_snapshots
    from .ingest import codemine
    from urllib.parse import urlsplit
    row = _source_row(record)
    parsed = urlsplit(row['repo_url'])
    parts = parsed.path.strip('/').removesuffix('.git').split('/')
    if (record.get('ecosystem') != 'npm' or parsed.scheme != 'https' or parsed.hostname != 'github.com'
            or parsed.username or parsed.password or len(parts) != 2
            or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) or p in ('.', '..') for p in parts)
            or not all(re.fullmatch(r'[0-9a-fA-F]{40}', str(record.get(k) or '')) for k in ('fix_commit', 'relevant_parent_commit'))):
        return {'fetched': 0, 'reasons': ['targeted_acquisition_source_not_supported']}
    targets = {}
    for fact in missing_facts:
        side = fact.split(':')[0]
        if side in ('before', 'after'):
            path = record.get('file_path_' + side)
            try:
                safe = security_snapshots.safe_path(path or '')
            except ValueError:
                continue
            targets[(record['relevant_parent_commit'] if side == 'before' else record['fix_commit'], safe)] = side
    if not targets:
        return {'fetched': 0, 'reasons': ['no_exact_target_paths']}
    try:
        pointer = {'host': parsed.hostname, 'owner': parts[0], 'repo': parts[1], 'repo_url': row['repo_url'], 'sha': record['fix_commit']}
        commit = codemine.GitFetcher(batched=True).commit(pointer)
        if commit.sha != record['fix_commit'] or record['relevant_parent_commit'] not in commit.parents:
            return {'fetched': 0, 'reasons': ['exact_commit_parent_mismatch']}
        count, reasons = 0, []
        for (revision, path), side in targets.items():
            data = commit.loader.content(revision, path)
            if data is None:
                reasons.append('exact_source_unavailable:' + side + ':' + path)
            elif len(data) > security_snapshots.MAX_BLOB_BYTES:
                reasons.append('source_file_budget_exceeded:' + side + ':' + path)
            else:
                data.decode('utf-8')
                count += 1
        security_snapshots._git_snapshot.cache_clear()
        clear = getattr(security_snapshots.GitSnapshot.read, 'cache_clear', None)
        if clear:
            clear()
        return {'fetched': count, 'reasons': reasons}
    except (OSError, ValueError, codemine.FetchError) as exc:
        return {'fetched': 0, 'reasons': ['targeted_acquisition_failed:' + str(exc)[:500]]}


def _capability_fingerprint():
    from .verify import runtime
    from importlib.metadata import PackageNotFoundError, version
    libraries = {}
    for name in ('docker', 'tree-sitter', 'tree-sitter-javascript', 'tree-sitter-typescript'):
        try:
            libraries[name] = version(name)
        except PackageNotFoundError:
            libraries[name] = None
    capability = runtime.status()
    return _digest({'runtime': {k: capability.get(k) for k in ('runtime_id', 'image_id')}, 'dependencies': libraries})


def execute(tag, *, action='repair', root=None, progress=None, cancelled=None, attempt_id=None, source_capture=None,
            record_ids=None, expected_sources=None):
    from . import oslock
    from .verify import resource_budget
    attempt_id = attempt_id or uuid.uuid4().hex
    selected = None
    if record_ids is not None:
        if (not isinstance(record_ids, list) or not 1 <= len(record_ids) <= 500
                or any(not isinstance(value, str) or not 1 <= len(value) <= 512 for value in record_ids)):
            raise ValueError('Recovery selection must contain 1 to 500 source record identities.')
        selected = sorted(set(record_ids))
    if expected_sources is not None and (selected is None or not isinstance(expected_sources, dict)
            or set(expected_sources) != set(selected)
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in expected_sources.values())):
        raise ValueError('Expected source bindings must match the selected recovery records.')
    lock_dir = _root(root) / '.evidence_recovery_attempts'
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = oslock.FileLock(lock_dir / (_digest(attempt_id) + '.lock'), note=False)
    try:
        lock.acquire(wait=0)
    except oslock.Busy:
        return {'state': 'waiting', 'code': 'recovery_attempt_busy', 'attempt_id': attempt_id,
                'reason': 'Another worker still owns this recovery attempt.', 'processed': 0, 'total': None}
    try:
        if resource_budget.current() is None:
            with resource_budget.scope('evidence-recovery:' + attempt_id):
                return _execute(tag, action=action, root=root, progress=progress, cancelled=cancelled, attempt_id=attempt_id,
                                source_capture=source_capture, record_ids=selected, expected_sources=expected_sources)
        return _execute(tag, action=action, root=root, progress=progress, cancelled=cancelled, attempt_id=attempt_id,
                        source_capture=source_capture, record_ids=selected, expected_sources=expected_sources)
    finally:
        lock.release()


def _execute(tag, *, action='repair', root=None, progress=None, cancelled=None, attempt_id=None, source_capture=None,
             record_ids=None, expected_sources=None):
    """Explicit worker entry point with one durable checkpoint per source unit."""
    from .verify import automatic, resource_budget
    if action not in ('repair', 'verify_failed'):
        raise ValueError('action must be repair or verify_failed')
    directory, manifest_id, manifest = _artifact(tag, root)
    _assert_source_available(directory, manifest)
    if source_capture is not None:
        from . import frozen_evidence_build
        if source_capture.get('parent_tag') != tag:
            raise ValueError('Recovery source capture belongs to another immutable parent.')
        frozen_evidence_build._validated_parent(source_capture, _root(root))
    attempt_id = attempt_id or uuid.uuid4().hex
    counts, processed, resumed = Counter(), 0, 0
    prog = progress or (lambda **state: None)
    stop = cancelled or (lambda: False)
    total = None if source_capture and source_capture.get('candidate_source') else (manifest.get('counts') or {}).get('records')
    selected = set(record_ids) if record_ids is not None else None
    if selected is not None:
        total = len(selected)
    seen_selection = set()
    from . import evidence_recovery_history as history
    @contextmanager
    def local_guard():
        if stop():
            raise InterruptedError('Recovery was cancelled before persisting its item history.')
        yield
    history_guard = getattr(cancelled, 'publication_guard', None) or local_guard
    implementation = _implementation_fingerprint()
    history_attempt = history.start(attempt_id, tag=tag, action=action, root=_root(root),
        workflow_id=getattr(cancelled, 'workflow_id', None),
        phase_id=attempt_id if getattr(cancelled, 'workflow_id', None) else None,
        guard=history_guard, manifest_sha256=manifest_id, implementation=_digest(implementation))
    history_owner = history_attempt['owner']
    runtime_report = None
    reported = [None, 0.0]
    resource_checked = [0.0]
    def report(state, reason=None, *, code=None):
        label = 'Verification waiting for execution environment' if state == 'waiting' else 'Recovering exact source evidence' if action == 'repair' else 'Verifying mechanism evidence'
        payload = {'state': state, 'label': label,
                   'processed': processed, 'total': total, 'counts': dict(counts), 'resumed': resumed, 'reason': reason,
                   'attempt_id': attempt_id, 'runtime': runtime_report,
                   'resources': dict(resource_budget.current().last)}
        if code is not None:
            payload['code'] = code
        now = time.monotonic()
        if state == 'running' and reported[0] == state and now - reported[1] < 1:
            return payload
        reported[:] = [state, now]
        with _connect(root) as db:
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, body=excluded.body',
                       (attempt_id, tag, action, state, _json(payload)))
        if state in ('waiting', 'done', 'failed'):
            try:
                history.finish(attempt_id, state, root=_root(root), guard=history_guard, owner=history_owner)
            except InterruptedError:
                if not stop():
                    raise
        elif state == 'cancelled':
            history.invalidate(attempt_id, root=_root(root), state='cancelled',
                               reason=reason or 'Recovery was cancelled.', owner=history_owner)
        prog(**payload)
        return payload
    report('running')
    try:
        if action == 'verify_failed':
            from .verify import runtime
            def runtime_progress(value):
                nonlocal runtime_report
                runtime_report = value
                report('preparing_runtime', value.get('reason'))
            runtime_report = runtime.ensure_ready(progress=runtime_progress, cancelled=stop)
            if stop() or runtime_report.get('code') == 'cancelled':
                raise InterruptedError('Verification runtime preparation cancelled.')
            if runtime_report.get('state') != 'ready':
                return report('waiting', runtime_report.get('reason') or 'Verification runtime unavailable.',
                              code=runtime_report.get('code') or 'verification_runtime_waiting')
        # Checkpoints bind prepared inputs and capabilities, never job outputs.
        input_key = _digest({'manifest': manifest_id, 'implementation': implementation,
                             'capability': _capability_fingerprint(), 'action': action,
                             'captured_source': _digest(source_capture) if source_capture is not None else None})
        with _connect(root) as db:
            repairs = repair_groups(json.loads(row['body']) for row in db.execute('SELECT body FROM repairs ORDER BY rowid'))
        records = frozen_evidence_build.iter_records(source_capture, root=_root(root)) if source_capture is not None else _records(directory)
        for record in records:
            if selected is not None and record.get('record_id') not in selected:
                continue
            if selected is not None:
                seen_selection.add(record['record_id'])
            if time.monotonic() - resource_checked[0] >= 1:
                resource_budget.current().check(require_runtime=False)
                resource_checked[0] = time.monotonic()
            if stop():
                raise InterruptedError('Evidence work was cancelled; completed checkpoints are retained.')
            rid = record.get('record_id') or _digest(record)
            # Binding covers the target code, contexts, revisions and dependency manifests.
            subject = apply_repairs(record, repairs)
            recovered = subject
            repair_revision_id = None
            key = _digest({'input': input_key, 'source': automatic.source_binding(record if action == 'repair' else subject)})
            with _connect(root) as db:
                old = db.execute('SELECT body FROM checkpoints WHERE input_key=? AND record_id=?', (key, rid)).fetchone()
            result = json.loads(old[0]) if old else None
            stale = expected_sources is not None and automatic.source_binding(record)['source_sha256'] != expected_sources[rid]
            if stale:
                result = {'status': 'unavailable', 'retryable': False, 'failure_category': 'source_integrity', 'reasons': ['source_changed_since_attempt'],
                          'expected_source_sha256': expected_sources[rid]}
            if action == 'verify_failed' and result and result.get('status') == 'verified' and not automatic.trusted_receipt_for(subject):
                result = None
            cached = bool(result and not result.get('retryable') and result.get('status') not in ('env_error', 'unavailable', 'failed', 'not_verified'))
            if stale:
                pass
            elif cached:
                resumed += 1
            else:
                if action == 'repair':
                    repaired, result = recover_record(subject)
                    if any(reason.startswith('exact_source_unavailable:') for reason in result.get('reasons', [])):
                        diagnosis = automatic.diagnose_case(subject)
                        missing_sides = {reason.split(':')[1] for reason in result['reasons'] if reason.startswith('exact_source_unavailable:')}
                        targeted = [fact for fact in diagnosis.get('missing_facts', []) if fact.split(':')[0] in missing_sides]
                        acquired = acquire_missing_source(subject, targeted)
                        if acquired['fetched']:
                            repaired, result = recover_record(subject)
                        result['acquisition'] = acquired
                        result['reasons'] = sorted(set(result.get('reasons', []) + acquired['reasons']))
                    changes = {k: repaired[k] for k in ('context_before', 'context_after') if repaired.get(k) != record.get(k)}
                    if changes:
                        with history_guard(), history.owner_guard(attempt_id, root=_root(root), owner=history_owner):
                            saved_repair = save_repair(record, changes, root=root)
                        repair_revision_id = saved_repair['repair_id']
                    recovered = repaired
                else:
                    from . import automatic_admission
                    gate = {'failed': [reason for reason in record.get('admission_reasons', [])
                                       if reason in automatic_admission._HARD_GATE]}
                    from . import automatic_evidence
                    source_problems = set(automatic_evidence.analysis_source_problems(subject, gate, root=_root(root))) - {'attributed', 'link_evidence'}
                    if automatic_evidence.CURRENT_POLICY == 'automatic-evidence/3':
                        from .automatic_evidence_v3 import repairable_tangled
                        if repairable_tangled(subject):
                            # Match policy 3's analysis scope. A runtime receipt
                            # still needs its final full-module and repair proof.
                            source_problems.difference_update({'source_excluded', 'production_code'})
                    spec = None if source_problems else automatic.plan_case(subject, image_id=runtime_report['image_id'])
                    if source_problems:
                        result = {'status': 'excluded', 'reasons': sorted(source_problems)}
                    elif automatic.trusted_receipt_for(subject):
                        result = {'status': 'verified', 'reasons': []}
                    elif spec is None:
                        diagnosis = automatic.diagnose_case(subject)
                        result = {'status': 'unavailable' if diagnosis.get('missing_facts') else 'unsupported',
                                  'mechanism': diagnosis.get('mechanism'), 'reasons': diagnosis.get('missing_facts') or [diagnosis.get('reason')],
                                  'missing_facts': list(diagnosis.get('missing_facts') or [])}
                    else:
                        receipt = automatic.run_case(subject, spec)
                        result = {'status': receipt['verdict'], 'reasons': receipt.get('reasons') or []}
                with _connect(root) as db:
                    db.execute('INSERT INTO checkpoints VALUES (?,?,?) ON CONFLICT(input_key,record_id) DO UPDATE SET body=excluded.body', (key, rid, _json(result)))
            history.record(attempt_id, {**record, 'record_id': rid}, {**recovered, 'record_id': rid}, result, root=_root(root),
                guard=history_guard, cached=cached, repair_revision_id=repair_revision_id, owner=history_owner)
            counts[result['status']] += 1
            processed += 1
            report('running')
            if result['status'] == 'env_error':
                return report('waiting', '; '.join(result.get('reasons') or ['Verification environment became unavailable.']))
        if selected is not None and seen_selection != selected:
            raise ValueError('Selected source records are no longer present: ' + ', '.join(sorted(selected - seen_selection)[:5]))
        total = processed
        return report('waiting' if counts['env_error'] else 'done', 'Execution environment unavailable; retry when restored.' if counts['env_error'] else None)
    except resource_budget.ResourceWait as exc:
        return report('waiting', exc.code + ': ' + str(exc), code=exc.code)
    except InterruptedError:
        report('cancelled')
        raise
    except BaseException as exc:
        report('failed', str(exc))
        raise


def start(tag, *, action='repair', root=None, start_job=None):
    from app.backend import jobs
    if action not in ('repair', 'verify_failed'):
        raise ValueError('action must be repair or verify_failed')
    _artifact(tag, root)  # Validate without reading source bodies on the API thread.
    with _LOCK:
        state = catalog_state(tag, root)
        if state['evidence_job_id']:
            return {'job_id': state['evidence_job_id']}
        attempt = uuid.uuid4().hex
        params = {'tag': tag, 'action': action, 'build_root': str(_root(root)), 'attempt_id': attempt}
        with _connect(root) as db:
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?)', (attempt, tag, action, 'queued', _json(params)))
        try:
            job = (start_job or jobs.start)('evidence_recovery', params)
        except BaseException as exc:
            with _connect(root) as db:
                db.execute('UPDATE attempts SET status=?, body=? WHERE id=?', ('failed', _json({'error': str(exc)}), attempt))
            raise
        return {'job_id': job.id}
