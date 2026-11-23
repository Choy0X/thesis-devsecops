"""One fenced transaction publishes source projections and their durable receipt."""
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable
import time
from .pipeline_spool import StaleAttempt

_session=ContextVar("mining_publication_session",default=None)


def current_session():return _session.get()


@dataclass(frozen=True, slots=True)
class Publication:
    """One ordered write and its final pre-commit validity check."""
    item: str
    publication_id: str
    write: Callable[[], object]
    check: Callable[[], object] = lambda: None


class SessionCollection:
    def __init__(self,collection,session):self.collection,self.session=collection,session

    def __getattr__(self,name):
        value=getattr(self.collection,name)
        if not callable(value) or name not in {"find","find_one","aggregate","count_documents","distinct",
                "insert_one","insert_many","update_one","update_many","replace_one","delete_one","delete_many",
                "find_one_and_update","find_one_and_replace","find_one_and_delete","bulk_write"}:return value
        def call(*args,**kwargs):
            supplied=kwargs.get("session")
            if supplied is not None and supplied is not self.session:raise RuntimeError("Nested publication session")
            kwargs["session"]=self.session
            return value(*args,**kwargs)
        return call


class SessionDatabase:
    def __init__(self,db,session):self.db,self.session=db,session
    def __getitem__(self,name):return SessionCollection(self.db[name],self.session)
    def __getattr__(self,name):return getattr(self.db,name)


def session_database(db):
    session=current_session()
    return SessionDatabase(db,session) if session is not None else db


class Publisher:
    def __init__(self,scope,attempt):
        from app.backend.pipeline.db import legacy
        self.db=legacy.handle()
        self.scope,self.attempt=str(scope),str(attempt)
        # Schema exists before opening the transaction, including these collections.
        for name in ("mining_publications","mining_attempts"):
            if name not in self.db.list_collection_names():
                try:self.db.create_collection(name)
                except Exception:
                    if name not in self.db.list_collection_names():raise
        self.db["mining_attempts"].update_one({"_id":self.scope},{"$set":{"attempt":self.attempt},"$inc":{"generation":1}},upsert=True)
        self.generation=self.db["mining_attempts"].find_one({"_id":self.scope})["generation"]

    @classmethod
    def borrowed(cls,db,scope,attempt,generation):
        """Attach to an existing fence without claiming a new attempt generation."""
        if not isinstance(scope,str) or not scope or not isinstance(attempt,str) or not attempt or type(generation) is not int or generation<1:
            raise ValueError('Invalid borrowed publication lease')
        row=db['mining_attempts'].find_one({'_id':scope,'attempt':attempt,'generation':generation})
        if row is None:raise StaleAttempt('Campaign publication attempt was superseded')
        result=cls.__new__(cls)
        result.db,result.scope,result.attempt,result.generation=db,scope,attempt,generation
        return result

    def receipt(self,publication_id):
        row=self.db["mining_publications"].find_one({"_id":publication_id})
        return row.get("receipt") if row else None

    def publish(self,item,publication_id,write,*,check=lambda:None):
        from app.backend.pipeline.db import mongo
        def body(session):
            token=_session.set(session)
            try:
                check()
                db=SessionDatabase(self.db,session)
                # A write conflict fences a transaction concurrent with a new attempt.
                claim=db["mining_attempts"].update_one({"_id":self.scope,"generation":self.generation,"attempt":self.attempt},
                    {"$inc":{"publications":1}})
                if claim.matched_count!=1:raise StaleAttempt("Campaign publication attempt was superseded")
                if previous:=db["mining_publications"].find_one({"_id":publication_id}):return previous["receipt"]
                write()
                check()
                receipt={"publication_id":publication_id,"receipt":"mongo:"+publication_id}
                db["mining_publications"].insert_one({"_id":publication_id,"item":item,"scope":self.scope,
                    "attempt":self.attempt,"generation":self.generation,"at":time.time(),"receipt":receipt})
                return receipt
            finally:_session.reset(token)
        with mongo.client().start_session() as session:
            return session.with_transaction(body)


    def publish_many(self,publications):
        """Publish at most eight descriptors atomically, returning after commit.

        Validate the bounded input before starting a transaction or calling user
        code. Every item retains the single-publication write/receipt order;
        the existing outer transaction owns rollback of all collection metadata.
        """
        from app.backend.pipeline.db import mongo
        entries=[];metadata_bytes=0
        for entry in publications:
            if len(entries)==8:raise ValueError('Publication batch exceeds eight items')
            if not isinstance(entry,Publication):raise TypeError('Expected Publication descriptor')
            if not callable(entry.write) or not callable(entry.check):raise TypeError('Publication callbacks must be callable')
            for value in (entry.item,entry.publication_id):
                if not isinstance(value,str) or not value:raise ValueError('Publication identities must be nonempty strings')
                if len(value)>65536:raise ValueError('Publication identity metadata exceeds 64 KiB')
                metadata_bytes+=len(value.encode('utf-8'))
                if metadata_bytes>65536:raise ValueError('Publication identity metadata exceeds 64 KiB')
            entries.append(entry)
        if not entries:return []

        def body(session):
            token=_session.set(session)
            try:
                db=SessionDatabase(self.db,session);receipts=[]
                lease={"_id":self.scope,"generation":self.generation,"attempt":self.attempt}
                for entry in entries:
                    entry.check()
                    claim=db["mining_attempts"].update_one(lease,{"$inc":{"publications":1}})
                    if claim.matched_count!=1:raise StaleAttempt("Campaign publication attempt was superseded")
                    previous=db["mining_publications"].find_one({"_id":entry.publication_id})
                    if previous is not None:
                        receipts.append(previous["receipt"])
                        continue
                    entry.write()
                    entry.check()
                    receipt={"publication_id":entry.publication_id,"receipt":"mongo:"+entry.publication_id}
                    db["mining_publications"].insert_one({"_id":entry.publication_id,"item":entry.item,"scope":self.scope,
                        "attempt":self.attempt,"generation":self.generation,"at":time.time(),"receipt":receipt})
                    receipts.append(receipt)
                # A later write can invalidate an earlier item's operation or
                # lease. No prefix receipt escapes before these checks commit.
                for entry in entries:entry.check()
                if db["mining_attempts"].find_one(lease) is None:
                    raise StaleAttempt("Campaign publication attempt was superseded")
                return receipts
            finally:_session.reset(token)
        with mongo.client().start_session() as session:
            return session.with_transaction(body)


    def failure(self,item,publication_id,write,*,check=lambda:None):
        """Failure projections obey the same fence and cannot replace success."""
        from app.backend.pipeline.db import mongo
        def body(session):
            token=_session.set(session)
            try:
                check();db=SessionDatabase(self.db,session)
                claim=db['mining_attempts'].update_one({'_id':self.scope,'generation':self.generation,'attempt':self.attempt},
                    {'$inc':{'failures':1}})
                if claim.matched_count!=1:raise StaleAttempt('Campaign failure attempt was superseded')
                if previous:=db['mining_publications'].find_one({'_id':publication_id}):return previous['receipt']
                write();check()
                return None
            finally:_session.reset(token)
        with mongo.client().start_session() as session:return session.with_transaction(body)


def atomic(write):
    """Related recovery projections commit together, including the queue receipt."""
    from app.backend.pipeline.db import mongo
    if current_session() is not None:return write()
    def body(session):
        token=_session.set(session)
        try:return write()
        finally:_session.reset(token)
    with mongo.client().start_session() as session:
        return session.with_transaction(body)
