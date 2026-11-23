"""Store-neutral write requests and errors used by repository code."""
from dataclasses import dataclass
from types import SimpleNamespace
ASCENDING=1
DESCENDING=-1
class ReturnDocument:
    BEFORE=False
    AFTER=True
class DatabaseError(RuntimeError):pass
class OperationFailure(DatabaseError):
    def __init__(self,message='',code=None,details=None):
        super().__init__(message);self.code=code;self.details=details or {}
class DuplicateKeyError(OperationFailure):
    def __init__(self,message='E11000 duplicate key',code=11000,details=None):super().__init__(message,code,details)
class BulkWriteError(OperationFailure):
    def __init__(self,details):super().__init__('Bulk write failed',details=details)
class ConnectionFailure(DatabaseError):pass
class AutoReconnect(ConnectionFailure):pass
class NetworkTimeout(ConnectionFailure):pass
class ServerSelectionTimeoutError(ConnectionFailure):pass
@dataclass
class InsertOne:
    _doc:dict
@dataclass
class ReplaceOne:
    _filter:dict
    _doc:dict
    _upsert:bool=False
    def __init__(self,filter,replacement,upsert=False,**kwargs):self._filter,self._doc,self._upsert=filter,replacement,upsert
@dataclass
class UpdateOne:
    _filter:dict
    _doc:dict
    _upsert:bool=False
    def __init__(self,filter,update,upsert=False,**kwargs):self._filter,self._doc,self._upsert=filter,update,upsert
class UpdateMany(UpdateOne):pass
@dataclass
class DeleteMany:
    _filter:dict
class DeleteOne(DeleteMany):pass
@dataclass
class SearchIndexModel:
    definition:dict
    name:str
    type:str='search'
