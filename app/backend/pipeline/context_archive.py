"""Resumable ZIP batches around the existing fingerprint-bound draft exchange."""
import contextlib
import hashlib
import io
import json
import pathlib
import re
import stat
import threading
import uuid
import zipfile

from app.backend.pipeline.context_exchange import canonical, EXPERT_PROMPT
from app.backend.pipeline.oslock import FileLock

MAX_BYTES = 32 * 1024 * 1024
_LOCK = threading.RLock()
PROMPT = EXPERT_PROMPT.replace(
    'Return one downloadable JSON file, without markdown fences, using response_template.',
    'For each packet use its response_template; preserve its packet ID and source fingerprints.') + '''

ZIP batch delivery instructions:
Open manifest.json and every JSON file in packets/. Analyze ALL items in this batch.
Replace the matching templates in responses/<packet_id>.json with your proposed results.
Return ONE downloadable ZIP, with manifest.json unchanged at its root and the completed
responses/ directory. You may include PROMPT.txt and packets/ only if unchanged.
Do not rename packet files or add extra files. Do not place everything inside an extra folder.
Do not omit difficult items: use needs_context with explicit missing facts when warranted.
Do not claim to have executed tests or verified a vulnerability unless supplied receipts prove it.
The app validates the returned ZIP and imports selected valid drafts before unlocking the next batch.
If you cannot finish, return the partial ZIP and clearly state that it is incomplete.
Never silently shorten source evidence or fabricate a successful result to complete the batch.
Use available file tools to create the downloadable ZIP. If those tools are unavailable,
say so explicitly instead of inventing a download link or claiming to have edited files.
'''


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON field: ' + key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


class BatchExchange:
    def __init__(self, exchange):
        self.exchange = exchange
        self.root = exchange.root / 'zip_batches'

    @contextlib.contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with _LOCK, FileLock(self.root / '.batches.lock', note=False):
            yield

    def _dir(self, sid):
        if not isinstance(sid, str) or not re.fullmatch('[0-9a-f]{32}', sid):
            raise ValueError('Invalid ZIP exchange ID')
        return self.root / sid

    def _load(self, sid):
        try:
            return json.loads((self._dir(sid) / 'session.json').read_text(encoding='utf-8'))
        except FileNotFoundError as error:
            raise ValueError('ZIP exchange not found') from error

    def _save(self, doc):
        self.exchange._save(self._dir(doc['session_id']) / 'session.json', doc)

    def _view(self, doc):
        index = doc['batch_index']
        start = index * doc['batch_size']
        batch = doc['batches'].get(str(index), {})
        count = min(doc['batch_size'], len(doc['ids']) - start)
        imported = len(batch.get('imported', []))
        return {'session_id': doc['session_id'], 'campaign_id': doc.get('campaign_id'),
                'batch_index': index, 'batch_count': (len(doc['ids']) + doc['batch_size'] - 1) // doc['batch_size'],
                'batch_size': doc['batch_size'], 'total': len(doc['ids']), 'item_count': count,
                'start': start + 1, 'end': start + count, 'imported': imported,
                'prepared': bool(batch.get('ready') and (self._dir(doc['session_id']) / str(index) / 'batch.zip').is_file()), 'packet_count': len(batch.get('packets', [])),
                'bytes': batch.get('bytes', 0), 'prompt': PROMPT, 'can_advance': imported == count,
                'review_status': 'model_draft'}

    def create(self, ids, *, batch_size=100, campaign_id=None):
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100000 or
                any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids)):
            raise ValueError('Select 1 to 100000 distinct positive integer pair IDs')
        if type(batch_size) is not int or not 1 <= batch_size <= 1000:
            raise ValueError('Batch size must be from 1 to 1000')
        if campaign_id is not None and (not isinstance(campaign_id, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,100}', campaign_id)):
            raise ValueError('Invalid campaign ID')
        doc = {'schema': 'context-zip-session/1', 'session_id': uuid.uuid4().hex,
               'ids': list(ids), 'campaign_id': campaign_id, 'batch_size': batch_size,
               'batch_index': 0, 'batches': {}}
        with self.locked():
            self._save(doc)
        return self._view(doc)

    def get(self, sid):
        with self.locked():
            return self._view(self._load(sid))

    def prepare(self, sid):
        with self.locked():
            doc = self._load(sid)
            key = str(doc['batch_index'])
            batch = doc['batches'].get(key)
            if batch and self._view(doc)['prepared']:
                return self._view(doc)
            if not batch:
                start = doc['batch_index'] * doc['batch_size']
                ids = doc['ids'][start:start + doc['batch_size']]
                if doc.get('campaign_id'):
                    from app.backend.pipeline.acceleration.context_gate import require_ready
                    require_ready(doc['campaign_id'], ids)
                packets = self.exchange.export(ids, campaign_id=doc.get('campaign_id'))
                batch = {'packets': [p['packet_id'] for p in packets], 'imported': []}
                doc['batches'][key] = batch
                self._save(doc)  # Retry a failed ZIP write with the same immutable packets.
            packets = [json.loads(self.exchange._path(pid).read_text(encoding='utf-8')) for pid in batch['packets']]
            manifest = {'schema': 'context-review-zip/1', 'session_id': sid, 'batch_index': doc['batch_index'],
                        'packets': [{'packet_id': p['packet_id'], 'items': len(p['items']),
                            'sha256': hashlib.sha256(canonical(p).encode()).hexdigest()} for p in packets]}
            folder = self._dir(sid) / key
            folder.mkdir(parents=True, exist_ok=True)
            entries = {'manifest.json': canonical(manifest), 'PROMPT.txt': PROMPT}
            for packet in packets:
                pid = packet['packet_id']
                entries[f'packets/{pid}.json'] = canonical(packet)
                entries[f'responses/{pid}.json'] = canonical(packet['response_template'])
            if sum(len(value.encode('utf-8')) for value in entries.values()) > MAX_BYTES:
                raise ValueError('ZIP batch exceeds 32 MiB of complete evidence; start a smaller batch without truncation')
            from app.backend.pipeline import fsio
            def write_zip(path):
                with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
                    for name, content in entries.items():
                        z.writestr(name, content)
            fsio.write_with(folder / 'batch.zip', write_zip)
            batch.update(ready=True, bytes=(folder / 'batch.zip').stat().st_size, manifest=manifest)
            self._save(doc)
            return self._view(doc)

    def archive(self, sid, *, batch_index=None):
        with self.locked():
            doc = self._load(sid)
            index = doc['batch_index'] if batch_index is None else batch_index
            if type(index) is not int or not 0 <= index <= doc['batch_index'] or not doc['batches'].get(str(index), {}).get('ready'):
                raise ValueError('Prepare this batch before downloading it')
            path = self._dir(sid) / str(index) / 'batch.zip'
            if not path.is_file():
                raise ValueError('Saved ZIP is missing. Prepare this batch again to rebuild it from its recorded packets.')
            return path

    def advance(self, sid):
        with self.locked():
            doc = self._load(sid)
            view = self._view(doc)
            if not view['can_advance']:
                raise ValueError('Import valid responses for every item in this batch before opening the next batch')
            if view['batch_index'] + 1 >= view['batch_count']:
                raise ValueError('All selected batches are complete')
            doc['batch_index'] += 1
            self._save(doc)
            return self._view(doc)

    def _read(self, doc, data):
        if len(data) > MAX_BYTES:
            raise ValueError('Returned ZIP exceeds 32 MiB')
        batch = doc['batches'].get(str(doc['batch_index']), {})
        if not batch.get('ready'):
            raise ValueError('Prepare the current batch first')
        original = self._dir(doc['session_id']) / str(doc['batch_index']) / 'batch.zip'
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z, zipfile.ZipFile(original) as exported:
                infos = z.infolist()
                if len(infos) > 4096 or sum(i.file_size for i in infos) > MAX_BYTES:
                    raise ValueError('Returned ZIP exceeds the expanded import budget')
                names = [i.filename for i in infos]
                if len(names) != len(set(n.lower() for n in names)):
                    raise ValueError('Duplicate ZIP members')
                allowed = set(exported.namelist())
                responses = []
                for info in infos:
                    name = info.filename
                    directory = info.is_dir() and name in ('packets/', 'responses/')
                    if (name not in allowed and not directory) or info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16):
                        raise ValueError('Unsupported or unsafe ZIP member: ' + name)
                    if directory:
                        continue
                    # Read no more than the bounded declared size; ZipFile also checks CRC.
                    content = z.read(info)
                    if name.startswith('responses/'):
                        value = _json(content)
                        if not isinstance(value, dict) or value.get('packet_id') != pathlib.PurePosixPath(name).stem:
                            raise ValueError('Response filename and packet ID differ')
                        responses.append(value)
                    elif name == 'manifest.json' and canonical(_json(content)) != canonical(batch['manifest']):
                        raise ValueError('Returned batch manifest differs from the recorded export')
                    elif name != 'manifest.json' and content != exported.read(name):
                        raise ValueError('Returned manifest or source evidence changed: ' + name)
                if 'manifest.json' not in names:
                    raise ValueError('Returned ZIP needs the unchanged manifest.json')
                if not responses:
                    raise ValueError('Returned ZIP has no response packets')
                return responses
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, UnicodeError) as error:
            raise ValueError('Invalid or truncated ZIP: ' + str(error)) from error

    def _report(self, responses, batch):
        reports = [self.exchange.preview(value) for value in responses]
        received = {p['packet_id'] for p in reports}
        missing = [item for report in reports for item in report['missing']]
        for pid in batch['packets']:
            if pid not in received:
                packet = json.loads(self.exchange._path(pid).read_text(encoding='utf-8'))
                missing.extend(i['item_id'] for i in packet['items'])
        return {'items': [item for report in reports for item in report['items']], 'missing': missing}

    def preview(self, sid, data):
        with self.locked():
            doc = self._load(sid)
            responses = self._read(doc, data)
            report = self._report(responses, doc['batches'][str(doc['batch_index'])])
            upload = hashlib.sha256(data).hexdigest()
            self.exchange._save(self._dir(sid) / str(doc['batch_index']) / 'uploads' / (upload + '.json'), responses)
            return {**report, 'upload_id': upload, 'session': self._view(doc)}

    def apply(self, sid, upload_id, selected):
        if not isinstance(upload_id, str) or not re.fullmatch('[0-9a-f]{64}', upload_id):
            raise ValueError('Invalid ZIP upload ID')
        if not isinstance(selected, list) or any(not isinstance(i, str) for i in selected):
            raise ValueError('Select returned item IDs to import')
        with self.locked():
            doc = self._load(sid)
            batch = doc['batches'].get(str(doc['batch_index']), {})
            path = self._dir(sid) / str(doc['batch_index']) / 'uploads' / (upload_id + '.json')
            responses = json.loads(path.read_text(encoding='utf-8'))
            imported = unchanged = 0
            accepted = set(batch.get('imported', []))
            for response in responses:
                result = self.exchange.apply(response, selected=selected)
                imported += result['imported']; unchanged += result['unchanged']
                accepted.update(item['item_id'] for item in result['items']
                    if item['status'] == 'valid' and item['item_id'] in selected)
                batch['imported'] = sorted(accepted)
                self._save(doc)  # Survive interruption between packet imports.
            return {'upload_id': upload_id, 'imported': imported, 'unchanged': unchanged, 'session': self._view(doc),
                    **self._report(responses, batch), 'review_status': 'model_draft'}
