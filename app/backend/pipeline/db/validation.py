"""Declared document validation, enforced before every persisted mutation."""
import datetime
from decimal import Decimal, InvalidOperation
import re
from .operations import OperationFailure
from .types import ObjectId, Binary, Decimal128, Timestamp

_CONSTRAINTS = frozenset({'bsonType','required','properties','enum','items','additionalProperties',
                          'minimum','maximum','minLength','maxLength','pattern','description','title'})
_TYPES = frozenset({'object','array','string','int','long','double','number','bool','date',
                    'null','objectId','binData','decimal','timestamp'})


def _number(value):
    """Exact numeric value, without treating Python bool as BSON numeric data."""
    if isinstance(value,bool):return None
    if isinstance(value,int):
        return Decimal(int(value)) if -(2**63)<=value<2**63 else None
    if isinstance(value,float):return Decimal.from_float(value)
    if type(value).__name__=='Decimal128':return value.to_decimal()
    return None


def bson_equal(left,right):
    """Enum equality uses BSON types, with numeric types compared by value."""
    a,b=_number(left),_number(right)
    if a is not None or b is not None:
        if a is None or b is None:return False
        if a.is_nan() or b.is_nan():return a.is_nan() and b.is_nan()
        return a==b
    if isinstance(left,bool) or isinstance(right,bool):
        return isinstance(left,bool) and isinstance(right,bool) and left==right
    if type(left).__name__=='ObjectId' or type(right).__name__=='ObjectId':
        return type(left).__name__==type(right).__name__=='ObjectId' and str(left)==str(right)
    if isinstance(left,(bytes,bytearray)) or isinstance(right,(bytes,bytearray)):
        return (isinstance(left,(bytes,bytearray)) and isinstance(right,(bytes,bytearray))
                and getattr(left,'subtype',0)==getattr(right,'subtype',0) and bytes(left)==bytes(right))
    if isinstance(left,dict) or isinstance(right,dict):
        return (isinstance(left,dict) and isinstance(right,dict) and list(left)==list(right)
                and all(bson_equal(left[key],right[key]) for key in left))
    if isinstance(left,(list,tuple)) or isinstance(right,(list,tuple)):
        return (isinstance(left,(list,tuple)) and isinstance(right,(list,tuple)) and len(left)==len(right)
                and all(bson_equal(a,b) for a,b in zip(left,right)))
    if isinstance(left,datetime.datetime) or isinstance(right,datetime.datetime):
        from .codec import scalar
        return isinstance(left,datetime.datetime) and isinstance(right,datetime.datetime) and scalar(left)==scalar(right)
    if left is None or right is None:return left is None and right is None
    if isinstance(left,str) or isinstance(right,str):return isinstance(left,str) and isinstance(right,str) and left==right
    from .codec import canonical_bytes
    return canonical_bytes(left)==canonical_bytes(right)


def validate_schema(validator):
    """Check every supported rule before installing or evaluating a schema."""
    def invalid(detail):raise OperationFailure('Unsupported or malformed schema: '+detail)
    if not isinstance(validator,dict):invalid('validator must be a document')
    if not validator:return
    if set(validator)!={'$jsonSchema'}:raise OperationFailure('Unsupported document validator')
    def check(spec):
        if not isinstance(spec,dict):invalid('schema must be a document')
        unknown=set(spec)-_CONSTRAINTS
        if unknown:invalid('constraints '+', '.join(sorted(str(key) for key in unknown)))
        if 'bsonType' in spec:
            kind=spec['bsonType'];kinds=kind if isinstance(kind,list) else [kind]
            if not kinds or any(not isinstance(k,str) or k not in _TYPES for k in kinds):invalid('bsonType')
            if len(set(kinds))!=len(kinds):invalid('duplicate bsonType')
        if 'required' in spec:
            names=spec['required']
            if not isinstance(names,list) or any(not isinstance(k,str) for k in names):invalid('required')
            if len(set(names))!=len(names):invalid('duplicate required field')
        if 'properties' in spec:
            props=spec['properties']
            if not isinstance(props,dict) or any(not isinstance(k,str) for k in props):invalid('properties')
            for sub in props.values():check(sub)
        if 'items' in spec:check(spec['items'])
        if 'additionalProperties' in spec and not isinstance(spec['additionalProperties'],bool):invalid('additionalProperties schema is not supported')
        if 'enum' in spec:
            from .codec import encode_document
            if not isinstance(spec['enum'],list) or not spec['enum']:invalid('enum must be a nonempty array')
            try:encode_document(spec['enum'])
            except (TypeError,ValueError,OverflowError) as exc:invalid('enum value type ('+type(exc).__name__+')')
        for key in ('minimum','maximum'):
            if key in spec:
                try:value=_number(spec[key])
                except (InvalidOperation,ValueError,TypeError):invalid(key)
                if value is None or value.is_nan():invalid(key)
        for key in ('minLength','maxLength'):
            if key in spec and (not isinstance(spec[key],int) or isinstance(spec[key],bool) or spec[key]<0):invalid(key)
        for key in ('description','title','pattern'):
            if key in spec and not isinstance(spec[key],str):invalid(key)
        if 'pattern' in spec:
            try:re.compile(spec['pattern'])
            except re.error:invalid('pattern')
    check(validator['$jsonSchema'])


def validate_write(document, validator, options, old=None):
    """Moderate rules exempt only updates to documents already invalid."""
    from .collection_options import check_options
    check_options(options)
    if not validator or options.get('validationLevel') == 'off':return
    if options.get('validationLevel') == 'moderate' and old is not None:
        try:validate(old,validator)
        except OperationFailure as exc:
            if exc.code == 121:return
            raise
    validate(document,validator)


def validate(document, validator):
    validate_schema(validator)
    if not validator:return
    def fail(path,detail):raise OperationFailure('Document validation failed at '+path+': '+detail,121)
    def check(value,spec,path):
        kind=spec.get('bsonType')
        kinds=kind if isinstance(kind,list) else [kind] if kind else []
        def matches(k):
            if k=='object':return isinstance(value,dict)
            if k=='array':return isinstance(value,list)
            if k=='string':return isinstance(value,str) and type(value).__name__!='ObjectId'
            if k=='int':return isinstance(value,int) and not isinstance(value,bool) and type(value).__name__!='Int64' and -(2**31)<=value<2**31
            if k=='long':return (isinstance(value,int) and not isinstance(value,bool) and -(2**63)<=value<2**63
                                 and (type(value).__name__=='Int64' or not -(2**31)<=value<2**31))
            if k=='double':return isinstance(value,float)
            if k=='number':return _number(value) is not None
            if k=='bool':return isinstance(value,bool)
            if k=='date':return isinstance(value,datetime.datetime)
            if k=='null':return value is None
            if k=='objectId':return isinstance(value,ObjectId) or type(value).__name__=='ObjectId'
            if k=='binData':return isinstance(value,(bytes,bytearray,Binary))
            if k=='decimal':return isinstance(value,Decimal128) or type(value).__name__=='Decimal128'
            if k=='timestamp':return isinstance(value,Timestamp) or type(value).__name__=='Timestamp'
            raise OperationFailure('Unsupported schema type '+str(k))
        if kinds and not any(matches(k) for k in kinds):fail(path,'expected '+str(kind))
        if 'enum' in spec and not any(bson_equal(value,allowed) for allowed in spec['enum']):fail(path,'value outside enum')
        if isinstance(value,dict):
            for key in spec.get('required',[]):
                if key not in value:fail(path+'.'+key,'required field missing')
            props=spec.get('properties',{})
            if spec.get('additionalProperties') is False and set(value)-set(props):fail(path,'unexpected properties')
            for key,sub in props.items():
                if key in value:check(value[key],sub,path+'.'+key)
        if isinstance(value,list) and 'items' in spec:
            for i,item in enumerate(value):check(item,spec['items'],path+'['+str(i)+']')
        if isinstance(value,str) and type(value).__name__!='ObjectId':
            if 'minLength' in spec and len(value)<spec['minLength']:fail(path,'too short')
            if 'maxLength' in spec and len(value)>spec['maxLength']:fail(path,'too long')
            if 'pattern' in spec and re.search(spec['pattern'],value) is None:fail(path,'pattern mismatch')
        number=_number(value)
        if number is not None and ('minimum' in spec or 'maximum' in spec):
            if number.is_nan():fail(path,'not a comparable number')
            if 'minimum' in spec and number<_number(spec['minimum']):fail(path,'below minimum')
            if 'maximum' in spec and number>_number(spec['maximum']):fail(path,'above maximum')
    check(document,validator['$jsonSchema'],'$')
