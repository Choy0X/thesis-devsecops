"""Embedded durable document storage backed by indexed SQLite JSONB.

Connections belong to a process/thread. SQL performs selection, ordering and
pagination; only matching documents cross into Python. WAL uses FULL durability.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from types import SimpleNamespace
import uuid
import weakref

from . import codec
from .operations import DuplicateKeyError, BulkWriteError, OperationFailure
from .query import Query, values, value_key, get, put, remove, MISSING, field, literal, single_equality, membership_anchor, path_parts
from .types import ObjectId

_session=ContextVar('embedded_session',default=None)
_instances=weakref.WeakSet()


def table_name(name,prefix='d_'):
    return prefix+hashlib.sha256(name.encode('utf-8')).hexdigest()[:24]


def _regex(pattern,value,options=''):
    if not isinstance(value,str):return 0
    flags=0
    for key,flag in [('i',re.I),('m',re.M),('s',re.S),('x',re.X)]:
        if key in str(options):flags|=flag
    import regex
    try:return int(regex.search(pattern,value,flags,timeout=.2) is not None)
    except TimeoutError:raise OperationFailure('Search regular expression exceeded its deadline')


def _contains_regex(value):
    # Native patterns round-trip as Regex and historically need a lookup rewrite.
    if type(value).__name__=='Regex' or isinstance(value,re.Pattern):return True
    if isinstance(value,dict):return any(_contains_regex(item) for item in value.values())
    if isinstance(value,(list,tuple)):return any(_contains_regex(item) for item in value)
    return False


def _index_values(document,path):
    """Stream the same multikey values and ordinals without a path-sized list."""
    parts=path_parts(path)
    def walk(value,at,ordinal):
        if at==len(parts):
            yield value,ordinal
            if isinstance(value,list):
                for i,item in enumerate(value):yield item,ordinal+'/'+str(i)
            return
        key=parts[at]
        if isinstance(value,list):
            if key.isdigit():
                i=int(key)
                if i<len(value):yield from walk(value[i],at+1,ordinal+'/'+str(i))
            else:
                for i,item in enumerate(value):yield from walk(item,at,ordinal+'/'+str(i))
        elif isinstance(value,dict) and key in value:yield from walk(value[key],at+1,ordinal+'/'+key)
    return walk(document,0,'')


class Database:
    def __init__(self,path,*,name='local'):
        self.path=Path(path).resolve();self.name=name
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.connections={};self._collections={}
        self._client=Client(self)
        _instances.add(self)
        conn=self._connection()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS _collections(name TEXT PRIMARY KEY, validator TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS _paths(collection TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(collection,path));
            CREATE TABLE IF NOT EXISTS _indexes(collection TEXT NOT NULL,name TEXT NOT NULL,spec TEXT NOT NULL,PRIMARY KEY(collection,name));
            CREATE TABLE IF NOT EXISTS _search_indexes(collection TEXT NOT NULL,name TEXT NOT NULL,spec TEXT NOT NULL,PRIMARY KEY(collection,name));
            CREATE TABLE IF NOT EXISTS _metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS _fts_paths(collection TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(collection,path));
        """)
        if 'options' not in {row[1] for row in conn.execute('PRAGMA table_info(_collections)')}:
            with self.transaction():
                if 'options' not in {row[1] for row in conn.execute('PRAGMA table_info(_collections)')}:
                    conn.execute("ALTER TABLE _collections ADD COLUMN options TEXT NOT NULL DEFAULT '{}'")
        self._upgrade_lookup_encoding()
    def _upgrade_lookup_encoding(self):
        conn=self._connection()
        if conn.execute("SELECT value FROM _metadata WHERE key='lookup_encoding'").fetchone()==('2',):return
        with self.transaction():
            for name in self.list_collection_names():
                collection=self[name]
                conn.execute('DELETE FROM '+collection._values)
                cursor=conn.execute('SELECT id_key,json(payload),types FROM '+collection._table+' ORDER BY rowid')
                try:
                    while batch:=cursor.fetchmany(100):
                        for oldkey,payload,types in batch:
                            document=codec.decode_document(payload,types);key=codec.id_key(document['_id'])
                            if oldkey!=key:conn.execute('UPDATE '+collection._table+' SET id_key=? WHERE id_key=?',(key,oldkey))
                            for path in collection._paths:collection._index_path(conn,key,document,path)
                finally:cursor.close()
                conn.execute('DROP TABLE IF EXISTS '+collection._fts)
                conn.execute('DROP TABLE IF EXISTS '+collection._fts_map)
            conn.execute('DELETE FROM _fts_paths')
            conn.execute("INSERT OR REPLACE INTO _metadata(key,value) VALUES ('lookup_encoding','2')")
    def _connection(self):
        session=_session.get()
        if session is not None:return session.bind(self)
        return self._thread_connection()
    def _thread_connection(self):
        key=(os.getpid(),threading.get_ident())
        conn=self.connections.get(key)
        if conn is None:
            # Never hold the handle-map lock while SQLite waits for another writer.
            conn=sqlite3.connect(self.path,timeout=30,isolation_level=None,check_same_thread=False,cached_statements=256)
            conn.execute('PRAGMA busy_timeout=30000')
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA foreign_keys=ON')
            from app.backend.pipeline.acceleration.memory_profile import current as memory_profile
            conn.execute(f'PRAGMA cache_size=-{memory_profile().sqlite_cache_kib}')
            conn.execute('PRAGMA temp_store=FILE')
            conn.execute('PRAGMA mmap_size=0')
            conn.create_function('rx',3,_regex,deterministic=True)
            with self.lock:self.connections[key]=conn
        return conn
    @property
    def client(self):return self._client
    def _invalidate_write_metadata(self,name):
        session=_session.get()
        if session is not None and session.database is not None and session.database.path==self.path:
            session.write_metadata.pop(name,None)
    def __getitem__(self,name):
        if not isinstance(name,str) or not name or '\x00' in name:raise ValueError('Invalid collection name')
        found=self._collections.get(name)
        if found is not None:return found
        created=Collection(self,name)
        with self.lock:return self._collections.setdefault(name,created)
    def __getattr__(self,name):
        if name.startswith('_'):raise AttributeError(name)
        return self[name]
    def get_collection(self,name,**kwargs):return self[name]
    def list_collection_names(self,*args,**kwargs):
        return [r[0] for r in self._connection().execute('SELECT name FROM _collections ORDER BY name')]
    def list_collections(self,**kwargs):
        from .collection_options import unpack_options
        rows=[]
        for name,validator,options in self._connection().execute('SELECT name,validator,options FROM _collections ORDER BY name'):
            rows.append({'name':name,'options':unpack_options(options,validator)})
        return iter(rows)
    def create_collection(self,name,**kwargs):
        from .collection_options import check_options,pack_options
        check_options(kwargs)
        with self.transaction():
            if name in self.list_collection_names():raise OperationFailure('Collection exists',48)
            coll=self[name]
            self._connection().execute('UPDATE _collections SET validator=?,options=? WHERE name=?',
                (*pack_options(kwargs),name))
            self._invalidate_write_metadata(name)
        return coll
    def drop_collection(self,name):
        if isinstance(name,Collection):name=name.name
        with self.transaction():
            for prefix in ('d_','v_','f_','m_'):
                self._connection().execute('DROP TABLE IF EXISTS '+table_name(name,prefix))
            for table in ('_collections','_paths','_indexes','_search_indexes','_fts_paths'):
                column='name' if table=='_collections' else 'collection'
                self._connection().execute('DELETE FROM '+table+' WHERE '+column+'=?',(name,))
            self._invalidate_write_metadata(name)
        with self.lock:self._collections.pop(name,None)
    @contextmanager
    def transaction(self,write=True):
        existing=_session.get()
        if existing is not None:
            existing.bind(self)
            yield existing
            return
        with Session(self) as session:
            with session.start_transaction(write=write):yield session
    def command(self,command,*args,**kwargs):
        if isinstance(command,dict):command, value=next(iter(command.items()));args=(value,*args)
        if command in ('ping','hello','buildInfo'):
            return {'ok':1,'version':sqlite3.sqlite_version,'setName':'embedded'}
        if command=='collMod':
            from .collection_options import check_options,pack_options,unpack_options
            check_options(kwargs)
            with self.transaction():
                row=self._connection().execute('SELECT validator,options FROM _collections WHERE name=?',(args[0],)).fetchone()
                if row is None:raise OperationFailure('Collection does not exist',26)
                options=unpack_options(row[1],row[0]);options.update(kwargs);check_options(options)
                self._connection().execute('UPDATE _collections SET validator=?,options=? WHERE name=?',
                    (*pack_options(options),args[0]))
                self._invalidate_write_metadata(args[0])
            return {'ok':1}
        if command=='collStats':
            c=self[args[0]];conn=self._connection()
            n,size=conn.execute('SELECT count(*),coalesce(sum(length(payload)+length(types)),0) FROM '+c._table).fetchone()
            return {'ok':1,'count':n,'size':size,'storageSize':size,'totalIndexSize':0,'nindexes':len(list(c.list_indexes()))}
        if command in ('dbStats','dbstats'):
            page=self._connection().execute('PRAGMA page_count').fetchone()[0];size=self._connection().execute('PRAGMA page_size').fetchone()[0]
            return {'ok':1,'dataSize':page*size,'storageSize':self.path.stat().st_size,'collections':len(self.list_collection_names())}
        raise NotImplementedError('Unsupported local database command '+command)
    def close(self):
        with self.lock:
            for conn in list(self.connections.values()):conn.close()
            self.connections.clear()
    def integrity_check(self):return self._connection().execute('PRAGMA integrity_check').fetchone()[0]


class Session:
    def __init__(self,database=None):
        self.database=database;self.connection=None;self.token=None;self.in_transaction=False
        self.write_locked=False;self.write_metadata={}
    def bind(self,database):
        if self.database is not None and self.database.path!=database.path:raise OperationFailure('One transaction cannot span database files')
        self.database=database
        if self.connection is None:self.connection=database._thread_connection()
        return self.connection
    def __enter__(self):return self
    def __exit__(self,*args):
        if self.in_transaction:
            self.connection.rollback();self.in_transaction=False
        self.write_locked=False;self.write_metadata.clear()
        if self.token is not None:_session.reset(self.token);self.token=None
    @contextmanager
    def start_transaction(self,write=True,**kwargs):
        if self.in_transaction:raise OperationFailure('Nested transaction must reuse its owner')
        conn=self.bind(self.database) if self.database else None
        self.token=_session.set(self)
        self.in_transaction=True
        self.write_metadata.clear()
        try:
            if conn is None:raise OperationFailure('Transaction has no database owner')
            conn.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            self.write_locked=bool(write)
            yield self
            conn.commit()
        except BaseException as failure:
            if conn is not None:
                conn.rollback()
                # Opening a collection can run SQL and register another handle
                # concurrently. Snapshot under the registry lock, then release
                # it before SQLite work. Retain committed handles to avoid a
                # rollback causing schema-write storms in unrelated readers.
                with self.database.lock:collections=tuple(self.database._collections.items())
                try:
                    for name,collection in collections:
                        exists=conn.execute('SELECT 1 FROM _collections WHERE name=?',(name,)).fetchone()
                        collection._paths=({r[0] for r in conn.execute('SELECT path FROM _paths WHERE collection=?',(name,))}
                                           if exists else set())
                        if not exists:
                            with self.database.lock:
                                if self.database._collections.get(name) is collection:self.database._collections.pop(name)
                except sqlite3.Error as cleanup_error:
                    # A failed refresh cannot leave uncommitted path hints or
                    # replace the original operational error needed for retry.
                    with self.database.lock:
                        for name,collection in collections:
                            collection._paths=set()
                            if self.database._collections.get(name) is collection:self.database._collections.pop(name)
                    failure.add_note('Rollback metadata refresh failed: '+str(cleanup_error))
            raise
        finally:
            self.in_transaction=False;self.write_locked=False;self.write_metadata.clear()
            _session.reset(self.token);self.token=None
    def with_transaction(self,callback,**kwargs):
        with self.start_transaction():return callback(self)


class Client:
    def __init__(self,database):self.database=database
    def start_session(self,**kwargs):return Session(self.database)
    @property
    def admin(self):return self.database
    def __getitem__(self,name):
        if name==self.database.name:return self.database
        return Database(self.database.path.with_name(name+'.sqlite3'),name=name)
    def list_database_names(self):return [p.stem for p in self.database.path.parent.glob('*.sqlite3')]
    def drop_database(self,name):
        from .database import check_name,_databases,_lock
        check_name(name)
        folder=self.database.path.parent.resolve()
        target=folder/(name+'.sqlite3')
        if target.resolve().parent!=folder or target.is_symlink():raise ValueError('Database removal escapes its records folder')
        for database in list(_instances):
            if database.path==target:
                result=database._connection().execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if result[0]:raise OperationFailure('Database is busy; removal was not confirmed')
                database.close();database._collections.clear()
        with _lock:
            for key,db in list(_databases.items()):
                if db.path==target:_databases.pop(key)
        sidecars=[target.with_name(target.name+suffix) for suffix in ('-wal','-shm','-journal')]
        if any(p.is_symlink() for p in sidecars):raise ValueError('Refusing linked database recovery file')
        target.unlink(missing_ok=True)
        for sidecar in sidecars:sidecar.unlink(missing_ok=True)
        from . import legacy
        legacy.forget()
    def close(self):self.database.close()


def count_options(options):
    """Validate the bounded-count options used by list APIs and read-only tools."""
    skip=options.get('skip',0)
    if not isinstance(skip,int) or isinstance(skip,bool):raise TypeError('Count skip must be an integer')
    if skip<0:raise ValueError('Count skip must not be negative')
    maximum=options.get('limit',0)
    if 'limit' in options:
        if not isinstance(maximum,int) or isinstance(maximum,bool):raise TypeError('Count limit must be an integer')
        if maximum<=0:raise ValueError('Count limit must be positive')
    return {'skip':skip,'limit':maximum}


class Cursor:
    def __init__(self,collection,query=None,projection=None,**kwargs):
        self.collection,self.query,self.projection=collection,query or {},projection
        self.order=kwargs.get('sort') or [];self.offset=kwargs.get('skip',0);self.maximum=kwargs.get('limit',0);self.size=kwargs.get('batch_size',100)
    def sort(self,key,direction=1):self.order=list(key) if isinstance(key,(list,tuple)) else [(key,direction)];return self
    def skip(self,count):self.offset=int(count);return self
    def limit(self,count):self.maximum=abs(int(count));return self
    def batch_size(self,count):self.size=max(1,int(count));return self
    def hint(self,*args,**kwargs):return self
    def allow_disk_use(self,*args):return self
    def close(self):pass
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def _count_sql(self,selection):
        if self.maximum:selection+=' LIMIT '+str(self.maximum)
        elif self.offset:selection+=' LIMIT -1'
        if self.offset:selection+=' OFFSET '+str(self.offset)
        return 'SELECT count(*) FROM ('+selection+')'
    def _row_fields(self,alias='d'):
        projection=self.projection
        if isinstance(projection,(list,tuple,set)):projection={key:1 for key in projection}
        if not isinstance(projection,dict):return 'json('+alias+'.payload),'+alias+'.types'
        included=[key for key,value in projection.items() if value and key!='_id']
        if not included and projection!={'_id':1}:return 'json('+alias+'.payload),'+alias+'.types'
        roots={key.split('.')[0] for key in included}
        if projection.get('_id',1):roots.add('_id')
        wanted=','.join(literal(key) for key in sorted(roots))
        # Narrow to requested root branches before text conversion and decoding.
        # Python retains the exact dotted-array projection and typed semantics.
        # Arrow returns the original JSON token. Round-tripping a scalar
        # through SQLite REAL/json_quote would round doubles and lose -0.0.
        payload='(SELECT json_group_object(_selected.key,json('+alias+'.payload -> _selected.fullkey)) FROM json_each('+alias+'.payload) _selected WHERE _selected.key IN ('+wanted+'))'
        marks="(SELECT json_group_array(json(_type.value)) FROM json_each("+alias+".types) _type WHERE json_extract(_type.value,'$[0][0]') IN ("+wanted+'))'
        return payload+','+marks
    def _sql(self,count=False):
        equality=single_equality(self.query)
        if equality is not None and (count or len(self.order)==1 and self.order[0][0]=='_id'):
            name,kind,value=equality
            self.collection.ensure_path(name)
            predicate='path=? AND kind=? AND value=?'
            params=[name,kind,value]
            source=self.collection._values
            if count:
                return self._count_sql('SELECT DISTINCT doc_key FROM '+source+' WHERE '+predicate),params
            direction=' DESC' if self.order[0][1]<0 else ' ASC'
            # All duplicate multikey hits for a document are adjacent in this
            # covering index. DISTINCT preserves one document before pagination.
            ctes=[]
            if self.order[0][1]<0 and self.maximum:
                # Reverse traversal plus the fixed ascending typed-ID tie break
                # otherwise forces DISTINCT to sort the complete matching range.
                # K distinct sort values contain at least K distinct documents;
                # retain every boundary tie, then paginate the bounded set.
                bounds='SELECT DISTINCT id_sort FROM '+source+' INDEXED BY '+source+'_page WHERE '+predicate
                bounds+=' ORDER BY id_sort DESC LIMIT '+str(self.maximum+max(0,self.offset))
                ctes.append('bounds AS MATERIALIZED ('+bounds+')')
                eligible='SELECT DISTINCT v.id_sort,v.doc_key FROM bounds b CROSS JOIN '+source+' v INDEXED BY '+source+'_page WHERE v.path=? AND v.kind=? AND v.value=? AND v.id_sort IS b.id_sort'
                eligible+=' ORDER BY v.id_sort DESC,v.doc_key ASC'
                params=params+params
            else:
                eligible='SELECT DISTINCT id_sort,doc_key FROM '+source+' INDEXED BY '+source+'_page WHERE '+predicate
                eligible+=' ORDER BY id_sort'+direction+',doc_key ASC'
            if self.maximum:eligible+=' LIMIT '+str(self.maximum)
            elif self.offset:eligible+=' LIMIT -1'
            if self.offset:eligible+=' OFFSET '+str(self.offset)
            ctes.append('eligible AS MATERIALIZED ('+eligible+')')
            sql='WITH '+','.join(ctes)+' SELECT '+self._row_fields()+' FROM eligible e JOIN '+self.collection._table+' d ON d.id_key=e.doc_key'
            sql+=' ORDER BY e.id_sort'+direction+',e.doc_key ASC'
            return sql,params
        # Broad anchors can add eager work to capped pages or count queries.
        # Keep those APIs on their existing planner path.
        anchor=membership_anchor(self.query) if not count and not self.maximum and not self.offset else None
        prefix='';source=self.collection._table+' d'
        if anchor is not None:
            name,condition=anchor
            driver=Query(self.collection)
            eligible='SELECT d.id_key FROM '+source+' WHERE '+driver.compile({name:condition})
            # The typed membership predicate deduplicates multikey hits before
            # any payload work. Residuals probe only these candidate documents;
            # LIMIT/OFFSET must remain after every residual has been evaluated.
            prefix='WITH membership_candidates AS MATERIALIZED ('+eligible+') '
            source='membership_candidates c JOIN '+self.collection._table+' d ON d.id_key=c.id_key'
            q=Query(self.collection,correlated=True)
            where=q.compile({key:value for key,value in self.query.items() if key!=name})
            params=driver.params+q.params
        else:
            q=Query(self.collection);where=q.compile(self.query);params=q.params
        sql=prefix+('SELECT 1' if count else 'SELECT '+self._row_fields())+' FROM '+source+' WHERE '+where
        if count:return self._count_sql(sql),params
        if not count:
            if self.order:sql+=' ORDER BY '+','.join(field('d.payload',name)+(' DESC' if direction<0 else ' ASC') for name,direction in self.order)
            else:sql+=' ORDER BY d.rowid'
            if self.maximum:sql+=' LIMIT '+str(self.maximum)
            elif self.offset:sql+=' LIMIT -1'
            if self.offset:sql+=' OFFSET '+str(self.offset)
        return sql,params
    def __iter__(self):
        sql,params=self._sql();cursor=self.collection.database._connection().execute(sql,params)
        try:
            while rows:=cursor.fetchmany(self.size):
                for payload,types in rows:yield project(codec.decode_document(payload,types),self.projection)
        finally:cursor.close()
    def __getitem__(self,index):
        if isinstance(index,slice):
            self.skip(index.start or 0)
            if index.stop is not None:self.limit(index.stop-(index.start or 0))
            return self
        for i,item in enumerate(self):
            if i==index:return item
        raise IndexError(index)
    def next(self):return next(iter(self))
    def distinct(self,key):return self.collection.distinct(key,self.query)
    def explain(self):
        sql,params=self._sql()
        return list(self.collection.database._connection().execute('EXPLAIN QUERY PLAN '+sql,params))


def project(document,projection):
    if projection is None:return document
    if isinstance(projection,(list,tuple,set)):projection={key:1 for key in projection}
    included=[key for key,value in projection.items() if value and key!='_id']
    if included or projection.get('_id')==1 and len(projection)==1:
        trie={}
        for path in included:
            branch=trie
            for part in path.split('.'):
                branch=branch.setdefault(part,{})
            branch[None]=True
        if projection.get('_id',1):trie['_id']={None:True}
        def select(value,branch):
            if None in branch:return copy.deepcopy(value)
            if isinstance(value,list):return [select(item,branch) for item in value]
            if isinstance(value,dict):return {key:select(value[key],child) for key,child in branch.items() if key in value}
            return copy.deepcopy(value)
        return select(document,trie)
    out=copy.deepcopy(document)
    def exclude(value,parts):
        if isinstance(value,list):
            for item in value:exclude(item,parts)
        elif isinstance(value,dict) and parts[0] in value:
            if len(parts)==1:value.pop(parts[0])
            else:exclude(value[parts[0]],parts[1:])
    for key,value in projection.items():
        if not value:exclude(out,key.split('.'))
    return out


class Collection:
    def __init__(self,database,name):
        self.database,self.name=database,name
        self._table=table_name(name);self._values=table_name(name,'v_');self._fts=table_name(name,'f_');self._fts_map=table_name(name,'m_');self._paths=set()
        with database.transaction():
            c=database._connection()
            c.execute('INSERT OR IGNORE INTO _collections(name) VALUES (?)',(name,))
            c.execute('CREATE TABLE IF NOT EXISTS '+self._table+'(id_key TEXT PRIMARY KEY,payload BLOB NOT NULL,types TEXT NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS '+self._table+'_id_sort ON '+self._table+'('+field('payload','_id')+')')
            c.execute('CREATE TABLE IF NOT EXISTS '+self._values+'(path TEXT NOT NULL,kind INTEGER NOT NULL,value,doc_key TEXT NOT NULL,ordinal TEXT NOT NULL,id_sort,PRIMARY KEY(path,doc_key,ordinal)) WITHOUT ROWID')
            columns={row[1] for row in c.execute('PRAGMA table_info('+self._values+')')}
            if 'id_sort' not in columns:
                c.execute('ALTER TABLE '+self._values+' ADD COLUMN id_sort')
                temporary='pagination_keys_'+uuid.uuid4().hex
                c.execute('CREATE TEMP TABLE '+temporary+'(doc_key TEXT PRIMARY KEY,id_sort)')
                try:
                    c.execute('INSERT INTO '+temporary+' SELECT id_key,'+field('payload','_id')+' FROM '+self._table)
                    c.execute('UPDATE '+self._values+' AS v SET id_sort=k.id_sort FROM '+temporary+' k WHERE k.doc_key=v.doc_key')
                    if c.execute('SELECT 1 FROM '+self._values+' v LEFT JOIN '+temporary+' k ON k.doc_key=v.doc_key WHERE k.doc_key IS NULL LIMIT 1').fetchone():
                        raise OperationFailure('Orphaned lookup rows prevent pagination index upgrade')
                finally:c.execute('DROP TABLE '+temporary)
            c.execute('CREATE INDEX IF NOT EXISTS '+self._values+'_lookup ON '+self._values+'(path,kind,value,doc_key)')
            c.execute('CREATE INDEX IF NOT EXISTS '+self._values+'_page ON '+self._values+'(path,kind,value,id_sort,doc_key)')
            c.execute('CREATE INDEX IF NOT EXISTS '+self._values+'_doc ON '+self._values+'(doc_key)')
            self._paths={r[0] for r in c.execute('SELECT path FROM _paths WHERE collection=?',(name,))}
            self._ensure_fts_map(c)
            database._invalidate_write_metadata(name)
        self.ensure_path('_id')
    def ensure_path(self,path):
        if path in self._paths:return
        with self.database.transaction():
            conn=self.database._connection()
            if not conn.execute('SELECT 1 FROM _paths WHERE collection=? AND path=?',(self.name,path)).fetchone():
                for key,payload,types,id_sort in conn.execute('SELECT id_key,json(payload),types,'+field('payload','_id')+' FROM '+self._table):
                    self._index_path(conn,key,codec.decode_document(payload,types),path,id_sort=id_sort)
                conn.execute('INSERT INTO _paths(collection,path) VALUES (?,?)',(self.name,path))
                self.database._invalidate_write_metadata(self.name)
            self._paths.add(path)
    def _write_metadata(self,conn):
        session=_session.get()
        cache=session.write_metadata if session is not None and session.in_transaction and session.write_locked else None
        if cache is not None and self.name in cache:return cache[self.name]
        from .collection_options import unpack_options
        row=conn.execute('SELECT validator,options FROM _collections WHERE name=?',(self.name,)).fetchone()
        options=unpack_options(row[1],row[0]) if row else {}
        paths=frozenset(r[0] for r in conn.execute('SELECT path FROM _paths WHERE collection=?',(self.name,)))
        fts_paths=tuple(r[0] for r in conn.execute('SELECT path FROM _fts_paths WHERE collection=?',(self.name,)))
        self._paths=set(paths)
        metadata=options,paths,fts_paths
        # BEGIN IMMEDIATE excludes other writers. In-process schema changes
        # invalidate this session's entry, and every transaction starts fresh.
        if cache is not None:cache[self.name]=metadata
        return metadata
    def _ensure_fts_map(self,conn):
        exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(self._fts_map,)).fetchone()
        if exists:return
        conn.execute('CREATE TABLE '+self._fts_map+'(fts_rowid INTEGER PRIMARY KEY,doc_key TEXT NOT NULL)')
        conn.execute('CREATE INDEX '+self._fts_map+'_doc ON '+self._fts_map+'(doc_key)')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(self._fts,)).fetchone():
            conn.execute('INSERT INTO '+self._fts_map+' SELECT rowid,doc_key FROM '+self._fts)
    def _insert_fts(self,conn,rows):
        for path,key,text in rows:
            row=conn.execute('INSERT INTO '+self._fts+'(path,doc_key,text) VALUES (?,?,?)',(path,key,text))
            conn.execute('INSERT INTO '+self._fts_map+'(fts_rowid,doc_key) VALUES (?,?)',(row.lastrowid,key))
    def ensure_fts_path(self,path):
        self.ensure_path(path)
        with self.database.transaction():
            conn=self.database._connection()
            self._ensure_fts_map(conn)
            conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS '+self._fts+" USING fts5(path UNINDEXED,doc_key UNINDEXED,text,tokenize='trigram')")
            if conn.execute('SELECT 1 FROM _fts_paths WHERE collection=? AND path=?',(self.name,path)).fetchone():return
            from .query import fold
            cursor=conn.execute('SELECT doc_key,value FROM '+self._values+' WHERE path=? AND kind=3',(path,))
            while rows:=cursor.fetchmany(200):
                self._insert_fts(conn,((path,key,fold(value)) for key,value in rows))
            conn.execute('INSERT INTO _fts_paths(collection,path) VALUES (?,?)',(self.name,path))
            self.database._invalidate_write_metadata(self.name)
    def _refresh_fts(self,conn,key,document=None,*,changed_roots=None,fts_paths=None):
        paths=self._write_metadata(conn)[2] if fts_paths is None else fts_paths
        if not paths:return
        if changed_roots is not None and not any(path.split('.')[0] in changed_roots for path in paths):return
        self._ensure_fts_map(conn)
        conn.execute('DELETE FROM '+self._fts+' WHERE rowid IN (SELECT fts_rowid FROM '+self._fts_map+' WHERE doc_key=?)',(key,))
        conn.execute('DELETE FROM '+self._fts_map+' WHERE doc_key=?',(key,))
        if document is not None:
            from .query import fold
            rows=((path,key,fold(value)) for path in paths for value,_ in values(document,path) if isinstance(value,str) and type(value).__name__!='ObjectId')
            self._insert_fts(conn,rows)
    def _index_path(self,conn,key,document,path,*,id_sort=MISSING):
        if id_sort is MISSING:
            row=conn.execute('SELECT '+field('payload','_id')+' FROM '+self._table+' WHERE id_key=?',(key,)).fetchone()
            if row is None:raise OperationFailure('Lookup index target disappeared')
            id_sort=row[0]
        self._insert_lookup_rows(conn,self._lookup_rows(key,document,(path,),id_sort))
    def _lookup_rows(self,key,document,paths,id_sort):
        for path in paths:
            for value,ordinal in _index_values(document,path):
                kind,encoded=value_key(value)
                yield path,kind,encoded,key,ordinal,id_sort
    def _insert_lookup_rows(self,conn,rows):
        sql='INSERT OR REPLACE INTO '+self._values+'(path,kind,value,doc_key,ordinal,id_sort) VALUES (?,?,?,?,?,?)'
        batch=[];size=0
        for row in rows:
            # UTF-8 needs at most four bytes per code point. A single encoded
            # lookup value may exceed the budget, but never share that batch.
            width=sum(len(item)*4 if isinstance(item,str) else 16 for item in row)
            if batch and (len(batch)>=256 or size+width>1<<20):
                conn.executemany(sql,batch);batch=[];size=0
            batch.append(row);size+=width
        if batch:conn.executemany(sql,batch)
    def _write(self,document,*,replace=False,previous=None,encoded=None):
        if '_id' not in document:
            document['_id']=ObjectId();encoded=None
        from .validation import validate_write
        conn=self.database._connection()
        options,all_paths,fts_paths=self._write_metadata(conn);validator=options.get('validator',{})
        if replace and previous is None:
            row=conn.execute('SELECT json(payload),types FROM '+self._table+' WHERE id_key=?',(codec.id_key(document['_id']),)).fetchone()
            if row:previous=codec.decode_document(*row)
        validate_write(document,validator,options,previous)
        # Validation is read-only. An update can reuse its comparison encoding,
        # including the lossless type sidecar, after validation succeeds.
        payload,types=codec.encode_document(document) if encoded is None else encoded
        key=codec.id_key(document['_id'])
        conn=self.database._connection()
        try:
            if replace:
                changed=conn.execute('UPDATE '+self._table+' SET payload=jsonb(?),types=? WHERE id_key=?',(payload,types,key))
                if changed.rowcount!=1:raise OperationFailure('Replacement target disappeared before its indexes could be updated')
            else:conn.execute('INSERT INTO '+self._table+'(id_key,payload,types) VALUES (?,jsonb(?),?)',(key,payload,types))
            changed_roots=None
            paths=all_paths
            if replace and previous is not None:
                roots={path.split('.')[0] for path in paths}
                # Typed lookup equality avoids bool/int, ObjectId/string and binary
                # subtype collisions. A changed root rebuilds all its multikey ordinals.
                changed_roots={root for root in roots if (root in previous)!=(root in document)
                    or root in document and (value_key(previous[root])!=value_key(document[root])
                        or _contains_regex(previous[root]))}
                paths={path for path in paths if path.split('.')[0] in changed_roots}
                # Equivalent numeric IDs can still change their stored sort value.
                if codec.encode_document(previous['_id'])!=codec.encode_document(document['_id']):paths=all_paths
                for path in paths:conn.execute('DELETE FROM '+self._values+' WHERE path=? AND doc_key=?',(path,key))
            if paths:
                id_sort=conn.execute('SELECT '+field('payload','_id')+' FROM '+self._table+' WHERE id_key=?',(key,)).fetchone()[0]
                self._insert_lookup_rows(conn,self._lookup_rows(key,document,paths,id_sort))
            self._refresh_fts(conn,key,document,changed_roots=changed_roots,fts_paths=fts_paths)
        except sqlite3.IntegrityError as error:raise DuplicateKeyError('E11000 '+str(error)) from error
        return document['_id']
    def find(self,filter=None,projection=None,**kwargs):return Cursor(self,filter,projection,**kwargs)
    def find_one(self,filter=None,projection=None,**kwargs):
        if filter is not None and not isinstance(filter,dict):filter={'_id':filter}
        return next(iter(self.find(filter,projection,**kwargs).limit(1)),None)
    def count_documents(self,filter=None,**kwargs):
        cursor=self.find(filter,**count_options(kwargs));sql,params=cursor._sql(count=True)
        return self.database._connection().execute(sql,params).fetchone()[0]
    def estimated_document_count(self,**kwargs):return self.count_documents({})
    def explain(self,filter=None):return self.find(filter).explain()
    def insert_one(self,document,**kwargs):
        with self.database.transaction():identifier=self._write(document)
        return SimpleNamespace(inserted_id=identifier,acknowledged=True)
    def insert_many(self,documents,ordered=True,**kwargs):
        identifiers=[];errors=[]
        with self.database.transaction():
            for at,document in enumerate(documents):
                try:identifiers.append(self._write(document))
                except DuplicateKeyError as error:
                    errors.append({'index':at,'code':11000,'errmsg':str(error)})
                    if ordered:break
        if errors:raise BulkWriteError({'writeErrors':errors,'nInserted':len(identifiers)})
        return SimpleNamespace(inserted_ids=identifiers,acknowledged=True)
    def replace_one(self,filter,replacement,upsert=False,**kwargs):
        with self.database.transaction():
            old=self.find_one(filter)
            if old is None and not upsert:return SimpleNamespace(matched_count=0,modified_count=0,upserted_id=None)
            value=copy.deepcopy(replacement)
            if old is not None:
                value.setdefault('_id',old['_id'])
                if codec.id_key(value['_id'])!=codec.id_key(old['_id']):raise OperationFailure('Immutable _id cannot change')
            elif '_id' not in value and '_id' in filter:
                candidate=filter['_id']
                if not isinstance(candidate,dict):value['_id']=candidate
                elif set(candidate)=={'$eq'}:value['_id']=candidate['$eq']
            identifier=self._write(value,replace=old is not None,previous=old)
        return SimpleNamespace(matched_count=int(old is not None),modified_count=int(old is not None and codec.encode_document(old)!=codec.encode_document(value)),upserted_id=identifier if old is None else None)
    def update_one(self,filter,update,upsert=False,**kwargs):return self._update(filter,update,upsert,False)
    def update_many(self,filter,update,upsert=False,**kwargs):return self._update(filter,update,upsert,True)
    def _update(self,filter,update,upsert,many):
        matched=modified=0;identifier=None;insert=False
        with self.database.transaction():
            if many:
                q=Query(self);where=q.compile(filter)
                conn=self.database._connection();targets='mutation_'+uuid.uuid4().hex
                # Freeze eligible IDs on disk so changed predicates cannot revisit rows.
                conn.execute('CREATE TEMP TABLE '+targets+' AS SELECT d.id_key FROM '+self._table+' d WHERE '+where,q.params)
                cursor=conn.execute('SELECT json(d.payload),d.types FROM '+targets+' t JOIN '+self._table+' d ON d.id_key=t.id_key')
                def documents():
                    while batch:=cursor.fetchmany(200):
                        for payload,types in batch:yield codec.decode_document(payload,types)
                rows=documents()
            else:
                old=self.find_one(filter);rows=iter([] if old is None else [old])
            try:
                for old in rows:
                    value=copy.deepcopy(old)
                    apply_update(value,update,inserting=False)
                    if codec.id_key(value.get('_id'))!=codec.id_key(old['_id']):raise OperationFailure('Immutable _id cannot change')
                    encoded=codec.encode_document(value) if '_id' in value else None
                    if (codec.encode_document(value) if encoded is None else encoded)!=codec.encode_document(old):
                        self._write(value,replace=True,previous=old,encoded=encoded);modified+=1
                    matched+=1
                if not matched and upsert:
                    insert=True
                    value={k:v for k,v in filter.items() if not k.startswith('$') and not isinstance(v,dict)}
                    apply_update(value,update,inserting=True)
                    identifier=self._write(value)
            finally:
                if many:cursor.close();conn.execute('DROP TABLE '+targets)
        return SimpleNamespace(matched_count=matched,modified_count=modified,upserted_id=identifier if insert else None)
    def find_one_and_update(self,filter,update,upsert=False,return_document=False,**kwargs):
        with self.database.transaction():
            old=self.find_one(filter,sort=kwargs.get('sort'))
            if old is None and not upsert:return None
            result=self.update_one({'_id':old['_id']} if old else filter,update,upsert=upsert)
            identifier=old['_id'] if old else result.upserted_id
            return self.find_one({'_id':identifier}) if return_document else old
    def find_one_and_replace(self,filter,replacement,upsert=False,return_document=False,**kwargs):
        with self.database.transaction():
            old=self.find_one(filter);result=self.replace_one(filter,replacement,upsert=upsert)
            return self.find_one({'_id':old['_id'] if old else result.upserted_id}) if return_document else old
    def find_one_and_delete(self,filter,**kwargs):
        with self.database.transaction():
            old=self.find_one(filter)
            if old:self.delete_one({'_id':old['_id']})
            return old
    def delete_one(self,filter,**kwargs):return self._delete(filter,False)
    def delete_many(self,filter,**kwargs):return self._delete(filter,True)
    def _delete(self,filter,many):
        count=0
        with self.database.transaction():
            q=Query(self);where=q.compile(filter);conn=self.database._connection()
            targets='deletion_'+uuid.uuid4().hex
            conn.execute('CREATE TEMP TABLE '+targets+' AS SELECT d.id_key FROM '+self._table+' d WHERE '+where+(' LIMIT 1' if not many else ''),q.params)
            cursor=conn.execute('SELECT id_key FROM '+targets)
            try:
                while rows:=cursor.fetchmany(500):
                    keys=[r[0] for r in rows];placeholders=','.join('?' for _ in keys)
                    for key in keys:self._refresh_fts(conn,key)
                    conn.execute('DELETE FROM '+self._values+' WHERE doc_key IN ('+placeholders+')',keys)
                    conn.execute('DELETE FROM '+self._table+' WHERE id_key IN ('+placeholders+')',keys)
                    count+=len(keys)
            finally:cursor.close();conn.execute('DROP TABLE '+targets)
        return SimpleNamespace(deleted_count=count,acknowledged=True)
    def distinct(self,key,filter=None,**kwargs):
        self.ensure_path(key);q=Query(self);where=q.compile(filter or {})
        sql='SELECT DISTINCT v.kind,v.value FROM '+self._values+' v JOIN '+self._table+' d ON d.id_key=v.doc_key WHERE v.path=? AND ('+where+') ORDER BY v.kind,v.value'
        out=[]
        for kind,value in self.database._connection().execute(sql,[key,*q.params]):
            if kind==4:
                payload,marks=json.loads(value)
                value=codec.decode_document(payload,marks)
                if isinstance(value,list):continue
            if kind==1:value=bool(value)
            elif kind==5:
                import datetime
                value=codec.EPOCH+datetime.timedelta(microseconds=value)
            elif kind==6:value=ObjectId(value)
            elif kind==7:
                import base64
                from .types import Binary
                subtype,raw=json.loads(value);value=Binary(base64.b64decode(raw),subtype)
            elif kind in (8,9,10):
                payload,marks=json.loads(value);value=codec.decode_document(payload,marks)
            out.append(value)
        return out
    def bulk_write(self,requests,ordered=True,**kwargs):
        from .operations import InsertOne,ReplaceOne,UpdateOne,UpdateMany,DeleteMany,DeleteOne
        result={'inserted_count':0,'matched_count':0,'modified_count':0,'deleted_count':0,'upserted_count':0,'upserted_ids':{}};errors=[]
        with self.database.transaction():
            for at,request in enumerate(requests):
                try:
                    if isinstance(request,InsertOne):self.insert_one(request._doc);result['inserted_count']+=1
                    elif isinstance(request,ReplaceOne):
                        r=self.replace_one(request._filter,request._doc,upsert=request._upsert);result['matched_count']+=r.matched_count;result['modified_count']+=r.modified_count
                        if r.upserted_id is not None:result['upserted_count']+=1;result['upserted_ids'][at]=r.upserted_id
                    elif isinstance(request,UpdateOne):
                        r=self._update(request._filter,request._doc,request._upsert,isinstance(request,UpdateMany));result['matched_count']+=r.matched_count;result['modified_count']+=r.modified_count
                        if r.upserted_id is not None:result['upserted_count']+=1;result['upserted_ids'][at]=r.upserted_id
                    elif isinstance(request,DeleteMany):result['deleted_count']+=self._delete(request._filter,not isinstance(request,DeleteOne)).deleted_count
                    else:raise TypeError('Unsupported write request '+type(request).__name__)
                except DuplicateKeyError as error:
                    errors.append({'index':at,'code':11000,'errmsg':str(error)})
                    if ordered:break
        if errors:raise BulkWriteError({'writeErrors':errors,**result})
        return SimpleNamespace(**result,bulk_api_result=result,acknowledged=True)
    def create_index(self,keys,**options):
        if isinstance(keys,str):keys=[(keys,1)]
        keys=[tuple(item) if not isinstance(item,str) else (item,1) for item in keys]
        name=options.get('name') or '_'.join(str(k)+'_'+str(d) for k,d in keys)
        if name=='_id_' or keys==[('_id',1)]:return '_id_'
        conn=self.database._connection();existing=conn.execute('SELECT spec FROM _indexes WHERE collection=? AND name=?',(self.name,name)).fetchone()
        if existing:return name
        with self.database.transaction():
            if conn.execute('SELECT 1 FROM _indexes WHERE collection=? AND name=?',(self.name,name)).fetchone():return name
            for path,direction in keys:self.ensure_path(path)
            index=table_name(self.name+'\0'+name,'i_')
            columns=','.join(('json_quote('+field('payload',key)+')' if options.get('unique') else field('payload',key))+(' DESC' if direction<0 else '') for key,direction in keys)
            where=''
            partial=options.get('partialFilterExpression')
            if partial:
                from .query import partial_predicate
                where=' WHERE '+partial_predicate(partial)
            try:conn.execute('CREATE '+('UNIQUE ' if options.get('unique') else '')+'INDEX IF NOT EXISTS '+index+' ON '+self._table+'('+columns+')'+where)
            except sqlite3.IntegrityError as error:raise DuplicateKeyError(str(error)) from error
            spec={'name':name,'key':dict(keys),**{k:v for k,v in options.items() if k!='name'}}
            conn.execute('INSERT INTO _indexes(collection,name,spec) VALUES (?,?,?)',(self.name,name,json.dumps(spec)))
            self.database._invalidate_write_metadata(self.name)
        return name
    def list_indexes(self):
        yield {'name':'_id_','key':{'_id':1},'unique':True}
        yield from (json.loads(r[0]) for r in self.database._connection().execute('SELECT spec FROM _indexes WHERE collection=?',(self.name,)))
    def index_information(self):return {row['name']:row for row in self.list_indexes()}
    def drop_index(self,name):
        with self.database.transaction():
            self.database._connection().execute('DROP INDEX IF EXISTS '+table_name(self.name+'\0'+name,'i_'))
            self.database._connection().execute('DELETE FROM _indexes WHERE collection=? AND name=?',(self.name,name))
            self.database._invalidate_write_metadata(self.name)
    def list_search_indexes(self,name=None):
        for row in self.database._connection().execute('SELECT spec FROM _search_indexes WHERE collection=?'+(' AND name=?' if name else ''),(self.name,name) if name else (self.name,)):
            yield {**json.loads(row[0]),'queryable':True,'status':'READY'}
    def create_search_index(self,model,**kwargs):
        value={'name':model.name,'definition':model.definition,'type':model.type}
        self.database._connection().execute('INSERT OR REPLACE INTO _search_indexes(collection,name,spec) VALUES (?,?,?)',(self.name,model.name,json.dumps(value)))
        return model.name
    def update_search_index(self,name,definition):
        from .operations import SearchIndexModel
        return self.create_search_index(SearchIndexModel(definition,name))
    def aggregate(self,pipeline,**kwargs):
        from .aggregation import aggregate
        return aggregate(self,pipeline)
    def drop(self):return self.database.drop_collection(self.name)


def apply_update(document,update,*,inserting=False):
    if not isinstance(update,dict) or not all(k.startswith('$') for k in update):raise ValueError('Updates require explicit operators')
    for operator,fields in update.items():
        for path,value in fields.items():
            old=get(document,path,MISSING)
            if operator=='$set' or operator=='$setOnInsert' and inserting:put(document,path,copy.deepcopy(value))
            elif operator=='$setOnInsert':continue
            elif operator=='$unset':remove(document,path)
            elif operator=='$inc':put(document,path,(0 if old is MISSING else old)+value)
            elif operator in ('$max','$min'):
                if old is MISSING or (value>old if operator=='$max' else value<old):put(document,path,value)
            elif operator in ('$push','$addToSet'):
                current=[] if old is MISSING else list(old)
                additional=value.get('$each') if isinstance(value,dict) and '$each' in value else [value]
                for item in additional:
                    if operator=='$push' or not any(value_key(item)==value_key(existing) for existing in current):current.append(copy.deepcopy(item))
                if isinstance(value,dict) and '$slice' in value:current=current[:value['$slice']] if value['$slice']>=0 else current[value['$slice']:]
                put(document,path,current)
            elif operator=='$pull':
                if old is MISSING:continue
                if isinstance(value,dict):raise NotImplementedError('Conditional pull is not implemented')
                put(document,path,[item for item in old if value_key(item)!=value_key(value)])
            elif operator=='$currentDate':
                import datetime
                put(document,path,datetime.datetime.now(datetime.timezone.utc))
            else:raise NotImplementedError('Unsupported update operator '+operator)
