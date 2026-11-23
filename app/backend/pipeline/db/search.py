"""Native FTS5 candidate search plus exact residual predicates.

The trigram index accelerates literal substrings without replacing the public
case-insensitive substring/regex contract with token-only matching.
"""
import re
from .query import Query, fold


def literal_pattern(pattern):
    if not isinstance(pattern,str):return None
    out=[];i=0
    while i<len(pattern):
        char=pattern[i]
        if char=='\\':
            i+=1
            if i>=len(pattern) or pattern[i].isalnum():return None
            out.append(pattern[i])
        elif char in '.^$*+?{}[]()|':return None
        else:out.append(char)
        i+=1
    return ''.join(out)


def candidate(query,name,pattern,*,key=None):
    text=literal_pattern(pattern)
    if text is None or len(text)<3 or not text.isascii():return None
    query.collection.ensure_fts_path(name)
    quoted='"'+fold(text).replace('"','""')+'"'
    return (key or query.key)+' IN (SELECT doc_key FROM '+query.collection._fts+' WHERE '+query.collection._fts+' MATCH '+query.bind(quoted)+' AND path='+query.bind(name)+')'


def compile_search(collection,spec,doc='d.payload',key='d.id_key'):
    q=Query(collection,doc,key)
    def one(body):
        operators=[k for k in body if k not in ('index','sort','count','returnStoredSource','highlight','scoreDetails')]
        if len(operators)!=1:raise ValueError('Search requires one operator')
        op=operators[0];item=body[op]
        if op=='compound':
            parts=[]
            for group in ('must','filter','mustNot'):
                for term in item.get(group,[]):parts.append(('NOT ('+one(term)+')') if group=='mustNot' else one(term))
            should=item.get('should',[])
            minimum=item.get('minimumShouldMatch',0 if parts else 1)
            if should and minimum:
                terms=[one(term) for term in should]
                parts.append('('+'+'.join('(CASE WHEN '+term+' THEN 1 ELSE 0 END)' for term in terms)+'>='+str(int(minimum))+')')
            return '('+' AND '.join(parts)+')' if parts else '1'
        if op in ('equals','in','exists','range'):
            path=item['path']
            condition={'equals':lambda:{'$eq':item['value']},'in':lambda:{'$in':item['value']},'exists':lambda:{'$exists':True},'range':lambda:{'$'+k:v for k,v in item.items() if k in ('gt','gte','lt','lte')}}[op]()
            return q.condition(path,condition)
        if op not in ('text','autocomplete','wildcard','regex'):raise NotImplementedError('Unsupported local search operator '+op)
        paths=item['path'];paths=[paths] if isinstance(paths,str) else paths
        if not isinstance(paths,list):raise NotImplementedError('Search paths must be explicit')
        terms=item['query'];terms=terms if isinstance(terms,list) else [terms]
        results=[]
        for path in paths:
            for term in terms:
                if op=='wildcard':pattern=''.join('.*' if ch=='*' else '.' if ch=='?' else re.escape(ch) for ch in term)
                elif op=='regex':pattern=term
                elif op=='autocomplete':pattern=r'(?<!\w)'+re.escape(term)
                else:pattern=re.escape(term)
                results.append(q.condition(path,{'$regex':pattern,'$options':'i'}))
        return '('+' OR '.join(results)+')' if results else '0'
    where=one(spec)
    return where,q.params,spec.get('sort',{})
