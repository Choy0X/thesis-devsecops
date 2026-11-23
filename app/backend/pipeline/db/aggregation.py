"""Compile repository aggregation pipelines into bounded SQLite queries.

Selection, joins, grouping, sorting and pagination execute in SQL. Python only
decodes returned rows. Unsupported stages and expressions fail explicitly.
"""
from __future__ import annotations

import json

from . import codec
from .query import Query, field, field_type, json_path, literal, path_parts, single_equality


def _json_at(document, path):
    value = field(document, path)
    typ = field_type(document, path)
    return "(CASE " + typ + " WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE json_quote(" + value + ") END)"


def _concat_paths(prefix, suffix):
    return "json(substr(" + prefix + ",1,length(" + prefix + ")-1) || CASE WHEN json_array_length(" + suffix + ")>0 THEN ',' || substr(" + suffix + ",2) ELSE ']' END)"


class Compiler:
    def __init__(self, collection):
        self.collection = collection
        self.parameters = {}
        self.serial = 0
        self.metadata_version = None
        self.metadata_fast_used = False

    def alias(self):
        self.serial += 1
        return "a" + str(self.serial)

    def bind(self, value):
        name = "p" + str(len(self.parameters))
        self.parameters[name] = value
        return ":" + name

    def absorb(self, sql, parameters):
        """Name Query's positional parameters, respecting quoted SQL literals."""
        result = []
        quoted = False
        at = 0
        parameters = iter(parameters)
        while at < len(sql):
            char = sql[at]
            if char == "'":
                result.append(char)
                if quoted and at + 1 < len(sql) and sql[at + 1] == "'":
                    result.append("'")
                    at += 2
                    continue
                quoted = not quoted
            elif char == "?" and not quoted:
                result.append(self.bind(next(parameters)))
            else:
                result.append(char)
            at += 1
        try:
            next(parameters)
        except StopIteration:
            return "".join(result)
        raise ValueError("Aggregation query parameter mismatch")

    def jexpr(self, value, doc, variables):
        """An expression represented as valid JSON, preserving booleans/arrays."""
        if isinstance(value, str) and value.startswith("$$"):
            name, _, tail = value[2:].partition(".")
            if name in ("ROOT", "CURRENT"):
                result = "json(" + doc + ")"
            elif name in variables:
                result = variables[name]
            else:
                raise ValueError("Unknown aggregation variable " + name)
            return _json_at(result, tail) if tail else result
        if isinstance(value, str) and value.startswith("$"):
            return _json_at(doc, value[1:])
        if isinstance(value, list):
            return "json_array(" + ",".join("json(" + self.jexpr(x, doc, variables) + ")" for x in value) + ")"
        if not isinstance(value, dict):
            return self.bind(json.dumps(codec.scalar(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        if not any(str(key).startswith("$") for key in value):
            return "json_object(" + ",".join(literal(key) + ",json(" + self.jexpr(item, doc, variables) + ")" for key, item in value.items()) + ")"
        if len(value) != 1:
            raise NotImplementedError("Unsupported aggregation expression object")
        op, body = next(iter(value.items()))
        if op == "$literal":
            return self.bind(json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        if op == "$ifNull":
            if not isinstance(body, list) or len(body) < 2:
                raise ValueError("$ifNull requires at least two expressions")
            args = [self.jexpr(item, doc, variables) for item in body]
            return "coalesce(" + ",".join("nullif(" + item + ",'null')" for item in args[:-1]) + "," + args[-1] + ")"
        if op == "$cond":
            if isinstance(body, dict):
                body = [body["if"], body["then"], body["else"]]
            return "(CASE WHEN " + self.expr(body[0], doc, variables) + " THEN " + self.jexpr(body[1], doc, variables) + " ELSE " + self.jexpr(body[2], doc, variables) + " END)"
        if op == "$arrayElemAt":
            source = self.jexpr(body[0], doc, variables)
            index = self.expr(body[1], doc, variables)
            path = "('$[' || CASE WHEN " + index + "<0 THEN '#' || " + index + " ELSE " + index + " END || ']')"
            item = "json_extract(" + source + "," + path + ")"
            typ = "json_type(" + source + "," + path + ")"
            return "(CASE " + typ + " WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE json_quote(" + item + ") END)"
        if op in ("$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$and", "$or", "$not", "$in"):
            return "(CASE WHEN " + self.operator(op, body, doc, variables) + " THEN 'true' ELSE 'false' END)"
        if op == "$toUpper":
            return "json_quote(upper(coalesce(" + self.expr(body, doc, variables) + ",'')))"
        if op == "$toLower":
            return "json_quote(lower(coalesce(" + self.expr(body, doc, variables) + ",'')))"
        if op == "$size":
            return "json_quote(json_array_length(" + self.jexpr(body, doc, variables) + "))"
        if op in ("$add", "$multiply", "$subtract", "$divide"):
            operator = {"$add": "+", "$multiply": "*", "$subtract": "-", "$divide": "/"}[op]
            return "json_quote((" + operator.join(self.expr(item, doc, variables) for item in body) + "))"
        raise NotImplementedError("Unsupported aggregation expression " + op)

    def expr(self, value, doc, variables):
        return "json_extract(" + self.jexpr(value, doc, variables) + ",'$')"

    def operator(self, op, body, doc, variables):
        if op in ("$and", "$or"):
            terms = [self.expr(item, doc, variables) for item in body]
            return "(" + (" AND " if op == "$and" else " OR ").join(terms) + ")" if terms else ("1" if op == "$and" else "0")
        if op == "$not":
            return "NOT (" + self.expr(body[0], doc, variables) + ")"
        if op == "$in":
            return "(" + self.expr(body[0], doc, variables) + " IN (SELECT value FROM json_each(" + self.jexpr(body[1], doc, variables) + ")))"
        operator = {"$eq": "IS", "$ne": "IS NOT", "$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}[op]
        return "(" + self.expr(body[0], doc, variables) + " " + operator + " " + self.expr(body[1], doc, variables) + ")"

    def indexed_equal(self, collection, alias, path, other):
        """Use the multikey index for correlated joins, including local arrays."""
        collection.ensure_path(path)
        v, item = self.alias(), self.alias()
        wanted = self.bind(path)
        kind = "CASE json_type(" + other + ",'$') WHEN 'null' THEN 0 WHEN 'true' THEN 1 WHEN 'false' THEN 1 WHEN 'integer' THEN 2 WHEN 'real' THEN 2 WHEN 'text' THEN 3 ELSE 4 END"
        scalar = "json_extract(" + other + ",'$')"
        predicate = v + ".path=" + wanted + " AND ((" + v + ".kind=" + kind + " AND " + v + ".value IS " + scalar + ") OR (json_type(" + other + ",'$')='array' AND EXISTS (SELECT 1 FROM json_each(" + other + ") " + item + " WHERE " + v + ".value IS " + item + ".value)))"
        found = alias + ".id_key IN (SELECT " + v + ".doc_key FROM " + collection._values + " " + v + " WHERE " + predicate + ")"
        missing = "(json_type(" + other + ",'$')='null' AND " + alias + ".id_key NOT IN (SELECT doc_key FROM " + collection._values + " WHERE path=" + wanted + "))"
        return "(" + found + " OR " + missing + ")"

    def expression_match(self, condition, collection, alias, indexed, variables):
        if isinstance(condition, dict) and len(condition) == 1:
            op, body = next(iter(condition.items()))
            if op in ("$and", "$or"):
                parts = [self.expression_match(item, collection, alias, indexed, variables) for item in body]
                return "(" + (" AND " if op == "$and" else " OR ").join(parts) + ")" if parts else ("1" if op == "$and" else "0")
            if indexed and op == "$eq":
                for left, right in (body, list(reversed(body))):
                    if isinstance(left, str) and left.startswith("$") and not left.startswith("$$") and not (isinstance(right, str) and right.startswith("$") and not right.startswith("$$")):
                        return self.indexed_equal(collection, alias, left[1:], self.jexpr(right, alias + ".payload", variables))
        return self.expr(condition, alias + ".payload", variables)

    def match(self, condition, collection, alias, indexed, variables):
        terms = []
        for key, value in condition.items():
            if key == "$expr":
                terms.append(self.expression_match(value, collection, alias, indexed, variables))
            elif key in ("$and", "$or", "$nor"):
                parts = [self.match(item, collection, alias, indexed, variables) for item in value]
                result = "(" + (" AND " if key == "$and" else " OR ").join(parts) + ")" if parts else ("1" if key != "$or" else "0")
                terms.append("NOT " + result if key == "$nor" else result)
            else:
                q = Query(collection, doc=alias + ".payload", key=alias + ".id_key", indexed=indexed, variables={name: "json_extract(" + expr + ",'$')" for name, expr in variables.items()})
                terms.append(self.absorb(q.compile({key: value}), q.params))
        return "(" + " AND ".join(terms) + ")" if terms else "1"

    def nested_marks(self, source, prefix):
        """Prefix result-side type paths for lookup/facet arrays in SQL."""
        row, mark = self.alias(), self.alias()
        positions = "SELECT *,row_number() OVER (ORDER BY ord)-1 AS position FROM (" + source + ")"
        path = "json_extract(" + mark + ".value,'$[0]')"
        pref = "json_array(" + literal(prefix) + "," + row + ".position)"
        return "SELECT json_array(" + _concat_paths(pref, path) + ",json_extract(" + mark + ".value,'$[1]'),json_extract(" + mark + ".value,'$[2]')) AS mark FROM (" + positions + ") " + row + " CROSS JOIN json_each(" + row + ".types) " + mark

    def sort(self, source, spec):
        alias = self.alias()
        pairs = spec.items() if isinstance(spec, dict) else spec
        terms = []
        for key, direction in pairs:
            if direction not in (1, -1):
                raise NotImplementedError("Unsupported aggregation sort direction")
            terms.append(field(alias + ".payload", key) + (" ASC" if direction == 1 else " DESC"))
        terms.append(alias + ".ord")
        order = ",".join(terms)
        return "SELECT " + alias + ".payload," + alias + ".types," + alias + ".id_key,row_number() OVER (ORDER BY " + order + ") AS ord FROM (" + source + ") " + alias

    def pipeline(self, pipeline, collection=None, source=None, original=True, variables=None):
        collection = collection or self.collection
        variables = variables or {}
        whole_collection = source is None and original
        pipeline = list(pipeline)
        if source is None:
            source = "SELECT payload,types,id_key,rowid AS ord FROM " + collection._table
        if whole_collection:
            at = 0
            predicate = None
            while at < len(pipeline) and isinstance(pipeline[at], dict) and set(pipeline[at]) == {"$match"}:
                condition = pipeline[at]["$match"]
                if condition == {}:
                    at += 1
                elif predicate is None and self.ordinary_match(condition):
                    predicate = condition
                    at += 1
                else:
                    break
            unwound = None
            if at < len(pipeline) and set(pipeline[at]) == {"$unwind"}:
                spec = pipeline[at]["$unwind"]
                if isinstance(spec, str):
                    unwound = spec
                elif isinstance(spec, dict) and not (set(spec) - {"path", "preserveNullAndEmptyArrays"}) and not spec.get("preserveNullAndEmptyArrays"):
                    unwound = spec.get("path")
                if isinstance(unwound, str) and unwound.startswith("$") and not unwound.startswith("$$"):
                    at += 1
                    while at < len(pipeline) and pipeline[at] == {"$match": {}}:
                        at += 1
                else:
                    unwound = None
            if at < len(pipeline) and set(pipeline[at]) == {"$group"}:
                fast = self.count_group(collection, pipeline[at]["$group"], unwind=unwound, predicate=predicate)
                if fast is not None:
                    source = fast
                    pipeline = pipeline[at + 1:]
                    original = False
                    self.metadata_fast_used = True
        for position, stage in enumerate(pipeline):
            if not isinstance(stage, dict) or len(stage) != 1:
                raise ValueError("Each aggregation stage requires one operator")
            operator, spec = next(iter(stage.items()))
            a = self.alias()
            doc = a + ".payload"
            if operator == "$match":
                where = self.match(spec, collection, a, original, variables)
                source = "SELECT " + a + ".* FROM (" + source + ") " + a + " WHERE " + where
            elif operator == "$sort":
                source = self.sort(source, spec)
            elif operator in ("$skip", "$limit"):
                if isinstance(spec, bool) or not isinstance(spec, int) or spec < 0 or operator == "$limit" and spec == 0:
                    raise ValueError("Aggregation limits must be nonnegative integers ($limit must be positive)")
                source = "SELECT * FROM (" + source + ") ORDER BY ord LIMIT " + (str(spec) if operator == "$limit" else "-1 OFFSET " + str(spec))
            elif operator == "$count":
                path_parts(spec)
                source = "SELECT json_object(" + literal(spec) + ",count(*)) AS payload,'[]' AS types,'' AS id_key,1 AS ord FROM (" + source + ") HAVING count(*)>0"
                original = False
            elif operator in ("$project", "$set", "$addFields", "$unset"):
                source = self.project(source, operator, spec, a, variables)
                original = False
            elif operator == "$unwind":
                source = self.unwind(source, spec, a)
                original = False
            elif operator == "$group":
                source = self.group(source, spec, a, variables)
                original = False
            elif operator == "$facet":
                source = self.facet(source, spec, collection, original, variables)
                original = False
            elif operator == "$lookup":
                source = self.lookup(source, spec, collection, a, variables)
                original = False
            elif operator in ("$search", "$searchMeta"):
                if not original:
                    raise NotImplementedError("Search requires original collection documents")
                from .search import compile_search
                where, params, order = compile_search(collection, spec, doc=doc, key=a + ".id_key")
                source = "SELECT " + a + ".* FROM (" + source + ") " + a + " WHERE " + self.absorb(where, params)
                if operator == "$searchMeta":
                    source = "SELECT json_object('count',json_object('total',count(*))) AS payload,'[]' AS types,'' AS id_key,1 AS ord FROM (" + source + ")"
                    original = False
                elif order:
                    if isinstance(order, str):
                        source = "SELECT " + a + ".payload," + a + ".types," + a + ".id_key,row_number() OVER (ORDER BY " + order + ") AS ord FROM (" + source + ") " + a
                    else:
                        source = self.sort(source, order)
            else:
                raise NotImplementedError("Unsupported aggregation stage " + operator)
        return source

    @staticmethod
    def ordinary_match(condition):
        """Leave aggregation expressions and correlated variables to match()."""
        if not isinstance(condition, dict):
            return False
        def has_expression(value):
            if isinstance(value, dict):
                return "$expr" in value or any(has_expression(item) for item in value.values())
            if isinstance(value, list):
                return any(has_expression(item) for item in value)
            return False
        return not has_expression(condition)

    def count_group(self, collection, spec, *, unwind=None, predicate=None):
        """Count scalar field groups from a covering metadata index.

        Initial empty matches and one ordinary indexed match are supported.
        The latter materializes unique eligible IDs and joins group metadata.
        Complete non-null equalities use the typed covering lookup directly;
        other predicates retain Query's full filtering semantics. SQLite may
        choose either join order, including metadata-first shape guards.
        Supported keys are direct scalar
        paths, string $ifNull defaults, uppercase string defaults, and scalar
        language elements after unwinding the same path. Aggregation expressions
        in filters and
        unsupported shapes keep the general compiler. Scalar type marks are
        fetched once per result group; normalized string facets need no JSON
        payload reads at all. Missing/null default collisions merge after count.
        """
        key = spec.get("_id")
        counts = {name: value for name, value in spec.items() if name != "_id"}
        if any(not isinstance(value, dict) or set(value) != {"$sum"} or type(value["$sum"]) is not int or value["$sum"] != 1 for value in counts.values()):
            return None
        if not counts:
            return None
        mode, default = "plain", None
        if isinstance(key, dict) and set(key) == {"$toUpper"}:
            mode, key = "upper", key["$toUpper"]
        if isinstance(key, dict) and set(key) == {"$ifNull"}:
            body = key["$ifNull"]
            if not isinstance(body, list) or len(body) != 2 or not isinstance(body[1], str) or body[1].startswith("$"):
                return None
            default, key = body[1], body[0]
            if mode == "plain":
                mode = "default"
        elif mode == "upper":
            return None
        if not isinstance(key, str) or not key.startswith("$") or key.startswith("$$"):
            return None
        path = key[1:]
        parts = path_parts(path)
        if any("/" in part for part in parts):
            return None
        if unwind is not None and (mode != "plain" or unwind != key):
            return None
        collection.ensure_path(path)
        eligibility = []
        eligible = eligible_alias = None
        if predicate is not None:
            if not self.ordinary_match(predicate):
                return None
            eligible, eligible_alias = self.alias(), self.alias()
            # Prepare every predicate index, including any FTS candidate index,
            # before recording the metadata/read-snapshot version below.
            equality = single_equality(predicate)
            if equality is not None:
                name, kind, value = equality
                collection.ensure_path(name)
                where = "path=" + self.bind(name) + " AND kind=" + self.bind(kind) + " AND value=" + self.bind(value)
                # Duplicate matching array members still identify one input
                # document. The shared helper excludes null/missing equality.
                eligibility.append(eligible + " AS MATERIALIZED (SELECT DISTINCT doc_key AS id_key FROM " + collection._values + " WHERE " + where + ")")
            else:
                selected = self.alias()
                query = Query(collection, doc=selected + ".payload", key=selected + ".id_key")
                where = self.absorb(query.compile(predicate), query.params)
                eligibility.append(eligible + " AS MATERIALIZED (SELECT " + selected + ".id_key FROM " + collection._table + " " + selected + " WHERE " + where + ")")
        connection = collection.database._connection()
        if self.metadata_version is None:
            self.metadata_version = (id(connection), connection.execute("PRAGMA data_version").fetchone()[0], connection.total_changes)
        ordinal = "/" + "/".join(parts)
        prefix = ordinal + "/"
        tail = "substr(ordinal," + str(len(prefix) + 1) + ")"
        child = "(substr(ordinal,1," + str(len(prefix)) + ")=" + literal(prefix) + " AND length(" + tail + ")>0 AND " + tail + " NOT GLOB '*[^0-9]*')"
        base = "ordinal=" + literal(ordinal)
        if unwind is not None:
            supported_array = "(json_type(value,'$[0]')='array' AND json_array_length(value,'$[1]')=0)"
            bad = "(NOT (" + base + " OR " + child + ") OR (" + base + " AND (kind NOT IN (0,3,4) OR (kind=4 AND NOT " + supported_array + "))) OR (" + child + " AND kind NOT IN (0,3)))"
        else:
            unsupported = "kind=4" if mode == "plain" else "kind NOT IN (0,3)"
            bad = "(" + unsupported + (" OR NOT (" + base + ")" if len(parts) > 1 else "") + ")"
        wanted = self.bind(path)
        if eligible is None:
            metadata_source = collection._values
            eligible_test = ""
        else:
            # Do not force one metadata probe per eligible document. Sparse
            # unsupported shapes are far cheaper to guard metadata-first, and
            # the optimizer can use a covering path index for grouped counts.
            metadata_source = eligible + " " + eligible_alias + " JOIN " + collection._values + " ON doc_key=" + eligible_alias + ".id_key"
            eligible_test = ""
        # CASE is lazy in SQLite. Keep the eligible CTE exclusively inside its
        # unsupported-shape branch, rather than joining it into the guard's
        # source: a valid sparse path must not materialize every matching ID
        # just to establish that there are no invalid shapes. A bad shape still
        # triggers the exact full predicate before selecting the fallback.
        guarded_shape = bad if eligible is None else "CASE WHEN " + bad + " THEN doc_key IN (SELECT id_key FROM " + eligible + ") ELSE 0 END"
        guard = ("WITH " + ",".join(eligibility) + " " if eligibility else "") + "SELECT 1 FROM " + collection._values + " WHERE path=" + wanted + " AND " + guarded_shape + " LIMIT 1"
        unsupported = connection.execute(guard, self.parameters).fetchone()
        if unsupported:
            return None
        terminal = self.bind(ordinal)
        present, absent, combined, grouped = (self.alias() for _ in range(4))
        record, missing = self.alias(), self.alias()
        representative = mode == "plain" and unwind is None
        scope = "path=" + wanted + " AND " + ("((ordinal=" + terminal + " AND kind=3) OR " + child + ")" if unwind is not None else "ordinal=" + terminal)
        minimum = "min(doc_key)" if representative else "NULL"
        ctes = [*eligibility, present + " AS MATERIALIZED (SELECT kind,value,count(*) AS n," + minimum + " AS representative FROM " + metadata_source + " WHERE " + eligible_test + scope + " GROUP BY kind,value)"]
        if unwind is not None:
            ctes.append(grouped + " AS (SELECT * FROM " + present + ")")
        else:
            input_ids = eligible if eligible is not None else collection._table
            ctes.append(absent + " AS MATERIALIZED (SELECT (SELECT count(*) FROM " + input_ids + ")-(SELECT coalesce(sum(n),0) FROM " + present + ") AS n)")
            missing_key = "(SELECT " + missing + ".id_key FROM " + input_ids + " " + missing + " WHERE NOT EXISTS (SELECT 1 FROM " + collection._values + " WHERE " + scope + " AND doc_key=" + missing + ".id_key) LIMIT 1)" if representative else "NULL"
            ctes.append(combined + " AS (SELECT kind,value,n,representative FROM " + present + " UNION ALL SELECT 0,NULL,n," + missing_key + " FROM " + absent + " WHERE n>0)")
            if mode == "plain":
                normalized_kind, normalized_value = "kind", "value"
            else:
                normalized_kind = "3"
                normalized_value = "CASE WHEN kind=0 THEN " + self.bind(default) + " ELSE value END"
                if mode == "upper":
                    normalized_value = "upper(" + normalized_value + ")"
            normalized = self.alias()
            ctes.append(normalized + " AS (SELECT " + normalized_kind + " AS kind," + normalized_value + " AS value,n,representative FROM " + combined + ")")
            ctes.append(grouped + " AS (SELECT kind,value,sum(n) AS n,min(representative) AS representative FROM " + normalized + " GROUP BY kind,value)")
        if representative:
            identifier = _json_at(record + ".payload", path)
            types = self.pruned_types(record + ".types", retained=[], renamed=[("_id", path)])
            join = " JOIN " + collection._table + " " + record + " ON " + record + ".id_key=" + grouped + ".representative"
        else:
            identifier = "CASE WHEN " + grouped + ".kind=0 THEN 'null' ELSE json_quote(" + grouped + ".value) END"
            types, join = "'[]'", ""
        fields = ["'_id',json(" + identifier + ")"]
        fields.extend(literal(name) + "," + grouped + ".n" for name in counts)
        return ("WITH " + ",".join(ctes) + " SELECT jsonb(json_object(" + ",".join(fields) + ")) AS payload," + types + " AS types,"
                + grouped + ".representative AS id_key,row_number() OVER (ORDER BY " + grouped + ".kind," + grouped + ".value) AS ord FROM "
                + grouped + join)

    def project(self, source, operator, spec, a, variables):
        doc = a + ".payload"
        retained, removed, renamed = None, [], []
        if operator == "$unset":
            paths = [spec] if isinstance(spec, str) else list(spec)
            removed = paths
            payload = "json_remove(" + doc + "," + ",".join(literal(json_path(path)) for path in paths) + ")" if paths else doc
        else:
            included = operator == "$project" and any(value not in (0, False) for key, value in spec.items() if key != "_id")
            id_only = operator == "$project" and spec == {"_id": 1}
            if operator == "$project" and not included and not id_only:
                removed = [key for key, value in spec.items() if not value]
                payload = "json_remove(" + doc + "," + ",".join(literal(json_path(key)) for key in removed) + ")" if removed else doc
            else:
                if included and any(value in (0, False) for key, value in spec.items() if key != "_id"):
                    raise ValueError("Cannot mix inclusion and exclusion projections")
                payload = "'{}'" if operator == "$project" else doc
                items = list(spec.items())
                if operator == "$project" and spec.get("_id", 1) and "_id" not in spec:
                    items.insert(0, ("_id", 1))
                if operator == "$project":
                    retained = [key for key, value in items if value in (1, True)]
                    # Keep the original in a column, rather than duplicating an
                    # increasingly large CASE expression for every field.
                    source = "SELECT payload AS original,'{}' AS payload,types,id_key,ord FROM (" + source + ")"
                for key, value in items:
                    path = literal(json_path(key))
                    if operator == "$project" and value in (0, False):
                        continue
                    if operator == "$project" and value in (1, True):
                        step = self.alias()
                        item = _json_at(step + ".original", key)
                        payload = "(CASE WHEN " + field_type(step + ".original", key) + " IS NULL THEN " + step + ".payload ELSE json_set(" + step + ".payload," + path + ",json(" + item + ")) END)"
                        source = "SELECT " + step + ".original," + payload + " AS payload," + step + ".types," + step + ".id_key," + step + ".ord FROM (" + source + ") " + step
                    else:
                        removed.append(key)
                        if isinstance(value, str) and value.startswith("$") and not value.startswith("$$"):
                            renamed.append((key, value[1:]))
                        if operator == "$project":
                            step = self.alias()
                            payload = "json_set(" + step + ".payload," + path + ",json(" + self.jexpr(value, step + ".original", variables) + "))"
                            source = "SELECT " + step + ".original," + payload + " AS payload," + step + ".types," + step + ".id_key," + step + ".ord FROM (" + source + ") " + step
                        else:
                            payload = "json_set(" + payload + "," + path + ",json(" + self.jexpr(value, doc, variables) + "))"
                if operator == "$project":
                    a = self.alias()
                    payload = a + ".payload"
        types = self.pruned_types(a + ".types", retained, removed, renamed)
        return "SELECT jsonb(" + payload + ") AS payload," + types + " AS types," + a + ".id_key," + a + ".ord FROM (" + source + ") " + a

    @staticmethod
    def type_path_under(path, parts):
        checks = ["json_array_length(" + path + ")>=" + str(len(parts))]
        for i, part in enumerate(parts):
            value = str(int(part)) if str(part).isdigit() else literal(part)
            checks.append("json_extract(" + path + ",'$[" + str(i) + "]') IS " + value)
        return "(" + " AND ".join(checks) + ")"

    def pruned_types(self, types, retained=None, removed=(), renamed=()):
        mark = self.alias()
        path = "json_extract(" + mark + ".value,'$[0]')"
        conditions = []
        if retained is not None:
            conditions.append("(" + " OR ".join(self.type_path_under(path, path_parts(key)) for key in retained) + ")" if retained else "0")
        for key in removed:
            conditions.append("NOT " + self.type_path_under(path, path_parts(key)))
        result = "SELECT " + mark + ".value AS mark FROM json_each(" + types + ") " + mark
        if conditions:
            result += " WHERE " + " AND ".join(conditions)
        for target, origin in renamed:
            suffix = path
            for _ in path_parts(origin):
                suffix = "json_remove(" + suffix + ",'$[0]')"
            prefix = literal(json.dumps(path_parts(target), separators=(",", ":")))
            copied = "json_set(" + mark + ".value,'$[0]'," + _concat_paths(prefix, suffix) + ")"
            result += " UNION ALL SELECT " + copied + " AS mark FROM json_each(" + types + ") " + mark + " WHERE " + self.type_path_under(path, path_parts(origin))
        return "(SELECT coalesce(json_group_array(json(mark)),'[]') FROM (" + result + "))"

    def unwind(self, source, spec, a):
        if isinstance(spec, str):
            spec = {"path": spec}
        if not isinstance(spec.get("path"), str) or not spec["path"].startswith("$"):
            raise ValueError("$unwind requires a field path")
        path = spec["path"][1:]
        parts = path_parts(path)
        doc = a + ".payload"
        j = self.alias()
        typ = field_type(doc, path)
        values = "(CASE WHEN " + typ + "='array' THEN " + field(doc, path) + " WHEN " + typ + " IS NULL OR " + typ + "='null' THEN '[]' ELSE json_array(json(" + _json_at(doc, path) + ")) END)"
        element = "(CASE " + j + ".type WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' WHEN 'array' THEN json(" + j + ".value) WHEN 'object' THEN json(" + j + ".value) ELSE json_quote(" + j + ".value) END)"
        payload = "(CASE WHEN " + j + ".key IS NULL THEN CASE WHEN " + typ + "='array' THEN json_remove(" + doc + "," + literal(json_path(path)) + ") ELSE " + doc + " END ELSE json_set(" + doc + "," + literal(json_path(path)) + ",json(" + element + ")) END)"
        if spec.get("includeArrayIndex"):
            payload = "json_set(" + payload + "," + literal(json_path(spec["includeArrayIndex"])) + "," + j + ".key)"
        mark = self.alias()
        mp = "json_extract(" + mark + ".value,'$[0]')"
        prefix = " AND ".join("json_extract(" + mp + ",'$[" + str(i) + "]')=" + literal(part) for i, part in enumerate(parts))
        deeper = "json_array_length(" + mp + ")>" + str(len(parts))
        index = "json_extract(" + mp + ",'$[" + str(len(parts)) + "]')"
        transformed = "CASE WHEN (" + prefix + ") AND " + deeper + " THEN json_set(" + mark + ".value,'$[0]',json_remove(" + mp + ",'$[" + str(len(parts)) + "]')) ELSE " + mark + ".value END"
        keep = "NOT (" + prefix + ") OR NOT (" + deeper + ") OR " + index + "=" + j + ".key"
        types = "(SELECT coalesce(json_group_array(json(" + transformed + ")),'[]') FROM json_each(" + a + ".types) " + mark + " WHERE " + keep + ")"
        join = " LEFT JOIN " if spec.get("preserveNullAndEmptyArrays") else " JOIN "
        return "SELECT jsonb(" + payload + ") AS payload," + types + " AS types," + a + ".id_key,row_number() OVER (ORDER BY " + a + ".ord," + j + ".key) AS ord FROM (" + source + ") " + a + join + "json_each(" + values + ") " + j + " ON 1"

    def group(self, source, spec, a, variables):
        if "_id" not in spec:
            raise ValueError("$group requires _id")
        prepared = self.alias()
        key = self.jexpr(spec["_id"], a + ".payload", variables)
        source = "SELECT " + a + ".*," + key + " AS group_key FROM (" + source + ") " + a
        firsts = {}
        for name, accumulator in spec.items():
            if name == "_id":
                continue
            if not isinstance(accumulator, dict) or len(accumulator) != 1:
                raise NotImplementedError("Unsupported group accumulator")
            op, body = next(iter(accumulator.items()))
            if op == "$first":
                firsts[name] = self.alias()
        if firsts:
            window = []
            for name, alias in firsts.items():
                expression = self.jexpr(spec[name]["$first"], prepared + ".payload", variables)
                window.append("first_value(" + expression + ") OVER (PARTITION BY group_key ORDER BY ord) AS " + alias)
            source = "SELECT " + prepared + ".*," + ",".join(window) + " FROM (" + source + ") " + prepared
        g = self.alias()
        fields = ["'_id',json(" + g + ".group_key)"]
        for name, accumulator in spec.items():
            if name == "_id":
                continue
            op, body = next(iter(accumulator.items()))
            expression = self.expr(body, g + ".payload", variables) if op != "$first" else ""
            if op == "$sum":
                value = "coalesce(sum(CASE WHEN typeof(" + expression + ") IN ('integer','real') THEN " + expression + " ELSE 0 END),0)"
            elif op in ("$avg", "$min", "$max"):
                value = op[1:] + "(" + expression + ")"
            elif op == "$first":
                value = "json(" + g + "." + firsts[name] + ")"
            elif op == "$push":
                value = "json_group_array(json(" + self.jexpr(body, g + ".payload", variables) + ") ORDER BY " + g + ".ord)"
            elif op == "$addToSet":
                value = "json_group_array(DISTINCT json(" + self.jexpr(body, g + ".payload", variables) + "))"
            else:
                raise NotImplementedError("Unsupported group accumulator " + op)
            fields.append(literal(name) + "," + value)
        return "SELECT jsonb(json_object(" + ",".join(fields) + ")) AS payload,'[]' AS types," + g + ".group_key AS id_key,min(" + g + ".ord) AS ord FROM (" + source + ") " + g + " GROUP BY " + g + ".group_key"

    def facet(self, source, spec, collection, original, variables):
        base = self.alias()
        ctes = [base + " AS MATERIALIZED (" + source + ")"]
        fields, marks = [], []
        for name, pipeline in spec.items():
            branch = self.alias()
            branch_sql = self.pipeline(pipeline, collection, "SELECT * FROM " + base, original, variables)
            ctes.append(branch + " AS MATERIALIZED (" + branch_sql + ")")
            fields.append(literal(name) + ",json((SELECT coalesce(json_group_array(json(payload) ORDER BY ord),'[]') FROM " + branch + "))")
            marks.append(self.nested_marks("SELECT * FROM " + branch, name))
        types = "(SELECT coalesce(json_group_array(json(mark)),'[]') FROM (" + " UNION ALL ".join(marks) + "))" if marks else "'[]'"
        return "WITH " + ",".join(ctes) + " SELECT jsonb(json_object(" + ",".join(fields) + ")) AS payload," + types + " AS types,'' AS id_key,1 AS ord"

    def lookup(self, source, spec, collection, a, variables):
        allowed = {"from", "localField", "foreignField", "as", "let", "pipeline"}
        if set(spec) - allowed or "from" not in spec or "as" not in spec:
            raise NotImplementedError("Unsupported lookup specification")
        foreign = collection.database[spec["from"]]
        f = self.alias()
        base = "SELECT " + f + ".payload," + f + ".types," + f + ".id_key," + f + ".rowid AS ord FROM " + foreign._table + " " + f
        if "localField" in spec or "foreignField" in spec:
            if "localField" not in spec or "foreignField" not in spec:
                raise ValueError("Lookup requires both localField and foreignField")
            local = _json_at(a + ".payload", spec["localField"])
            base += " WHERE " + self.indexed_equal(foreign, f, spec["foreignField"], local)
        bound = dict(variables)
        bound.update({name: self.jexpr(value, a + ".payload", variables) for name, value in spec.get("let", {}).items()})
        branch = self.pipeline(spec.get("pipeline", []), foreign, base, True, bound)
        joined = "(SELECT coalesce(json_group_array(json(payload) ORDER BY ord),'[]') FROM (" + branch + "))"
        new_types = self.nested_marks(branch, spec["as"])
        old = self.alias()
        prior_types = self.pruned_types(a + ".types", removed=[spec["as"]])
        types = "(SELECT coalesce(json_group_array(json(mark)),'[]') FROM (SELECT " + old + ".value AS mark FROM json_each(" + prior_types + ") " + old + " UNION ALL " + new_types + "))"
        payload = "json_set(" + a + ".payload," + literal(json_path(spec["as"])) + ",json(" + joined + "))"
        return "SELECT jsonb(" + payload + ") AS payload," + types + " AS types," + a + ".id_key," + a + ".ord FROM (" + source + ") " + a


def aggregate(collection, pipeline):
    pipeline = list(pipeline)
    compiler = Compiler(collection)
    sql = compiler.pipeline(pipeline)
    sql = "SELECT json(payload),types FROM (" + sql + ") ORDER BY ord"

    def rows():
        with collection.database.transaction(write=False):
            connection = collection.database._connection()
            active, statement = compiler, sql
            if compiler.metadata_fast_used:
                # Pin the read snapshot before checking the version. Another
                # process can change field shapes between compilation and the
                # first iteration, and this connection can also have written.
                connection.execute("SELECT 1 FROM _collections LIMIT 1").fetchone()
                version = (id(connection), connection.execute("PRAGMA data_version").fetchone()[0], connection.total_changes)
                if version != compiler.metadata_version:
                    active = Compiler(collection)
                    statement = "SELECT json(payload),types FROM (" + active.pipeline(pipeline) + ") ORDER BY ord"
            cursor = connection.execute(statement, active.parameters)
            try:
                while batch := cursor.fetchmany(100):
                    for payload, types in batch:
                        yield codec.decode_document(payload, types)
            finally:
                cursor.close()

    return rows()
