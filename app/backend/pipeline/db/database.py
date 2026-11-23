"""Application-local SQLite database ownership, schema and lifecycle.

MongoDB is an optional read-only migration source, never a runtime backend.
"""
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from .embedded import Database
from .operations import OperationFailure

DEFAULT_DB='aivf'
TEST_PREFIX='aivf_test_'
PROTECTED=frozenset({'admin','config','local','agentscenter'})
_lock=threading.RLock()
_databases={}
class DatabaseUnavailable(RuntimeError):pass

def db_name():return os.environ.get('ZTSA_DATABASE_NAME','').strip() or DEFAULT_DB

def check_name(name):
    if name in PROTECTED or not re.fullmatch(r'aivf(?:_[a-zA-Z0-9_-]+)?',name):raise ValueError('Not an application database name: '+repr(name))
    if os.environ.get('PYTEST_CURRENT_TEST') and not name.startswith(TEST_PREFIX):raise ValueError('Tests may only open '+TEST_PREFIX+'* database files')
    return name

def file_path(name=None):
    from app.backend import paths
    return paths.database_dir()/(check_name(name or db_name())+'.sqlite3')

def database(name=None):
    name=check_name(name or db_name());path=file_path(name)
    key=(os.getpid(),str(path))
    with _lock:
        db=_databases.get(key)
        if db is None:db=Database(path,name=name);_databases[key]=db
        return db

def client():return database().client

def close():
    with _lock:
        values=list(_databases.values());_databases.clear()
    for db in values:db.close()

def ping():
    try:
        database()._connection().execute('SELECT 1').fetchone()
        return True,''
    except Exception as error:return False,type(error).__name__+': '+str(error)[:250]

def require():
    ok,reason=ping()
    if not ok:raise DatabaseUnavailable('Local database unavailable: '+reason)

def ensure_schema(db=None,*,search=True,wait_search=False,wait_seconds=300,only=None):
    from . import schema
    db=db if db is not None else database()
    counts={'collections':0,'indexes':0,'search_indexes':0,'validators':0}
    with db.transaction():
        present=set(db.list_collection_names())
        for spec in schema.COLLECTIONS:
            if only is not None and spec.name not in only:continue
            if spec.name not in present:
                db.create_collection(spec.name,validator=spec.validator,validationLevel='moderate',validationAction='error');counts['collections']+=1
            else:
                db.command('collMod',spec.name,validator=spec.validator,validationLevel='moderate',validationAction='error');counts['validators']+=1
            collection=db[spec.name];indexes={index['name'] for index in collection.list_indexes()}
            for index in spec.indexes:
                if index.name not in indexes:
                    collection.create_index(list(index.keys),**index.options());counts['indexes']+=1
            if search:
                existing={i['name'] for i in collection.list_search_indexes()}
                from .operations import SearchIndexModel
                for index in spec.search_indexes:
                    if index.name not in existing:
                        collection.create_search_index(SearchIndexModel(index.definition,index.name,index.type));counts['search_indexes']+=1
        if only is None:db['system'].update_one({'_id':'schema_version'},{'$set':{'value':schema.SCHEMA_VERSION,'updated_at':time.time()}},upsert=True)
    return counts

def next_id(db,name,count=1):
    if type(count) is not int or count<1:raise ValueError('ID block count must be positive')
    doc=db['counters'].find_one_and_update({'_id':name},{'$inc':{'seq':count}},upsert=True,return_document=True)
    return doc['seq']-count+1

def status():
    from . import schema
    path=file_path();ok,reason=ping()
    out={'engine':'sqlite','embedded':True,'path':str(path),'uri':str(path),'database':db_name(),
         'reachable':ok,'reason':reason,'container':{'name':'','state':'not_required'},'version':sqlite3.sqlite_version,
         'replica_set':False,'search':True,'vector_search':'exact_local','journal_mode':'wal'}
    if ok:
        db=database();names=set(db.list_collection_names());row=db['system'].find_one({'_id':'schema_version'}) if 'system' in names else None
        out.update(collections={'expected':len(schema.COLLECTIONS),'present':len(names & {c.name for c in schema.COLLECTIONS})},schema_version=row.get('value') if row else None,
                   bytes=path.stat().st_size if path.exists() else 0)
    return out

def start_container():
    require();ensure_schema()
    return {'ok':True,'detail':'Local embedded database is ready.'}

def container_state():return {'name':'','state':'not_required'}
def container_name():return ''
def uri():return str(file_path())
def mask_uri(value):
    if '@' not in value:return value
    scheme,_,rest=value.partition('://')
    return scheme+'://***@'+rest.rsplit('@',1)[1]
def wait_search_indexes(db,seconds=300):return None
