"""Disposable durable stage references. Published evidence remains in the application SQLite database."""
from contextlib import contextmanager
import hashlib
import json
from itertools import islice
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid
from .repository_service import ByteCache

_parsed_cache=ByteCache(64<<20)

STAGES = ("acquire", "parse", "context", "publish")
_BOOKKEEPING = frozenset("id status error attempts created updated parent_sha commit_message commit_date files_changed pairs mined_at langs detected_langs fix_check fix_check_reason link fix_check_package link_evidence evidence_level commit_kind parent_selection_reason parent_count campaign_id".split())


def semantic_input(row):
    return {key:value for key,value in row.items() if key not in _BOOKKEEPING}


class StaleAttempt(RuntimeError):
    pass


class DiskPressure(RuntimeError):
    pass


class CheckpointCorrupt(ValueError):
    """Observed content failed validation; path and access errors are separate."""
    def __init__(self, message, digest, raw):
        super().__init__(message)
        self.digest,self.raw=digest,raw


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


class Spool:
    def __init__(self, root, *, cache_bytes=20_000_000_000, reserve_bytes=15_000_000_000, scope="default"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_bytes, self.reserve_bytes = int(cache_bytes), int(reserve_bytes)
        explicit = os.environ.get('ZTSA_CAMPAIGN_SPOOL_GB', '').strip()         # a person's number, for a drive with room: once the spool is full every write scans and evicts, which slowed a kernel campaign by half
        if explicit:
            try:
                self.cache_bytes = max(1_000_000_000, int(float(explicit) * 1_000_000_000))
            except ValueError:
                pass
        self.scope = str(scope)
        self.lock = threading.RLock()
        from app.backend.pipeline.oslock import FileLock
        # WAL negotiation on a brand-new database can raise SQLITE_BUSY before
        # SQLite's normal writer timeout applies. Serialize only initialization;
        # ordinary operations retain their independent connections/transactions.
        self.db=None
        initializing=FileLock(self.root/'queue-init.lock',note=False).acquire(wait=30)
        try:
            self._open()
        except BaseException:
            try:
                if self.db is not None:self.db.close()
            finally:initializing.release()
            raise
        else:
            try:initializing.release()
            except BaseException:
                if self.db is not None:self.db.close()
                raise

    def _open(self):
        self.db = sqlite3.connect(self.root / "queue.sqlite", check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS control(id TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, scope TEXT NOT NULL, input TEXT NOT NULL, next INTEGER NOT NULL DEFAULT 0,
            spent REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued', error TEXT);
        CREATE TABLE IF NOT EXISTS outputs(task TEXT, stage INTEGER, hash TEXT NOT NULL, size INTEGER NOT NULL,
            PRIMARY KEY(task,stage), FOREIGN KEY(task) REFERENCES tasks(id));
        CREATE TABLE IF NOT EXISTS artifacts(hash TEXT PRIMARY KEY, size INTEGER NOT NULL, accessed REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS receipts(task TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS descriptor_admissions(scope TEXT NOT NULL,epoch INTEGER NOT NULL,commit_key TEXT NOT NULL,
            PRIMARY KEY(scope,epoch,commit_key)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS quarantine(hash TEXT PRIMARY KEY, expected TEXT NOT NULL,
            size INTEGER NOT NULL, created REAL NOT NULL);
        """)
        self._prepare_accounting()
        self.db.execute("INSERT OR IGNORE INTO control VALUES(?,0)",(self.scope,))
        self.db.commit()
        self._recover_artifacts()

    def _prepare_accounting(self):
        # One serialized startup reconciliation, not a SUM for every checkpoint.
        # The per-hash ledger also handles REPLACE correctly whether a connection
        # enables recursive DELETE triggers or not. All mutations and rollback
        # update accounting in the same SQLite transaction as the artifact row.
        try:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE INDEX IF NOT EXISTS mining_tasks_commit ON tasks(scope,json_extract(input,'$.commit_key'));
                CREATE INDEX IF NOT EXISTS mining_tasks_status ON tasks(scope,status,id);
                CREATE INDEX IF NOT EXISTS mining_tasks_stages ON tasks(scope,next,status);
                CREATE INDEX IF NOT EXISTS mining_tasks_pending ON tasks(scope,id) WHERE status NOT IN ('completed','retired');
                CREATE INDEX IF NOT EXISTS mining_outputs_hash ON outputs(hash,task);
                CREATE INDEX IF NOT EXISTS mining_artifacts_accessed ON artifacts(accessed,hash,size);
                CREATE TABLE IF NOT EXISTS artifact_sizes(hash TEXT PRIMARY KEY,size INTEGER NOT NULL CHECK(size>=0));
                CREATE TABLE IF NOT EXISTS artifact_bytes(id INTEGER PRIMARY KEY CHECK(id=1),total INTEGER NOT NULL CHECK(total>=0));
                DELETE FROM artifact_sizes;
                INSERT INTO artifact_sizes SELECT hash,size FROM artifacts;
                INSERT OR REPLACE INTO artifact_bytes VALUES(1,(SELECT COALESCE(SUM(size),0) FROM artifacts));
                CREATE TRIGGER IF NOT EXISTS mining_artifact_insert AFTER INSERT ON artifacts BEGIN
                    UPDATE artifact_bytes SET total=total+NEW.size-COALESCE((SELECT size FROM artifact_sizes WHERE hash=NEW.hash),0) WHERE id=1;
                    INSERT OR REPLACE INTO artifact_sizes VALUES(NEW.hash,NEW.size);
                END;
                CREATE TRIGGER IF NOT EXISTS mining_artifact_delete AFTER DELETE ON artifacts BEGIN
                    UPDATE artifact_bytes SET total=total-COALESCE((SELECT size FROM artifact_sizes WHERE hash=OLD.hash),0) WHERE id=1;
                    DELETE FROM artifact_sizes WHERE hash=OLD.hash;
                END;
                CREATE TRIGGER IF NOT EXISTS mining_artifact_update AFTER UPDATE OF hash,size ON artifacts BEGIN
                    UPDATE artifact_bytes SET total=total+NEW.size
                        -COALESCE((SELECT size FROM artifact_sizes WHERE hash=OLD.hash),0)
                        -CASE WHEN OLD.hash!=NEW.hash THEN COALESCE((SELECT size FROM artifact_sizes WHERE hash=NEW.hash),0) ELSE 0 END WHERE id=1;
                    DELETE FROM artifact_sizes WHERE hash=OLD.hash;
                    INSERT OR REPLACE INTO artifact_sizes VALUES(NEW.hash,NEW.size);
                END;
                COMMIT;
            """)
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    def _artifact_bytes(self):
        row=self.db.execute('SELECT total FROM artifact_bytes WHERE id=1').fetchone()
        if row is None:raise RuntimeError('Artifact byte accounting is unavailable')
        return row[0]

    def _recover_artifacts(self):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            folder=self.root/"artifacts"
            if not folder.exists():return
            for prefix in folder.iterdir():
                if prefix.is_symlink() or not prefix.is_dir():continue
                for path in prefix.glob("*.json"):
                    digest=path.stem
                    if len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest):continue
                    if prefix.name!=digest[:2] or path.is_symlink():continue
                    stat=path.stat()
                    self.db.execute("INSERT OR IGNORE INTO artifacts VALUES(?,?,?)",(digest,stat.st_size,stat.st_mtime))

    def close(self):
        with self.lock:
            self.db.close()

    def begin(self):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("UPDATE control SET epoch=epoch+1 WHERE id=?",(self.scope,))
            self.db.execute("UPDATE tasks SET status='queued',error=NULL WHERE status='running' AND scope=?",(self.scope,))
            return self.db.execute("SELECT epoch FROM control WHERE id=?",(self.scope,)).fetchone()[0]

    def check(self, epoch):
        with self.lock:
            if self.db.execute("SELECT epoch FROM control WHERE id=?",(self.scope,)).fetchone()[0] != epoch:
                raise StaleAttempt("Mining attempt has been superseded")

    @staticmethod
    def _descriptor_keys(keys):
        if (not isinstance(keys,(list,tuple)) or len(keys)>256
            or any(not isinstance(key,str) or not key or len(key.encode('utf-8'))>4096 for key in keys)):
            raise ValueError('Refill identities are limited to 256 bounded commit keys')
        if len(set(keys))!=len(keys):raise ValueError('Refill contains duplicate descriptors')
        return json.dumps(keys,separators=(',',':'))

    def seen_descriptors(self,keys,epoch):
        payload=self._descriptor_keys(keys)
        with self.lock:
            self.check(epoch)
            found=self.db.execute('SELECT 1 FROM descriptor_admissions WHERE scope=? AND epoch=? '
                'AND commit_key IN (SELECT value FROM json_each(?)) LIMIT 1',(self.scope,epoch,payload)).fetchone()
            self.check(epoch)
            return found is not None

    def claim_descriptors(self,keys,epoch):
        payload=self._descriptor_keys(keys)
        from . import disk_budget
        disk_budget.check((128<<10)+len(payload.encode('utf-8'))*4)
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._claim_descriptors(keys,epoch)
            self.check(epoch)

    def _claim_descriptors(self,keys,epoch):
        self.check(epoch)
        if self.seen_descriptors(keys,epoch):raise ValueError('Refill repeated an already admitted descriptor')
        self.db.execute('DELETE FROM descriptor_admissions WHERE scope=? AND epoch<?',(self.scope,epoch))
        self.db.executemany('INSERT INTO descriptor_admissions VALUES(?,?,?)',
            ((self.scope,epoch,key) for key in keys))

    def _enqueue_prepared(self,key,payload,commit_key):
        self.db.execute("UPDATE tasks SET status='retired' WHERE scope=? AND id!=? AND json_extract(input,'$.commit_key')=? AND status!='completed'",(self.scope,key,commit_key))
        self.db.execute("INSERT OR IGNORE INTO tasks(id,scope,input) VALUES(?,?,?)",(key,self.scope,payload))
        self.db.execute("UPDATE tasks SET input=? WHERE id=?",(payload,key))
        self.db.execute("UPDATE tasks SET status='queued',spent=0,error=NULL WHERE id=? AND status='failed'",(key,))

    def enqueue_descriptors(self,rows,implementation,epoch):
        """Claim identities and queue their inputs in one fenced transaction."""
        if not isinstance(rows,(list,tuple)) or len(rows)>256:
            raise ValueError('Refill is limited to 256 descriptors')
        identities=[row['commit_key'] for row in rows]
        self._descriptor_keys(identities)
        # Allocate before taking the writer lock. Allocation/serialization
        # failure cannot leave claimed identities without their queued tasks.
        prepared=[(hashlib.sha256(encoded([self.scope,implementation,semantic_input(row)])).hexdigest(),
                   encoded(row).decode(),row['commit_key']) for row in rows]
        keys=[record[0] for record in prepared]
        from . import disk_budget
        disk_budget.check((128<<10)+sum(len(record[1].encode('utf-8')) for record in prepared)*4)
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self._claim_descriptors(identities,epoch)
            for record in prepared:self._enqueue_prepared(*record)
            self.check(epoch)
        return keys

    def enqueue(self, row, implementation, *, epoch=None):
        payload = encoded(row).decode()
        key = hashlib.sha256(encoded([self.scope, implementation, semantic_input(row)])).hexdigest()
        with self.lock, self.db:
            if epoch is not None:
                self.db.execute('BEGIN IMMEDIATE')
                self.check(epoch)
            self._enqueue_prepared(key,payload,row.get('commit_key'))
            if epoch is not None:self.check(epoch)
        return key

    def input(self, key):
        with self.lock:
            return json.loads(self.db.execute("SELECT input FROM tasks WHERE id=?", (key,)).fetchone()[0])

    def stage(self, key):
        with self.lock:
            row = self.db.execute("SELECT next FROM tasks WHERE id=?", (key,)).fetchone()
            if row is None:raise KeyError("Unknown mining task: "+str(key))
            return STAGES[row[0]] if row[0] < len(STAGES) else None

    def stages(self, keys):
        """One consistent, bounded stage snapshot for a scheduling pass."""
        keys=tuple(islice(keys,257))
        if len(keys)>256:raise ValueError("Stage snapshot lookahead is limited to 256")
        if not keys:return {}
        with self.lock:
            rows=self.db.execute("SELECT id,next FROM tasks WHERE id IN ("+','.join('?' for _ in keys)+")",keys).fetchall()
        found={row['id']:row['next'] for row in rows}
        for key in keys:
            if key not in found:raise KeyError("Unknown mining task: "+str(key))
        return {key:STAGES[found[key]] if found[key]<len(STAGES) else None for key in keys}

    def spent(self, key):
        with self.lock:
            return self.db.execute("SELECT spent FROM tasks WHERE id=?", (key,)).fetchone()[0]

    def publication_id(self, key):
        return hashlib.sha256(("mining-publication/3:" + key).encode()).hexdigest()

    def _path(self, digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid artifact fingerprint")
        path=self.root / "artifacts" / digest[:2] / (digest + ".json")
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):raise ValueError("Artifact path escaped cache root")
        return path

    def artifact_path(self, key, stage):
        with self.lock:
            row = self.db.execute("SELECT hash FROM outputs WHERE task=? AND stage=?", (key, STAGES.index(stage))).fetchone()
            return self._path(row[0]) if row else None

    def output(self, key, stage, *, verify_disk=False):
        with self.lock:
            if stage=='publish':
                receipt=self.db.execute('SELECT data FROM receipts WHERE task=?',(key,)).fetchone()
                if receipt:return json.loads(receipt[0])
            path = self.artifact_path(key, stage)
            if path is None:
                return None
            stat=path.stat()
            identity=(str(path),stat.st_mtime_ns,stat.st_size)
            raw=None if verify_disk or stage not in ('parse','context') else _parsed_cache.get(identity)
            if raw is None:
                raw=path.read_bytes()
                if hashlib.sha256(raw).hexdigest()!=path.stem:raise CheckpointCorrupt("Checkpoint checksum mismatch",path.stem,raw)
                if stage in ('parse','context'):_parsed_cache.put(identity,raw,len(raw))
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, UnicodeError) as error:
                raise CheckpointCorrupt("Checkpoint JSON is invalid",path.stem,raw) from error

    def _write_artifact(self, path, raw, epoch):
        self._space(len(raw))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            from app.backend.pipeline import fsio
            from . import operations
            def replace_artifact():
                operation=operations.current()
                if operation is not None:operation.check()
                self.check(epoch)
                if self._path(path.stem)!=path:
                    raise ValueError("Checkpoint artifact path changed")
                os.replace(temporary,path)
            fsio._retry(replace_artifact)
        finally:
            temporary.unlink(missing_ok=True)

    def _preserve_corrupt(self, expected, raw, epoch):
        # Preserve exact observed bytes at their actual content address. The
        # quarantine pin is bounded and shares normal cache/disk accounting.
        digest=hashlib.sha256(raw).hexdigest()
        found=self.db.execute('SELECT 1 FROM quarantine WHERE hash=?',(digest,)).fetchone()
        if not found:
            count,size=self.db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM quarantine').fetchone()
            if count>=8 or size+len(raw)>64<<20:
                raise DiskPressure('Corrupt checkpoint quarantine is full; references retained')
        path=self._path(digest)
        try:
            existing=path.read_bytes()
        except FileNotFoundError:
            existing=None
        if existing is None:
            self._write_artifact(path,raw,epoch)
        elif existing!=raw:
            raise ValueError('Quarantine destination failed integrity verification')
        self.db.execute('INSERT OR REPLACE INTO artifacts VALUES(?,?,?)',(digest,len(raw),time.time()))
        self.db.execute('INSERT OR IGNORE INTO quarantine VALUES(?,?,?,?)',(digest,expected,len(raw),time.time()))

    def _quarantine_observation(self, error, epoch):
        # The writer lock and epoch were established by the caller. Preserve
        # the observation first, then revalidate the current canonical file.
        path=self._path(error.digest)
        try:
            current=path.read_bytes()
        except FileNotFoundError:
            current=None
        if current is not None:
            self.db.execute('UPDATE artifacts SET size=? WHERE hash=?',(len(current),error.digest))
        self._preserve_corrupt(error.digest,error.raw,epoch)
        if current is None:return True
        if hashlib.sha256(current).hexdigest()==error.digest:
            try:json.loads(current)
            except (json.JSONDecodeError,UnicodeError):pass
            else:return False
        if current!=error.raw:self._preserve_corrupt(error.digest,current,epoch)
        return True

    def _recovery_epoch(self, epoch):
        with self.lock:
            if epoch is None:
                epoch=self.db.execute("SELECT epoch FROM control WHERE id=?",(self.scope,)).fetchone()[0]
            self.check(epoch)
            return epoch

    def _recovery_state(self, key):
        row=self.db.execute("SELECT next,spent,status,error FROM tasks WHERE id=? AND scope=?",(key,self.scope)).fetchone()
        if row is None:raise KeyError("Unknown mining task: "+str(key))
        outputs=self.db.execute("SELECT stage,hash,size FROM outputs WHERE task=? ORDER BY stage",(key,)).fetchall()
        return tuple(row),tuple(tuple(output) for output in outputs)

    def reconcile(self, key, epoch=None):
        epoch=self._recovery_epoch(epoch)
        while True:
            with self.lock:
                self.check(epoch)
                if self.db.execute('SELECT 1 FROM receipts WHERE task=?',(key,)).fetchone():return None
                observed=self._recovery_state(key)
            # File validation must not occupy SQLite's writer slot. Only an
            # unchanged observation may later authorize a destructive repair.
            invalid=corruption=None
            for i in range(observed[0][0]):
                try:
                    if self.output(key, STAGES[i],verify_disk=True) is None:
                        invalid=i
                        break
                except CheckpointCorrupt as error:
                    invalid,corruption=i,error
                    break
                except FileNotFoundError:
                    invalid=i
                    break
            with self.lock,self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.check(epoch)
                if self.db.execute('SELECT 1 FROM receipts WHERE task=?',(key,)).fetchone():return None
                current=self._recovery_state(key)
                if current!=observed:
                    continue
                if invalid is not None:
                    if corruption is not None and not self._quarantine_observation(corruption,epoch):
                        continue
                    self.db.execute("DELETE FROM outputs WHERE task=? AND stage>=?", (key, invalid))
                    self.db.execute("UPDATE tasks SET next=?,status='queued',error=NULL WHERE id=? AND scope=?", (invalid,key,self.scope))
                    return STAGES[invalid]
                next_stage=current[0][0]
                return STAGES[next_stage] if next_stage<len(STAGES) else None

    def reclaim_disposable(self, needed):
        freed=0
        with self.lock:
            own=not self.db.in_transaction
            if own:self.db.execute('BEGIN IMMEDIATE')
            try:
                rows=self.db.execute("""SELECT a.hash,a.size FROM artifacts a WHERE NOT EXISTS
                    (SELECT 1 FROM outputs o JOIN tasks t ON t.id=o.task WHERE o.hash=a.hash AND t.status NOT IN ('completed','retired'))
                    AND NOT EXISTS (SELECT 1 FROM quarantine q WHERE q.hash=a.hash)
                    ORDER BY a.accessed""").fetchall()
                for row in rows:
                    self._path(row['hash']).unlink(missing_ok=True)
                    self.db.execute('DELETE FROM artifacts WHERE hash=?',(row['hash'],))
                    freed+=row['size']
                    if freed>=needed:break
                if own:self.db.commit()
            except BaseException:
                if own:self.db.rollback()
                raise
        return freed

    def _space(self, needed):
        from .disk_budget import check as disk_check
        disk_check(needed)
        total=self._artifact_bytes()
        if total+needed>self.cache_bytes:
            total-=self.reclaim_disposable(total+needed-self.cache_bytes)
        if total+needed>self.cache_bytes or shutil.disk_usage(self.root).free-needed<self.reserve_bytes:
            raise DiskPressure('SSD cache or disk reserve exhausted; active checkpoints retained')

    def acknowledge(self,key,receipt,epoch=None):
        if not receipt.get('receipt') or receipt.get('publication_id')!=self.publication_id(key):
            raise ValueError('Authoritative publication receipt required')
        with self.lock,self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if epoch is not None:self.check(epoch)
            self.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?)',(key,encoded(receipt).decode()))
            self.db.execute("UPDATE tasks SET next=?,status='completed',error=NULL WHERE id=?",(len(STAGES),key))
        return receipt

    def reconcile_receipts(self,lookup,epoch=None):
        # Iterate only this scope's references. Lookup remains outside both the
        # instance lock and the writer transaction; acknowledgement is fenced.
        epoch=self._recovery_epoch(epoch)
        last=''
        while True:
            with self.lock:
                self.check(epoch)
                rows=self.db.execute("SELECT id FROM tasks WHERE scope=? AND id>? AND status NOT IN ('completed','retired') ORDER BY id LIMIT 128",(self.scope,last)).fetchall()
            if not rows:return
            for row in rows:
                key=row['id'];last=key
                self.check(epoch)
                receipt=lookup(key,self.publication_id(key))
                if receipt:self.acknowledge(key,receipt,epoch)
                else:self.check(epoch)

    def checkpoint(self, key, stage, value, epoch, *, seconds=0):
        index = STAGES.index(stage)
        if stage == 'publish':
            if self.stage(key)!='publish':raise ValueError('Stage checkpoint out of order')
            self.acknowledge(key,value,epoch)
            return hashlib.sha256(encoded(value)).hexdigest()
        raw = encoded(value)
        digest = hashlib.sha256(raw).hexdigest()
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.check(epoch)
            if self.stage(key) != stage:
                raise ValueError("Stage checkpoint out of order")
            # Serialize missing-path resolution with shard creation, including
            # other Spool connections through the SQLite writer transaction.
            path = self._path(digest)
            try:
                existing=path.read_bytes()
            except FileNotFoundError:
                existing=None
            if existing is not None and existing!=raw:
                self._quarantine_observation(CheckpointCorrupt("Checkpoint checksum mismatch",digest,existing),epoch)
                # Quarantine writes may trigger bounded disposable reclamation;
                # always reconstruct the selected exact bytes atomically.
                existing=None
            if existing is None:self._write_artifact(path,raw,epoch)
            self.db.execute("INSERT OR REPLACE INTO artifacts VALUES(?,?,?)", (digest,len(raw),time.time()))
            self.db.execute("INSERT OR REPLACE INTO outputs VALUES(?,?,?,?)", (key,index,digest,len(raw)))
            from . import operations
            operations.check()
            self.db.execute("UPDATE tasks SET next=?,spent=spent+?,status=?,error=NULL WHERE id=?",
                (index+1, max(0,seconds), "completed" if index+1==len(STAGES) else "queued",key))
        return digest

    def running(self, key, epoch):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.check(epoch)
            self.db.execute("UPDATE tasks SET status='running' WHERE id=?", (key,))

    def defer(self, key, epoch, *, seconds=0):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.check(epoch)
            self.db.execute("UPDATE tasks SET status='queued',spent=spent+? WHERE id=?", (max(0,seconds),key))

    def fail(self, key, error, epoch, *, seconds=0):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.check(epoch)
            self.db.execute("UPDATE tasks SET status='failed',error=?,spent=spent+? WHERE id=?", (__import__('app.backend.pipeline.redact',fromlist=['text']).text(error)[:1000],max(0,seconds),key))

    def describe(self):
        with self.lock:
            stages={name:{"queued":0,"running":0,"failed":0} for name in STAGES}
            completed=0
            for row in self.db.execute("SELECT next,status,COUNT(*) AS n FROM tasks WHERE scope=? AND status!='retired' GROUP BY next,status",(self.scope,)):
                if row["next"]==len(STAGES):completed+=row["n"]
                else:stages[STAGES[row["next"]]][row["status"]]+=row["n"]
            size=self._artifact_bytes()
            return {"engine_version":3,"stages":stages,"completed":completed,"cache_bytes":size,"cache_limit_bytes":self.cache_bytes}
