"""Content-addressed JSON cache. Corruption is a miss; never deserialize pickle."""
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from .metrics import current


class ArtifactCache:
    def __init__(self, root):
        self.root = Path(root)
        self.locks = [threading.RLock() for _ in range(64)]
        self._hits = self._misses = 0

    def path(self, namespace, identity):
        digest = hashlib.sha256(json.dumps([namespace, identity], sort_keys=True, ensure_ascii=True,
                                         separators=(',', ':')).encode()).hexdigest()
        return self.root / digest[:2] / (digest + '.json')

    def get_or_compute(self, namespace, identity, calculate):
        path = self.path(namespace, identity)
        with self.locks[int(path.stem[:8], 16) % len(self.locks)]:
            try:
                envelope = json.loads(path.read_text(encoding='utf-8'))
                value = envelope['value']
                payload = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
                if envelope['sha256'] != hashlib.sha256(payload).hexdigest():
                    raise ValueError('cache checksum mismatch')
                self._hits += 1
                current.count(namespace + '_cache_hits')
                return value
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self._misses += 1
            current.count(namespace + '_cache_misses')
            value = calculate()
            payload = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
            document = json.dumps({'sha256': hashlib.sha256(payload).hexdigest(), 'value': value})
            from .disk_budget import check as disk_check
            disk_check(len(document.encode('utf-8')))
            temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(document, encoding='utf-8')
                os.replace(temporary, path)
            except OSError:
                current.count('cache_write_failures')
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    current.count('cache_cleanup_failures')
            return value

    def stats(self):
        return {'hits': self._hits, 'misses': self._misses}


_default = None


def default():
    global _default
    if _default is None:
        from app.backend import paths
        root = os.environ.get('ZTSA_ACCEL_CACHE') or paths.interim_dir() / 'acceleration' / 'artifacts-v1'
        _default = ArtifactCache(root)
    return _default
