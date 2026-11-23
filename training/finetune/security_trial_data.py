"""Deterministic, authored synthetic security supervision. No source is executed.

This small template corpus measures transfer between authored code shapes, not
independent security accuracy. Construction labels are not human review receipts.
Collected teaching publications are neither read nor admitted by this module.
"""
from __future__ import annotations

from collections import Counter
import difflib
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

SCHEMA = 'security-trial-dataset/1'
TAG = 'security_trial_v1'
MANIFEST = 'dataset_manifest.json'
LIMITS = [
    'Authored synthetic templates with shared mechanisms and vocabulary across partitions.',
    'Evaluation holds out code shapes, not independent projects, authors or mechanisms.',
    'Construction oracles assert the stated local invariant only; no human security review is claimed.',
    'Fixture programs and proposed regression tests were never executed.',
    'Patch applicability proves an exact edit only, not security or functional correctness.',
    'No collected teaching_v1 code, permission gate, review state or admission API is used.',
]


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _normalized(text):
    return _sha(re.sub(r'\s+', ' ', text).strip().encode())


def _wrap(expression, shape, language):
    """Each shape changes control or data flow, not just identifiers."""
    if language == 'python':
        prefix = 'import html\n\n'
        bodies = [
            f'    return {expression}\n',
            f'    if value is None:\n        return None\n    return {expression}\n',
            f'    result = None\n    for value in [value]:\n        result = {expression}\n    return result\n',
            f'    class Emitter:\n        def emit(self, value):\n            return {expression}\n    return Emitter().emit(value)\n',
            f'    def emit(value):\n        return {expression}\n    return list(map(emit, [value]))[0]\n',
            f'    try:\n        return {expression}\n    except TypeError:\n        raise\n',
            f'    actions = {{"emit": lambda value: {expression}}}\n    return actions["emit"](value)\n',
        ]
        return prefix + 'def handle(value, api):\n' + bodies[shape]
    bodies = [
        f'  return {expression};\n',
        f'  if (value === null) return null;\n  return {expression};\n',
        f'  let result;\n  for (const item of [value]) {{\n    value = item;\n    result = {expression};\n  }}\n  return result;\n',
        f'  const emitter = {{ emit(value) {{ return {expression}; }} }};\n  return emitter.emit(value);\n',
        f'  return [value].map(value => {expression})[0];\n',
        f'  try {{ return {expression}; }}\n  catch (error) {{ throw error; }}\n',
        f'  const actions = new Map([["emit", value => {expression}]]);\n  return actions.get("emit")(value);\n',
    ]
    return 'function handle(value, api) {\n' + bodies[shape] + '}\n'


def _mechanism(name, language):
    python = language == 'python'
    if name == 'shell':
        old = 'api("catalog " + value, shell=True)' if python else 'api.exec("catalog " + value)'
        new = 'api(["catalog", value], shell=False)' if python else 'api.execFile("catalog", [value])'
        return old, new, 'Shell command injection', 'Attacker-controlled data must remain one process argument, outside shell syntax.', 'Input changes shell command structure.', 'process argument boundary', 'api has subprocess.run semantics.' if python else 'api.exec and api.execFile have Node child_process semantics.'
    if name == 'sql':
        old = 'api.execute("SELECT id FROM members WHERE name = \'" + value + "\'")' if python else 'api.get("SELECT id FROM members WHERE name = \'" + value + "\'")'
        new = 'api.execute("SELECT id FROM members WHERE name = ?", (value,))' if python else 'api.get("SELECT id FROM members WHERE name = ?", [value])'
        return old, new, 'SQL injection', 'Attacker-controlled values must be bound separately from SQL syntax.', 'Input changes SQL query structure.', 'SQL value binding', 'api is an SQLite-style adapter: ? binds one value; a lone SQL string is parsed as syntax.'
    old = '"<p>" + value + "</p>"' if python else '(api.innerHTML = value)'
    new = '"<p>" + html.escape(value, quote=False) + "</p>"' if python else '(api.textContent = value)'
    return old, new, 'HTML markup injection', 'Untrusted display text must not be interpreted as HTML markup.', 'Input becomes attacker-controlled HTML markup.', 'HTML text versus markup', 'The returned string is inserted into an HTML body.' if python else 'api is a browser DOM element; innerHTML parses markup and textContent assigns literal text.'


def _test_code(mechanism, language):
    """Controlled adapter captures only. No real shell, database or browser."""
    if language == 'python':
        if mechanism == 'shell':
            return ('capture = []\n'
                    'def fake(*args, **kwargs):\n    capture.append((args, kwargs))\n'
                    'handle("two words", fake)\n'
                    'assert capture == [((["catalog", "two words"],), {"shell": False})]\n')
        if mechanism == 'sql':
            return ('capture = []\nclass Fake:\n'
                    '    def execute(self, *args):\n        capture.append(args)\n'
                    'handle("O\'Reilly", Fake())\n'
                    'assert capture == [("SELECT id FROM members WHERE name = ?", ("O\'Reilly",))]\n')
        return ('capture = [handle("<b>inert</b>", None), handle("hello", None)]\n'
                'assert capture == ["<p>&lt;b&gt;inert&lt;/b&gt;</p>", "<p>hello</p>"]\n')
    if mechanism == 'shell':
        return ('const capture = [];\nconst fake = {\n'
                '  exec: (...args) => capture.push(["exec", ...args]),\n'
                '  execFile: (...args) => capture.push(["execFile", ...args])\n};\n'
                'handle("two words", fake);\n'
                'assert.deepEqual(capture, [["execFile", "catalog", ["two words"]]]);\n')
    if mechanism == 'sql':
        return ('const capture = [];\nconst fake = {get: (...args) => capture.push(args)};\n'
                'handle("O\'Reilly", fake);\n'
                'assert.deepEqual(capture, [["SELECT id FROM members WHERE name = ?", ["O\'Reilly"]]]);\n')
    return ('const capture = [];\nconst fake = {\n'
            '  set innerHTML(value) { capture.push(["markup", value]); },\n'
            '  set textContent(value) { capture.push(["text", value]); }\n};\n'
            'handle("<b>inert</b>", fake);\nhandle("hello", fake);\n'
            'assert.deepEqual(capture, [["text", "<b>inert</b>"], ["text", "hello"]]);\n')


def build_rows():
    """Return records, views, source bytes and explicit construction oracles."""
    records, views, files, oracles = [], [], {}, []
    for mechanism in ('shell', 'sql', 'html'):
        for shape in range(7):
            # Six train shapes alternate languages. Held-out map dispatch uses JS
            # for shell/HTML and Python for SQL, never a renamed training shape.
            language = ('python' if shape % 2 == 0 else 'javascript') if shape < 6 else ('python' if mechanism == 'sql' else 'javascript')
            family = f'{mechanism}_shape_{shape}'
            split = 'train' if shape < 6 else 'val'
            path = f'fixtures/{family}.' + ('py' if language == 'python' else 'js')
            old, new, summary, invariant, impact, scope, api_fact = _mechanism(mechanism, language)
            hidden = 'api.apply(value)'
            sources = {side: _wrap(expr, shape, language) for side, expr in (('vulnerable', old), ('fixed', new), ('withheld', hidden))}
            revisions, descriptors = {}, {}
            for side, source in sources.items():
                raw = source.encode('utf-8')
                name = f'source/{family}/{side}.txt'
                files[name] = raw
                revision = hashlib.sha1(raw).hexdigest()
                revisions[side] = revision
                descriptors[side] = {'path': name, 'sha256': _sha(raw), 'bytes': len(raw),
                    'source_path': path, 'revision': revision, 'revision_kind': 'authored_content_identity'}
            rid = 'synthetic:' + family
            common_record = {'repository_identity': f'authored-synthetic://security-trial/{family}',
                'family_id': family, 'issue_group_id': family, 'language': language,
                'review_status': 'authored_construction_oracle_not_independent_review',
                'revision_kind': 'authored_content_identity', 'synthetic': True}
            records.append({**common_record, 'record_id': rid, 'label_state': 'supported',
                'relevant_parent_commit': revisions['vulnerable'], 'fix_commit': revisions['fixed'],
                'source_snapshots': _json([descriptors['vulnerable'], descriptors['fixed']])})
            records.append({**common_record, 'record_id': rid + ':withheld', 'label_state': 'insufficient_context',
                'relevant_parent_commit': revisions['withheld'], 'fix_commit': None,
                'source_snapshots': _json([descriptors['withheld']])})
            for task, side in [('discovery', 'vulnerable'), ('discovery', 'fixed'), ('repair', 'vulnerable'),
                               ('insufficient_context', 'withheld'), ('regression_test_design', 'vulnerable'),
                               ('regression_test_design', 'fixed')]:
                status = {'vulnerable': 'finding', 'fixed': 'no_finding', 'withheld': 'insufficient_context'}[side]
                quote = {'vulnerable': old, 'fixed': new, 'withheld': hidden}[side]
                answer = {'status': status, 'findings': [], 'evidence': [{'quote': quote,
                    'reason': invariant if side == 'vulnerable' else ('The shown operation preserves the stated boundary.' if side == 'fixed' else 'The decisive adapter implementation is absent.')}],
                    'assumptions': [], 'missing_context': [], 'validation_state': 'not_validated'}
                if side == 'vulnerable':
                    answer['findings'] = [{'summary': summary, 'location': path + ':handle',
                        'violated_invariant': invariant, 'attacker_control': 'value is untrusted user input.', 'impact': impact}]
                elif side == 'fixed':
                    answer['assumptions'] = ['Scoped conclusion for ' + scope + ' only; other risks are not assessed.']
                else:
                    answer['missing_context'] = ['Implementation of api.apply and its handling of untrusted value at the ' + scope + '.']
                context = f'[target] {path} ({language})\n{sources[side]}\n[scope]\nReview only {scope}. value is untrusted user input.\n'
                if side != 'withheld':
                    context += api_fact + '\n'
                else:
                    context += 'The implementation and contract of api.apply are withheld.\n'
                if task == 'repair':
                    answer['patch'] = ''.join(difflib.unified_diff(sources['vulnerable'].splitlines(True), sources['fixed'].splitlines(True), fromfile='a/' + path, tofile='b/' + path, n=2))
                    answer['behavioral_intent'] = 'Preserve ordinary input and the surrounding control flow while enforcing ' + scope + '.'
                if task == 'regression_test_design':
                    context += 'Propose a test for the boundary-preserving candidate. Inject only inert fake adapters; assume handle and assertion helpers are available. Tests are not run.\n'
                    answer['tests'] = [{'purpose': 'Check the boundary and ordinary input using captures only.',
                        'test_code': _test_code(mechanism, language),
                        'expected_observation': 'On the boundary-preserving version these assertions should pass; the vulnerable version should fail. No execution is claimed.',
                        'execution_state': 'not_run'}]
                pool = {'vulnerable': 'supported', 'fixed': 'scoped_negative', 'withheld': 'insufficient_context'}[side]
                views.append({'view_id': f'{family}:{task}:{side}', 'record_id': rid + (':withheld' if side == 'withheld' else ''),
                    'subject_id': f'{family}:{side}', 'family_id': family, 'issue_group_id': family,
                    'task': task, 'pool': pool, 'split': split, 'val_role': 'security_trial' if split == 'val' else '',
                    'eligible': True, 'weight': 1.0, 'context_text': context, 'response_json': _json(answer),
                    'source_revision': revisions[side], 'source_normalized_sha256': _normalized(sources[side]),
                    'allowed_patch_paths': [path], 'oracle_fixed_sha256': descriptors['fixed']['sha256'],
                    'lineage_json': _json({'origin': 'authored_synthetic', 'template_family': family, 'side': side,
                        'construction_oracle': mechanism + '_boundary_contrast_v1', 'human_review': False})})
            oracles.append({'family_id': family, 'split': split, 'language': language, 'mechanism': mechanism,
                'source_path': path, 'invariant': invariant, 'vulnerable_expression': old, 'fixed_expression': new,
                'decisive_fact_withheld': 'api.apply implementation and boundary semantics',
                'source_sha256': {side: descriptors[side]['sha256'] for side in sources},
                'oracle_kind': 'self_authored_construction', 'execution_state': 'not_run'})
    return records, views, files, oracles


def _validate(records, views, files, oracles):
    from training.finetune import sft_contracts
    from training.finetune.repair_validation import assess_patch
    if len(records) != 42 or len(views) != 126 or len(oracles) != 21:
        raise ValueError('Unexpected authored synthetic corpus membership')
    record_map = {record['record_id']: record for record in records}
    if len(record_map) != len(records) or len({view['view_id'] for view in views}) != len(views):
        raise ValueError('Duplicate corpus identity')
    groups, normalized = {}, {}
    for view in views:
        lineage = json.loads(view['lineage_json'])
        record = record_map[view['record_id']]
        if (lineage.get('origin') != 'authored_synthetic' or lineage.get('human_review') is not False
                or record.get('synthetic') is not True or view['eligible'] is not True or view['weight'] != 1.0):
            raise ValueError('Only authored synthetic origin is eligible')
        if view['split'] not in ('train', 'val') or view['val_role'] != ('security_trial' if view['split'] == 'val' else ''):
            raise ValueError('Invalid security trial split role')
        for group in (view['family_id'], view['issue_group_id'], lineage['template_family']):
            groups.setdefault(group, set()).add(view['split'])
        snapshot = [d for d in json.loads(record['source_snapshots']) if d['revision'] == view['source_revision']]
        if len(snapshot) != 1:
            raise ValueError('Ambiguous source revision')
        descriptor = snapshot[0]
        raw = files[descriptor['path']]
        source = raw.decode('utf-8')
        if (descriptor['revision_kind'] != 'authored_content_identity' or descriptor['sha256'] != _sha(raw)
                or descriptor['revision'] != hashlib.sha1(raw).hexdigest()
                or view['source_normalized_sha256'] != _normalized(source) or source not in view['context_text']):
            raise ValueError('Source identity or context differs')
        normalized.setdefault(_normalized(source), set()).add(view['split'])
        answer = json.loads(view['response_json'])
        if sft_contracts.validate_response(view['task'], answer):
            raise ValueError('Invalid response schema')
        if any(e['quote'] not in source for e in answer['evidence']):
            raise ValueError('Evidence quote is absent from exact source bytes')
        if view['task'] == 'repair':
            result = assess_patch(answer['patch'], {descriptor['source_path']: source}, allowed_paths=set(view['allowed_patch_paths']))
            if result['state'] != 'applies_exactly' or result['modified_sha256'] != {descriptor['source_path']: view['oracle_fixed_sha256']}:
                raise ValueError('Repair does not produce exact authored fixed source')
    if any(len(splits) != 1 for splits in [*groups.values(), *normalized.values()]):
        raise ValueError('Cross-split family or normalized-source leakage')
    if Counter(view['split'] for view in views) != {'train': 108, 'val': 18}:
        raise ValueError('Invalid bounded split membership')


def prepare_dataset(out_dir):
    """Create a fresh immutable-by-policy publication; never overwrite one."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    root = Path(out_dir)
    if root.exists():
        raise FileExistsError('Refusing to reuse a dataset publication: ' + str(root))
    records, views, files, oracles = build_rows()
    _validate(records, views, files, oracles)
    root.mkdir(parents=True, exist_ok=False)
    for name, raw in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    pq.write_table(pa.Table.from_pylist(records), root / 'records.parquet', compression='NONE')
    pq.write_table(pa.Table.from_pylist(views), root / 'views.parquet', compression='NONE')
    (root / 'construction_oracles.json').write_bytes((_json(oracles) + '\n').encode())
    (root / 'LIMITATIONS.txt').write_bytes(('\n'.join(LIMITS) + '\n').encode())
    inventory = {path.relative_to(root).as_posix(): {'sha256': _sha(path.read_bytes()), 'bytes': path.stat().st_size}
                 for path in sorted(root.rglob('*')) if path.is_file()}
    manifest = {'schema': SCHEMA, 'tag': TAG, 'dataset_status': 'frozen', 'synthetic': True,
        'independent_evaluation': False, 'origin': 'authored_synthetic', 'label_authority': 'self_authored_construction_oracles',
        'human_review': False, 'source_execution': 'not_run', 'collected_source_admission': 'not_called',
        'protocol': 'security_chat_v2', 'revision_kind': 'authored_content_identity',
        'counts': {'records': len(records), 'train_views': 108, 'heldout_views': 18, 'families': 21},
        'limitations': LIMITS, 'files': inventory}
    (root / MANIFEST).write_bytes((_json(manifest) + '\n').encode())
    verify_dataset(root)
    return manifest


def verify_dataset(path):
    """Authenticate every byte and compare eligibility/labels to this generator."""
    import pyarrow.parquet as pq
    path = Path(path)
    root = path.parent if path.name == MANIFEST else path
    manifest = json.loads((root / MANIFEST).read_bytes())
    if (manifest.get('schema') != SCHEMA or manifest.get('tag') != TAG or manifest.get('dataset_status') != 'frozen'
            or manifest.get('synthetic') is not True or manifest.get('independent_evaluation') is not False
            or manifest.get('collected_source_admission') != 'not_called' or manifest.get('human_review') is not False
            or manifest.get('protocol') != 'security_chat_v2' or manifest.get('origin') != 'authored_synthetic'
            or manifest.get('revision_kind') != 'authored_content_identity'
            or manifest.get('label_authority') != 'self_authored_construction_oracles'
            or manifest.get('source_execution') != 'not_run' or manifest.get('limitations') != LIMITS
            or manifest.get('counts') != {'records': 42, 'train_views': 108, 'heldout_views': 18, 'families': 21}):
        raise ValueError('Invalid authored synthetic dataset manifest')
    inventory = manifest.get('files')
    if not isinstance(inventory, dict) or not 4 <= len(inventory) <= 100:
        raise ValueError('Invalid dataset inventory')
    expected_records, expected_views, expected_files, expected_oracles = build_rows()
    expected_members = set(expected_files) | {'records.parquet', 'views.parquet', 'construction_oracles.json', 'LIMITATIONS.txt'}
    if set(inventory) != expected_members:
        raise ValueError('Dataset inventory differs from authored membership')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual != set(inventory) | {MANIFEST}:
        raise ValueError('Dataset inventory differs')
    files = {}
    for name, identity in inventory.items():
        rel = PurePosixPath(name)
        if not name or rel.is_absolute() or '\\' in name or ':' in name or '..' in rel.parts:
            raise ValueError('Unsafe dataset inventory path')
        member = root.joinpath(*rel.parts)
        if (not member.is_file() or member.is_symlink() or not member.resolve().is_relative_to(root.resolve())
                or any(parent.is_symlink() for parent in member.parents if parent.is_relative_to(root))):
            raise ValueError('Unsafe dataset inventory member')
        raw = member.read_bytes()
        if len(raw) > 4 << 20 or identity != {'sha256': _sha(raw), 'bytes': len(raw)}:
            raise ValueError('Dataset file identity differs: ' + name)
        files[name] = raw
    records = pq.read_table(root / 'records.parquet').to_pylist()
    views = pq.read_table(root / 'views.parquet').to_pylist()
    oracles = json.loads(files['construction_oracles.json'])
    _validate(records, views, files, oracles)
    if (records != expected_records or views != expected_views or oracles != expected_oracles
            or files['LIMITATIONS.txt'] != ('\n'.join(LIMITS) + '\n').encode()
            or any(files.get(name) != raw for name, raw in expected_files.items())):
        raise ValueError('Corpus differs from the authored synthetic construction oracle')
    return manifest
