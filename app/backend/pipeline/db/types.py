"""Lossless persistent value types, without a MongoDB runtime dependency."""
import datetime
import os
import re
import struct
import time
from dataclasses import dataclass
from types import SimpleNamespace

class Int64(int): pass
class ObjectId(str):
    def __new__(cls,value=None):
        value=value or (int(time.time()).to_bytes(4,'big')+os.urandom(8)).hex()
        if not re.fullmatch('[0-9a-fA-F]{24}',str(value)):
            raise ValueError('ObjectId requires 24 hexadecimal characters')
        return str.__new__(cls,str(value).lower())

class BinaryVectorDtype:
    FLOAT32=b'\x27'
    INT8=b'\x03'
    PACKED_BIT=b'\x10'

class Binary(bytes):
    def __new__(cls,data=b'',subtype=0):
        out=bytes.__new__(cls,data);out.subtype=int(subtype);return out
    @classmethod
    def from_vector(cls,values,dtype=BinaryVectorDtype.FLOAT32,padding=0):
        if hasattr(dtype,'value'):dtype=dtype.value
        if isinstance(dtype,int):dtype=bytes([dtype])
        values=list(values)
        if dtype==BinaryVectorDtype.FLOAT32:
            if padding:raise ValueError('FLOAT32 padding must be zero')
            body=struct.pack('<'+'f'*len(values),*values)
        elif dtype==BinaryVectorDtype.INT8:
            if padding:raise ValueError('INT8 padding must be zero')
            body=struct.pack('<'+'b'*len(values),*values)
        elif dtype==BinaryVectorDtype.PACKED_BIT:
            if not 0<=padding<=7:raise ValueError('Invalid bit padding')
            body=bytes(values)
        else:raise ValueError('Unsupported binary vector dtype')
        return cls(dtype+bytes([padding])+body,9)
    def as_vector(self):
        if self.subtype!=9 or len(self)<2:raise ValueError('Not a binary vector')
        dtype,padding=self[:1],self[1]
        if dtype==BinaryVectorDtype.FLOAT32:
            if padding or (len(self)-2)%4:raise ValueError('Invalid FLOAT32 vector')
            data=list(struct.unpack('<'+'f'*((len(self)-2)//4),self[2:]))
        elif dtype==BinaryVectorDtype.INT8:
            if padding:raise ValueError('Invalid INT8 vector')
            data=list(struct.unpack('<'+'b'*(len(self)-2),self[2:]))
        elif dtype==BinaryVectorDtype.PACKED_BIT:data=list(self[2:])
        else:raise ValueError('Unsupported binary vector dtype')
        return SimpleNamespace(data=data,dtype=dtype,padding=padding)

@dataclass(frozen=True)
class Timestamp:
    time:int
    inc:int
@dataclass(frozen=True)
class Decimal128:
    value:str
    def __str__(self):return self.value
    def to_decimal(self):
        from decimal import Decimal
        return Decimal(self.value)
@dataclass(frozen=True)
class Regex:
    pattern:str
    flags:int=0
