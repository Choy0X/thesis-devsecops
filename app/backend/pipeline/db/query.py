"""Parameterised SQL filters. Indexed multikey values retain missing/null semantics."""
import json
import datetime
import math
import re
from .codec import scalar

MISSING=object()

def path_parts(path):
    if not isinstance(path,str) or not path or '\x00' in path:raise ValueError('Invalid document field')
    return path.split('.')

def json_path(path):
    return '$'+''.join('['+p+']' if p.isdigit() else '.'+json.dumps(p,ensure_ascii=True) for p in path_parts(path))

def literal(value):return "'"+str(value).replace("'","''")+"'"

def field(doc,path):return 'json_extract('+doc+','+literal(json_path(path))+')'
def field_type(doc,path):return 'json_type('+doc+','+literal(json_path(path))+')'

def values(document,path):
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
    return list(walk(document,0,''))

def value_key(value):
    from .codec import encode_document
    name=type(value).__name__
    if isinstance(value,datetime.datetime):return 5,scalar(value)
    if name=='ObjectId':return 6,str(value)
    if isinstance(value,bytes):return 7,json.dumps([getattr(value,'subtype',0),scalar(value)],separators=(',',':'))
    if name in ('Decimal128','Timestamp','Regex'):
        payload,marks=encode_document(value)
        return {'Decimal128':8,'Timestamp':9,'Regex':10}[name],json.dumps([json.loads(payload),json.loads(marks)],separators=(',',':'))
    if value is None:return 0,None
    if isinstance(value,bool):return 1,int(value)
    if isinstance(value,(int,float)):return 2,int(value) if isinstance(value,int) else value
    if isinstance(value,str):return 3,value
    payload,marks=encode_document(value)
    # Numeric representation and timezone do not change equality inside arrays.
    tags=[]
    for path,kind,extra in json.loads(marks):
        if kind=='int64':continue
        if kind=='datetime':extra=0
        if kind=='bytes':kind,extra='binary',0
        tags.append([path,kind,extra])
    return 4,json.dumps([json.loads(payload),tags],ensure_ascii=False,separators=(',',':'))


def get(document,path,default=None):
    value=document
    for part in path_parts(path):
        if isinstance(value,dict):
            if part not in value:return default
            value=value[part]
        elif isinstance(value,list) and part.isdigit() and int(part)<len(value):value=value[int(part)]
        else:return default
    return value

def put(document,path,value):
    parts=path_parts(path);current=document
    for i,key in enumerate(parts[:-1]):
        if isinstance(current,list):
            if not key.isdigit():raise ValueError('Array update needs numeric position')
            j=int(key)
            while len(current)<=j:current.append(None)
            if current[j] is None:current[j]={}
            current=current[j]
        else:
            if key not in current:current[key]={}
            current=current[key]
    key=parts[-1]
    if isinstance(current,list):
        if not key.isdigit():raise ValueError('Array update needs numeric position')
        j=int(key)
        while len(current)<=j:current.append(None)
        current[j]=value
    else:current[key]=value

def remove(document,path):
    parts=path_parts(path);parent=document
    for part in parts[:-1]:
        if isinstance(parent,dict):parent=parent.get(part,MISSING)
        elif isinstance(parent,list) and part.isdigit() and int(part)<len(parent):parent=parent[int(part)]
        else:return
        if parent is MISSING:return
    key=parts[-1]
    if isinstance(parent,dict):parent.pop(key,None)
    elif isinstance(parent,list) and key.isdigit() and int(key)<len(parent):parent[int(key)]=None


def fold(value):return value.translate(str.maketrans({'\u0130':'i','\u0131':'i','\u017f':'s','\u212a':'k'})).casefold()


def single_equality(query):
    """A complete predicate representable by one typed lookup range.

    Null also matches absent fields, so it must retain the general compiler.
    Identity predicates use the document primary key directly.
    """
    if not isinstance(query,dict) or len(query)!=1:return None
    name,value=next(iter(query.items()))
    # The list API wraps even one filter in a positive conjunction. Only an
    # exact one-child wrapper is equivalent to its entire child predicate.
    if name=='$and':
        if not isinstance(value,(list,tuple)) or len(value)!=1:return None
        return single_equality(value[0])
    if not isinstance(name,str) or name.startswith('$') or name=='_id':return None
    if isinstance(value,dict) and any(str(k).startswith('$') for k in value):
        if set(value)!={'$eq'}:return None
        value=value['$eq']
    if value is None:return None
    path_parts(name)
    kind,encoded=value_key(value)
    return name,kind,encoded


def membership_anchor(query):
    """A conservative candidate driver for a compound field conjunction.

    Residual predicates retain the general compiler. Null, containers and richer
    persistent types are excluded from the driver rather than approximated.
    """
    if not isinstance(query,dict) or len(query)<2:return None
    if any(not isinstance(name,str) or name.startswith('$') for name in query):return None
    selected=None
    for name,condition in query.items():
        if not isinstance(condition,dict) or set(condition)!={'$in'}:continue
        items=condition['$in']
        if not isinstance(items,(list,tuple)) or not items:continue
        eligible=True
        for value in items:
            kind,_=value_key(value)
            if kind not in (1,2,3):eligible=False;break
            if kind==2 and (isinstance(value,int) and not -(1<<63)<=value<(1<<63)
                    or isinstance(value,float) and not math.isfinite(value)):
                eligible=False;break
        if eligible and (selected is None or len(items)<len(selected[1]['$in'])):
            selected=name,condition
    return selected


class Query:
    def __init__(self,collection=None,doc='d.payload',key='d.id_key',*,indexed=True,variables=None,correlated=False):
        self.collection,self.doc,self.key=collection,doc,key
        self.indexed=indexed and collection is not None
        self.correlated=correlated
        self.params=[];self.variables=variables or {}
    def bind(self,value):
        self.params.append(json.dumps(value,separators=(',',':')) if isinstance(value,(list,dict)) else scalar(value));return '?'
    def expr(self,value):
        if isinstance(value,str) and value.startswith('$$'):
            if value[2:] not in self.variables:raise ValueError('Unknown SQL pipeline variable')
            return self.variables[value[2:]]
        if isinstance(value,str) and value.startswith('$'):return field(self.doc,value[1:])
        if not isinstance(value,dict):return self.bind(value)
        if len(value)!=1:raise NotImplementedError('Unsupported expression object')
        op,body=next(iter(value.items()))
        if op=='$literal':return self.bind(body)
        if op=='$ifNull':return 'coalesce('+','.join(self.expr(x) for x in body)+')'
        if op=='$toUpper':return 'upper('+self.expr(body)+')'
        if op=='$arrayElemAt':return 'json_extract('+self.expr(body[0])+", '$[' || "+self.expr(body[1])+" || ']')"
        if op=='$cond':
            if isinstance(body,dict):body=[body['if'],body['then'],body['else']]
            return '(CASE WHEN '+self.expr(body[0])+' THEN '+self.expr(body[1])+' ELSE '+self.expr(body[2])+' END)'
        if op in ('$and','$or'):return '('+(' AND ' if op=='$and' else ' OR ').join(self.expr(x) for x in body)+')'
        if op in ('$eq','$ne','$gt','$gte','$lt','$lte'):
            a,b=body;operator={'$eq':'IS','$ne':'IS NOT','$gt':'>','$gte':'>=','$lt':'<','$lte':'<='}[op]
            return '('+self.expr(a)+' '+operator+' '+self.expr(b)+')'
        if op=='$in':return '('+self.expr(body[0])+' IN (SELECT value FROM json_each('+self.expr(body[1])+')))'
        raise NotImplementedError('Unsupported expression: '+op)
    def compile(self,query):
        if not query:return '1'
        clauses=[]
        for name,condition in query.items():
            if name in ('$and','$or','$nor'):
                terms=[self.compile(part) for part in condition]
                result='('+(' AND ' if name=='$and' else ' OR ').join(terms)+')' if terms else ('1' if name!='$or' else '0')
                clauses.append('NOT '+result if name=='$nor' else result)
            elif name=='$expr':clauses.append(self.expr(condition))
            elif name.startswith('$'):raise NotImplementedError('Unsupported query operator '+name)
            else:clauses.append(self.condition(name,condition))
        return '('+' AND '.join(clauses)+')'
    def _lookup_membership(self,predicate,source=None):
        source=source or self.collection._values+' v'
        if self.correlated:
            return 'EXISTS (SELECT 1 FROM '+source+' WHERE v.doc_key='+self.key+' AND '+predicate+')'
        return self.key+' IN (SELECT v.doc_key FROM '+source+' WHERE '+predicate+')'
    def matches(self,name,operator,value,options=''):
        kind,encoded=value_key(value)
        if self.indexed and name=='_id' and operator in ('=','exists'):
            if operator=='exists':return '1'
            from .codec import id_key
            return self.key+'='+self.bind(id_key(value))
        if self.indexed:
            self.collection.ensure_path(name)
            table=self.collection._values
            if operator=='regex':
                predicate='v.path='+self.bind(name)
                from .search import candidate
                indexed=candidate(self,name,value,key='v.doc_key') if 'x' not in str(options) else None
                if indexed:predicate+=' AND '+indexed
                predicate+=' AND v.kind=3 AND rx('+self.bind(value)+',v.value,'+self.bind(options)+')'
                # The doc-key index makes FTS candidates drive residual checks;
                # otherwise SQLite can scan every regex value before intersecting.
                source=table+' v'+(' INDEXED BY '+table+'_doc' if indexed else '')
                return self._lookup_membership(predicate,source)
            predicate='v.path='+self.bind(name)
            if operator=='exists':return self._lookup_membership(predicate)
            predicate+=' AND v.kind='+self.bind(kind)
            if operator=='regex':
                predicate='v.path='+self.bind(name)+' AND v.kind=3 AND rx('+self.bind(value)+',v.value,'+self.bind(options)+')'
            elif value is None:predicate+=' AND v.value IS NULL'
            else:predicate+=' AND v.value '+operator+' '+self.bind(encoded)
            return self._lookup_membership(predicate)
        if kind==4:
            from .codec import encode_document
            encoded=encode_document(value)[0]
        elif kind==7:encoded=scalar(value)
        elif kind in (8,9,10):
            from .codec import encode_document
            encoded=encode_document(value)[0]
        expr=field(self.doc,name);typ=field_type(self.doc,name)
        if operator=='exists':return typ+' IS NOT NULL'
        if operator=='regex':return 'rx('+self.bind(value)+','+expr+','+self.bind(options)+')'
        if value is None:return expr+' IS NULL'
        equality=expr+' '+operator+' '+self.bind(encoded)
        # Scalar equality also matches a member of a terminal array.
        return '('+equality+' OR ('+typ+"='array' AND EXISTS (SELECT 1 FROM json_each("+expr+') je WHERE je.value '+operator+' '+self.bind(encoded)+')))'
    def condition(self,name,condition):
        if not isinstance(condition,dict) or not any(str(k).startswith('$') for k in condition):condition={'$eq':condition}
        clauses=[]
        for operator,value in condition.items():
            if operator=='$options':continue
            if operator=='$exists':
                expr=self.matches(name,'exists',None);clauses.append(expr if value else 'NOT ('+expr+')')
            elif operator in ('$eq','$ne'):
                expr=self.matches(name,'=',value)
                if value is None:
                    exists=self.matches(name,'exists',None)
                    expr='('+expr+' OR NOT ('+exists+'))' if operator=='$eq' else '(NOT ('+expr+') AND '+exists+')'
                    clauses.append(expr)
                else:clauses.append('NOT ('+expr+')' if operator=='$ne' else expr)
            elif operator in ('$in','$nin'):
                if not isinstance(value,(list,tuple)):raise ValueError('Membership requires an array')
                if self.indexed and name=='_id':
                    from .codec import id_key
                    expr=self.key+' IN (SELECT value FROM json_each('+self.bind([id_key(item) for item in value])+'))'
                    clauses.append('NOT ('+expr+')' if operator=='$nin' else expr)
                    continue
                if self.indexed:
                    self.collection.ensure_path(name)
                    grouped={};parts=[];has_null=False
                    for item in value:
                        kind,encoded=value_key(item)
                        if item is None:has_null=True
                        else:grouped.setdefault(kind,[]).append(encoded)
                    for kind,items in grouped.items():
                        parts.append(self._lookup_membership('v.path='+self.bind(name)+' AND v.kind='+self.bind(kind)+' AND v.value IN (SELECT value FROM json_each('+self.bind(items)+'))'))
                    if has_null:parts.append(self.condition(name,{'$eq':None}))
                else:parts=[self.condition(name,{'$eq':v}) for v in value]
                expr='('+' OR '.join(parts)+')' if parts else '0'
                clauses.append('NOT ('+expr+')' if operator=='$nin' else expr)
            elif operator in ('$lt','$lte','$gt','$gte'):
                clauses.append(self.matches(name,{'$lt':'<','$lte':'<=','$gt':'>','$gte':'>='}[operator],value))
            elif operator=='$regex':clauses.append(self.matches(name,'regex',value,condition.get('$options','')))
            elif operator=='$not':clauses.append('NOT ('+self.condition(name,value)+')')
            elif operator=='$elemMatch':
                expr=field(self.doc,name);alias='elem'+str(len(self.params))
                inner=Query(None,alias+'.value',indexed=False)
                if any(k.startswith('$') for k in value):
                    # Scalar array element: evaluate its operators as a temporary value field.
                    inner.doc="json_object('value',"+alias+'.value)'
                    clause=inner.condition('value',value)
                else:clause=inner.compile(value)
                self.params.extend(inner.params)
                clauses.append('EXISTS (SELECT 1 FROM json_each('+expr+') '+alias+' WHERE '+clause+')')
            elif operator=='$size':clauses.append('json_array_length('+field(self.doc,name)+')='+self.bind(value))
            elif operator=='$all':clauses.extend(self.condition(name,{'$eq':item}) for item in value)
            elif operator=='$type':
                names={'string':'text','bool':'true','object':'object','array':'array','int':'integer','long':'integer','double':'real','null':'null'}
                clauses.append(field_type(self.doc,name)+'='+self.bind(names.get(value,value)))
            else:raise NotImplementedError('Unsupported field operator '+operator)
        return '('+' AND '.join(clauses)+')'


def partial_predicate(query,doc='payload'):
    """Scalar JSON expression for declared partial indexes (no SQL subqueries).

    The schema's partial fields are scalar. json_type guards preserve bool and
    numeric distinctions; unsupported filters fail before an index is recorded.
    """
    clauses=[]
    for name,condition in query.items():
        if name in ('$and','$or'):
            clauses.append('('+(' AND ' if name=='$and' else ' OR ').join(partial_predicate(x,doc) for x in condition)+')');continue
        if name.startswith('$'):raise NotImplementedError('Unsupported partial index condition '+name)
        if not isinstance(condition,dict):condition={'$eq':condition}
        expr=field(doc,name);typ=field_type(doc,name)
        for op,value in condition.items():
            if op=='$exists':clauses.append(typ+(' IS NOT NULL' if value else ' IS NULL'));continue
            if op not in ('$eq','$gt','$gte','$lt','$lte'):raise NotImplementedError('Unsupported partial index predicate '+op)
            kind,encoded=value_key(value)
            if kind==4:raise NotImplementedError('Container partial index constants are unsupported')
            bracket={0:typ+"='null'",1:typ+" IN ('true','false')",2:typ+" IN ('integer','real')",3:typ+"='text'"}[kind]
            operator={'$eq':'=','$gt':'>','$gte':'>=','$lt':'<','$lte':'<='}[op]
            encoded_sql='NULL' if encoded is None else literal(encoded) if isinstance(encoded,str) else str(encoded)
            if encoded is None:clauses.append('('+expr+' IS NULL)')
            else:clauses.append('('+bracket+' AND '+expr+' '+operator+' '+encoded_sql+')')
    return '('+' AND '.join(clauses)+')' if clauses else '1'
