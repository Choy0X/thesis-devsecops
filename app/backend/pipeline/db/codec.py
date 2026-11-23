"""Typed JSON payloads with queryable scalars and a lossless type sidecar."""
import base64
import datetime as dt
import json
import math
import re
from .types import Binary, ObjectId, Int64, Decimal128, Timestamp, Regex
EPOCH=dt.datetime(1970,1,1,tzinfo=dt.timezone.utc)

def scalar(value):
    if isinstance(value,dt.datetime):
        delta=(value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc))-EPOCH
        return (delta.days*86400+delta.seconds)*1_000_000+delta.microseconds
    if isinstance(value,bytes):return base64.b64encode(value).decode('ascii')
    if type(value).__name__=='ObjectId':return str(value)
    if type(value).__name__=='Decimal128':return str(value)
    return value

def encode_document(document):
    types=[]
    def visit(value,path):
        name=type(value).__name__
        if isinstance(value,dt.datetime):
            types.append([path,'datetime',None if value.tzinfo is None else int(value.utcoffset().total_seconds())])
            return scalar(value)
        if name=='ObjectId':types.append([path,'oid',None]);return str(value)
        if isinstance(value,bytes):
            subtype=getattr(value,'subtype',None)
            types.append([path,'binary' if subtype is not None else 'bytes',subtype]);return scalar(value)
        if name=='Int64':types.append([path,'int64',None]);return int(value)
        if name=='Decimal128':types.append([path,'decimal128',None]);return str(value)
        if name=='Timestamp':types.append([path,'timestamp',None]);return [value.time,value.inc]
        if name=='Regex' or isinstance(value,re.Pattern):
            types.append([path,'regex',None]);return [value.pattern,int(value.flags)]
        if isinstance(value,float) and not math.isfinite(value):
            types.append([path,'float',None]);return repr(value)
        if isinstance(value,dict):return {str(k):visit(v,path+[str(k)]) for k,v in value.items()}
        if isinstance(value,(list,tuple)):
            if isinstance(value,tuple):types.append([path,'tuple',None])
            return [visit(v,path+[i]) for i,v in enumerate(value)]
        if value is None or isinstance(value,(str,int,float,bool)):return value
        raise TypeError('Unsupported persistent value type: '+name)
    payload=visit(document,[])
    return json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False),json.dumps(types,separators=(',',':'))

def decode_document(payload,types='[]',*,allow_missing_types=False):
    result=json.loads(payload) if isinstance(payload,(str,bytes,bytearray)) else payload
    marks=json.loads(types) if isinstance(types,(str,bytes,bytearray)) else types
    for path,kind,extra in reversed(marks):
        parent=result
        try:
            for part in path[:-1]:parent=parent[part]
            value=parent[path[-1]] if path else result
        except (KeyError,IndexError,TypeError):
            if allow_missing_types:continue
            raise ValueError('Persistent type metadata references a missing field') from None
        if kind=='datetime':
            value=EPOCH+dt.timedelta(microseconds=value)
            value=value.replace(tzinfo=None) if extra is None else value.astimezone(dt.timezone(dt.timedelta(seconds=extra)))
        elif kind=='oid':value=ObjectId(value)
        elif kind in ('binary','bytes'):
            value=base64.b64decode(value,validate=True)
            if kind=='binary':value=Binary(value,extra)
        elif kind=='int64':value=Int64(value)
        elif kind=='decimal128':value=Decimal128(value)
        elif kind=='timestamp':value=Timestamp(*value)
        elif kind=='regex':value=Regex(*value)
        elif kind=='float':value=float(value)
        elif kind=='tuple':value=tuple(value)
        else:raise ValueError('Unsupported persistent type tag: '+kind)
        if path:parent[path[-1]]=value
        else:result=value
    return result

def canonical_bytes(document):
    payload,marks=encode_document(document)
    types=json.loads(marks);types.sort(key=lambda x:json.dumps(x[0],ensure_ascii=True))
    return json.dumps([json.loads(payload),types],sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')

def id_key(value):
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        if isinstance(value,int) or math.isfinite(value) and value.is_integer():value=int(value)
    payload,types=encode_document(value)
    return '['+payload+','+types+']'
