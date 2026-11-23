"""Durable, byte-bounded JSON exports. One selection, any number of files.

Only complete published packets are checkpoints. Unpublished spool data can be
discarded on resume; existing packets and their source identities never change.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid

from app.backend.pipeline import context_exchange as exchange_mod, fsio, util
from app.backend.pipeline.oslock import FileLock
from app.backend.pipeline.context_limits import DEFAULT_BYTES, file_bytes

MAX_BYTES = DEFAULT_BYTES
READ_ITEMS = 8  # Source-loading bound, never an output item-count boundary.


def identity(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
        raise ValueError('invalid export or upload ID')
    return value


def encode(value):
    return exchange_mod.canonical(value).encode('utf-8')


class JsonExports:
    export_stage = 'context_review_export'
    def __init__(self, exchange):
        self.exchange = exchange
        self.root = exchange.root / 'json_exports'

    def directory(self, eid):
        return self.root / identity(eid)

    def _read(self, eid):
        return json.loads((self.directory(eid) / 'selection.json').read_bytes())

    def _save(self, doc):
        self.exchange._save(self.directory(doc['export_id']) / 'selection.json', doc)

    def create(self, ids, *, campaign_id=None, max_file_bytes=None):
        limit = file_bytes(MAX_BYTES if max_file_bytes is None else max_file_bytes)
        if (not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('select distinct positive integer pair IDs')
        if campaign_id is not None and (not isinstance(campaign_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', campaign_id)):
            raise ValueError('invalid campaign ID')
        doc = {'export_id': uuid.uuid4().hex, 'campaign_id': campaign_id, 'ids': sorted(ids),
               'created_at': util.now_utc(), 'state': 'pending', 'files': [], 'failures': [], 'skipped': [],
               'processed': 0, 'elapsed_seconds': 0, 'job_id': None, 'max_file_bytes': limit}
        self._save(doc)
        return self.status(doc['export_id'])

    def status(self, eid):
        doc = self._read(eid)
        result = {k: v for k, v in doc.items() if k != 'ids'}
        result.update(total=len(doc['ids']), max_file_bytes=doc.get('max_file_bytes', DEFAULT_BYTES),
                      prompt=getattr(self.exchange, 'expert_prompt', exchange_mod.EXPERT_PROMPT),
                      review_status=getattr(self.exchange, 'review_status', 'model_draft'), automatic_upload=False)
        if doc['state'] == 'running' and doc.get('updated_at_epoch'):
            result['elapsed_seconds'] += max(0, time.time() - doc['updated_at_epoch'])
        result['files'] = [{k: v for k, v in f.items() if k != 'pair_ids'} for f in doc['files']]
        return result

    def launch(self, eid):
        from app.backend import jobs
        with FileLock(self.directory(eid) / '.prepare.lock', note=False):
            doc = self._read(eid)
            job = jobs.get(doc.get('job_id')) if doc.get('job_id') else None
            if job is not None and job.status == 'running':
                raise ValueError('This export already has a running job')
            if doc['state'] == 'complete':
                return self.status(eid)
            doc.update(state='queued', failures=[])
            self._save(doc)
            job = jobs.start(self.export_stage, {'export_id': eid})
            doc['job_id'] = job.id
            self._save(doc)
        return self.status(eid)

    def reconcile(self, eid):
        from app.backend import jobs
        result = self.status(eid)
        job = jobs.get(result.get('job_id')) if result.get('job_id') else None
        if result['state'] in ('running', 'queued') and (job is None or job.status != 'running'):
            # This is a read-time projection; only explicit resume changes state.
            result['state'] = 'interrupted'
            result['elapsed_seconds'] = self._read(eid)['elapsed_seconds']
        return result

    def _envelope(self, doc, index):
        pid = uuid.uuid5(uuid.UUID(doc['export_id']), str(index)).hex
        header = {'schema': getattr(self.exchange, 'packet_schema', exchange_mod.EXPORT_SCHEMA), 'packet_id': pid,
                  'prompt_version': getattr(self.exchange, 'prompt_version', exchange_mod.PROMPT_VERSION),
                  'expert_prompt': getattr(self.exchange, 'expert_prompt', exchange_mod.EXPERT_PROMPT),
                  'created_at': doc['created_at'], 'campaign_id': doc['campaign_id'],
                  'export_id': doc['export_id'], 'max_file_bytes': doc.get('max_file_bytes', DEFAULT_BYTES)}
        if self.exchange.source_policy:
            header['source_policy'] = self.exchange.source_policy
        prefix = encode(header)[:-1] + b',"items":['
        middle = b'],"response_template":' + encode({'schema': getattr(self.exchange, 'result_schema', exchange_mod.RESULT_SCHEMA), 'packet_id': pid})[:-1] + b',"results":['
        return pid, prefix, middle, b']}}'

    def _record(self, doc, packet, path):
        items = packet['items']
        if doc['campaign_id']:
            with self.exchange.locked():
                campaign = self.exchange._campaign(doc['campaign_id'])
                for item in items:
                    prior = campaign['items'].get(str(item['pair_id']), {})
                    # Crash recovery must not replace an imported outcome.
                    if prior.get('packet_id') == packet['packet_id']:
                        continue
                    campaign['items'][str(item['pair_id'])] = {
                        'packet_id': packet['packet_id'], 'source_sha256': item['source_sha256'],
                        'item_id': item['item_id'], 'status': 'awaiting_external',
                        'previous': {k: v for k, v in prior.items() if k != 'previous'}}
                self.exchange._save(self.exchange._campaign_path(doc['campaign_id']), campaign)
        doc['files'].append({'index': len(doc['files']), 'packet_id': packet['packet_id'],
                             'name': f"context-{doc['export_id']}-{len(doc['files']) + 1:04d}.json",
                             'bytes': path.stat().st_size, 'item_count': len(items),
                             'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                             'pair_ids': [i['pair_id'] for i in items]})
        self._save(doc)

    def prepare(self, eid, *, progress=lambda **state: None, should_stop=lambda: False):
        from app.backend.pipeline import security_snapshots
        from app.backend.pipeline.acceleration.context_gate import require_ready
        directory = self.directory(eid)
        with FileLock(directory / '.prepare.lock', note=False):
            doc = self._read(eid)
            limit = file_bytes(doc.get('max_file_bytes', DEFAULT_BYTES))
            doc.setdefault('skipped', [])
            if doc['state'] == 'complete':
                return self.status(eid)
            began, prior_elapsed = time.monotonic(), doc['elapsed_seconds']
            doc.update(state='running', failures=[], updated_at_epoch=time.time())
            self._save(doc)
            pending, item_bytes, template_bytes = [], 0, 0
            item_spool, template_spool = directory / 'items.spool', directory / 'templates.spool'

            def update():
                doc['elapsed_seconds'] = prior_elapsed + time.monotonic() - began
                doc['updated_at_epoch'] = time.time()
                self._save(doc)
                progress(state=doc['state'], processed=doc['processed'], total=len(doc['ids']),
                         counts={'files': len(doc['files']), 'failures': len(doc['failures'])},
                         elapsed_seconds=doc['elapsed_seconds'], label='Preparing selected evidence as JSON')

            def publish():
                nonlocal pending, item_bytes, template_bytes
                if not pending:
                    return
                pid, prefix, middle, suffix = self._envelope(doc, len(doc['files']))
                path = self.exchange._path(pid)
                def writer(part):
                    with part.open('wb') as target:
                        target.write(prefix)
                        with item_spool.open('rb') as source:
                            shutil.copyfileobj(source, target, length=256 * 1024)
                        target.write(middle)
                        with template_spool.open('rb') as source:
                            shutil.copyfileobj(source, target, length=256 * 1024)
                        target.write(suffix)
                if path.exists():
                    raise ValueError('An unrecorded packet requires recovery before publishing')
                fsio.write_with(path, writer)
                if path.stat().st_size > limit:
                    raise ValueError('Internal packet size accounting failed')
                # Only the compact identity receipts are needed for registration.
                self._record(doc, {'packet_id': pid, 'items': pending}, path)
                pending, item_bytes, template_bytes = [], 0, 0
                item_spool.write_bytes(b''); template_spool.write_bytes(b'')
                update()

            try:
                # A crash between atomic publication and its receipt is recoverable.
                pid = self._envelope(doc, len(doc['files']))[0]
                orphan = self.exchange._path(pid)
                if orphan.exists():
                    packet = json.loads(orphan.read_bytes())
                    if packet.get('export_id') != eid or packet.get('packet_id') != pid or orphan.stat().st_size > limit:
                        raise ValueError('Unrecorded export packet is corrupt')
                    self._record(doc, packet, orphan)
                done = {pid for f in doc['files'] for pid in f['pair_ids']} | {item['pair_id'] for item in doc['skipped']}
                remaining = [pid for pid in doc['ids'] if pid not in done]
                doc['processed'] = len(done)
                item_spool.write_bytes(b''); template_spool.write_bytes(b'')
                with security_snapshots.preparation():
                    for start in range(0, len(remaining), READ_ITEMS):
                        if should_stop():
                            doc['state'] = 'interrupted'; break
                        chunk = remaining[start:start + READ_ITEMS]
                        require_ready(doc['campaign_id'], chunk)
                        rows = {int(row['pair_id']): row for row in self.exchange.load_rows(chunk)}
                        for pair_id in chunk:
                            if should_stop():
                                doc['state'] = 'interrupted'; break
                            if pair_id not in rows:
                                doc['failures'].append({'pair_id': pair_id, 'reason': 'selected item no longer exists'})
                                doc['processed'] += 1
                                continue
                            row = rows.pop(pair_id)
                            item = self.exchange.export_item(row) if hasattr(self.exchange, 'export_item') else {'item_id': str(row.get('pair_key') or f'pair:{pair_id}'), 'pair_id': pair_id,
                                    'source_sha256': exchange_mod.fingerprint(row), 'source': exchange_mod.source_of(row),
                                    'previous_draft': {k: row.get(k + '_llm') for k in ('root_cause', 'secure_pattern', 'explanation')}}
                            if item is None:
                                doc['skipped'].append({'pair_id': pair_id, 'reason': 'No longer waiting for this review queue after export was requested'})
                                doc['processed'] += 1
                                continue
                            template = self.exchange.response_item(item) if hasattr(self.exchange, 'response_item') else {'item_id': item['item_id'], 'source_sha256': item['source_sha256'], 'status': 'draft',
                                        'draft': {'root_cause': '', 'secure_pattern': '', 'explanation': '', 'behavioral_intent': '',
                                                  'evidence': [], 'missing_context': [], 'assumptions': []}}
                            raw, reply = encode(item), encode(template)
                            _, prefix, middle, suffix = self._envelope(doc, len(doc['files']))
                            overhead = len(prefix) + len(middle) + len(suffix)
                            if overhead + len(raw) + len(reply) > limit:
                                doc['failures'].append({'pair_id': pair_id, 'reason': f'complete item exceeds {limit} UTF-8 bytes including prompt and response template'})
                                doc['processed'] += 1
                                continue
                            comma = 1 if pending else 0
                            if pending and overhead + item_bytes + template_bytes + len(raw) + len(reply) + 2 * comma > limit:
                                publish(); comma = 0
                            with item_spool.open('ab') as stream:
                                stream.write((b',' if comma else b'') + raw)
                            with template_spool.open('ab') as stream:
                                stream.write((b',' if comma else b'') + reply)
                            item_bytes += len(raw) + comma; template_bytes += len(reply) + comma
                            pending.append({k: item[k] for k in ('pair_id', 'item_id', 'source_sha256')})
                            doc['processed'] += 1
                            if time.monotonic() - began >= doc.get('_next_update', 0):
                                update(); doc['_next_update'] = time.monotonic() - began + 1
                        if doc['state'] == 'interrupted':
                            break
                publish()
                if doc['state'] != 'interrupted':
                    doc['state'] = 'incomplete' if doc['failures'] else 'complete'
            except Exception as error:
                doc['state'] = 'incomplete'
                doc['failures'].append({'pair_id': None, 'reason': str(error)})
            finally:
                doc.pop('_next_update', None)
                update()
            return self.status(eid)

    def download(self, eid, index):
        doc = self._read(eid)
        if type(index) is not int or index < 0 or index >= len(doc['files']):
            raise ValueError('file is not published')
        return self.exchange._path(doc['files'][index]['packet_id'])

    def preview(self, raw, *, max_file_bytes=None):
        limit = file_bytes(MAX_BYTES if max_file_bytes is None else max_file_bytes)
        if len(raw) > limit:
            raise ValueError(f'Returned JSON exceeds {limit} UTF-8 bytes')
        try:
            response = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non-finite JSON number')))
        except (ValueError, UnicodeError) as error:
            raise ValueError('Invalid JSON: ' + str(error)) from error
        report = self.exchange.preview(response, max_bytes=limit)
        upload_id = hashlib.sha256(raw).hexdigest()[:32]
        path = self.exchange.root / 'json_uploads' / (upload_id + '.json')
        fsio.write_with(path, lambda part: part.write_bytes(raw))
        self.exchange._save(path.with_suffix('.limit.json'), {'max_file_bytes': limit})
        return {**self._compact(report), 'upload_id': upload_id}

    @staticmethod
    def _compact(report):
        return {**report, 'items': [{k: v for k, v in item.items() if k != 'draft'} for item in report['items']]}

    def apply(self, upload_id, selected):
        identity(upload_id)
        if not isinstance(selected, list) or any(not isinstance(i, str) for i in selected):
            raise ValueError('selected must be an array of item IDs')
        path = self.exchange.root / 'json_uploads' / (upload_id + '.json')
        receipt = path.with_suffix('.limit.json')
        limit = file_bytes(json.loads(receipt.read_bytes())['max_file_bytes'] if receipt.exists() else DEFAULT_BYTES)
        if path.stat().st_size > limit:
            raise ValueError('Returned JSON exceeds its recorded import limit')
        return self._compact(self.exchange.apply(json.loads(path.read_bytes()), selected=selected, max_bytes=limit))


def default():
    return JsonExports(exchange_mod.default())
