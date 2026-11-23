"""Independent structural guard proofs for a deliberately small unit language.

This is an abstract interpretation, not execution of representative inputs.
Accepted predicates distinguish only three literal meta-property names. Thus
those three singleton sets and their complement partition *all* primitive
string keys exactly. Source values remain uninterpreted arbitrary plain data.
Both arms, even unreachable arms, are checked for unsupported operations.

Only shallow copies are covered. Unknown helpers, recursion, ambient reads,
mutation of bindings, nested property traversal, and imported claims abstain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from app.backend.pipeline import evidence_ast

RULE_ID = "shallow-copy-key-domain/1"
_META = ("__proto__", "constructor", "prototype")
_OTHER = "<all-other-string-keys>"
_DOMAINS = (*_META, _OTHER)
_RESERVED = {"Object", "Array", "Error", "undefined", "arguments", "eval", "globalThis", "require", "process", "module"}


class _Unsupported(ValueError):
    pass


def _require(condition):
    if not condition:
        raise _Unsupported("The complete unit is outside the checked shallow-copy language.")


def _children(node):
    return [child for child in node.named_children if child.type != "comment"] if node is not None else []


def _walk(node):
    yield node
    for child in _children(node):
        yield from _walk(child)


def _operator(node):
    return next((child.type for child in node.children if not child.is_named
                 and child.type not in ("(", ")", ";", ",")), "")


def _unwrap(node):
    while node is not None and node.type == "parenthesized_expression":
        parts = _children(node)
        _require(len(parts) == 1)
        node = parts[0]
    return node


@dataclass
class _State:
    env: dict
    domain: str
    operations: list = field(default_factory=list)
    flow: str | None = None
    indexed_keys: tuple | None = None

    def clone(self):
        return _State(dict(self.env), self.domain, list(self.operations), self.flow, self.indexed_keys)


class _Copy:
    def __init__(self, code, language):
        parsed = evidence_ast.parse(language, code)
        _require(parsed is not None)
        self.root, self.data = parsed
        _require(sum(1 for _ in _walk(self.root)) <= 2000)
        # JS identifier escapes denote the same binding as their decoded name.
        # Reject them across the whole source instead of comparing raw spellings
        # and accidentally treating a rebind as an independent local alias.
        _require(not any("\\" in self.text(node) for node in _walk(self.root)
                         if node.type in ("identifier", "property_identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern")))
        self.writes, self.guards = [], []
        self.function = self._unit()
        self.parameters = self._parameters(self.function)
        _require(len(self.parameters) == 2 and len(set(self.parameters)) == 2)
        self.target, self.source = self.parameters
        self.body = self.function.child_by_field_name("body")
        _require(self.body is not None and self.body.type == "statement_block")
        self._check_bindings()
        loops = [node for node in _walk(self.body) if node.type in ("for_statement", "for_in_statement")]
        _require(len(loops) == 1)
        self.loop = loops[0]
        self.results = {}
        for domain in _DOMAINS:
            state = _State({self.target: ("target",), self.source: ("source",)}, domain)
            self._statements(_children(self.body), state, inside_loop=False)
            _require(state.flow == "return")
            self.results[domain] = state.operations

    def text(self, node):
        return self.data[node.start_byte:node.end_byte].decode("utf-8") if node is not None else ""

    def _unit(self):
        statements = _children(self.root)
        _require(len(statements) == 1)
        node = statements[0]
        if node.type == "export_statement":
            parts = _children(node)
            _require(len(parts) == 1)
            node = parts[0]
        if node.type in ("lexical_declaration", "variable_declaration"):
            parts = _children(node)
            _require(len(parts) == 1 and parts[0].type == "variable_declarator")
            declaration = parts[0]
            name = declaration.child_by_field_name("name")
            _require(name is not None and name.type == "identifier" and self.text(name) not in _RESERVED)
            node = declaration.child_by_field_name("value")
        node = _unwrap(node)
        _require(node is not None and node.type in ("function_declaration", "function_expression", "arrow_function"))
        _require(not any(child.type in ("async", "*") for child in node.children))
        name = node.child_by_field_name("name")
        _require(name is None or self.text(name) not in _RESERVED)
        return node

    def _parameters(self, node):
        parameter = node.child_by_field_name("parameters") or node.child_by_field_name("parameter")
        _require(parameter is not None)
        parts = _children(parameter) if parameter.type == "formal_parameters" else [parameter]
        names = []
        for item in parts:
            if item.type == "required_parameter":
                _require(all(child.type in ("identifier", "type_annotation") for child in _children(item)))
                item = item.child_by_field_name("pattern")
            _require(item is not None and item.type == "identifier")
            names.append(self.text(item))
        return names

    def _check_bindings(self):
        names = list(self.parameters)
        for node in _walk(self.body):
            if node.type == "variable_declarator":
                name = node.child_by_field_name("name")
                _require(name is not None and name.type == "identifier")
                names.append(self.text(name))
            elif node.type == "for_in_statement":
                _require(any(child.type in ("var", "let", "const") for child in node.children))
                name = node.child_by_field_name("left")
                _require(name is not None and name.type == "identifier")
                names.append(self.text(name))
        _require(len(names) == len(set(names)) and not (set(names) & _RESERVED))

    def _member(self, node, property_name):
        node = _unwrap(node)
        if node is None or node.type != "member_expression":
            return None
        prop = node.child_by_field_name("property")
        if prop is None or prop.type != "property_identifier" or self.text(prop) != property_name:
            return None
        _require(not any(child.type == "optional_chain" for child in node.children))
        return node.child_by_field_name("object")

    def _literal(self, node):
        node = _unwrap(node)
        _require(node is not None and node.type == "string")
        raw = self.text(node)
        _require(len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"')
                 and all(child.type == "string_fragment" for child in _children(node)))
        return raw[1:-1]

    def _key_operand(self, node, state):
        node = _unwrap(node)
        if node.type == "string":
            literal = self._literal(node)
            _require(literal in _META)
            return literal
        value = self._expr(node, state)
        _require(value == ("key", state.domain))
        return state.domain

    def _truth(self, node, state):
        node = _unwrap(node)
        _require(node is not None)
        if node.type == "unary_expression" and _operator(node) == "!":
            return not self._truth(node.child_by_field_name("argument"), state)
        if node.type == "binary_expression":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            operator = _operator(node)
            if operator in ("&&", "||"):
                a, b = self._truth(left, state), self._truth(right, state)
                return a and b if operator == "&&" else a or b
            _require(operator in ("==", "===", "!=", "!=="))
            a, b = self._key_operand(left, state), self._key_operand(right, state)
            return (a == b) if operator in ("==", "===") else (a != b)
        if node.type == "call_expression":
            function = node.child_by_field_name("function")
            array = self._member(function, "includes")
            args = _children(node.child_by_field_name("arguments"))
            _require(array is not None and array.type == "array" and len(args) == 1)
            items = _children(array)
            # Reject holes/spreads rather than infer Array.includes behavior.
            _require(len([child for child in array.children if child.type == ","]) <= len(items))
            values = [self._literal(item) for item in items]
            _require(values and len(values) == len(set(values)) and set(values) <= set(_META))
            _require(self._expr(args[0], state) == ("key", state.domain))
            return state.domain in values
        raise _Unsupported("Only pure comparisons of primitive keys with literal meta names are covered.")

    def _expr(self, node, state):
        node = _unwrap(node)
        _require(node is not None)
        if node.type == "identifier":
            name = self.text(node)
            _require(name in state.env)
            return state.env[name]
        if node.type == "subscript_expression":
            obj = self._expr(node.child_by_field_name("object"), state)
            index = self._expr(node.child_by_field_name("index"), state)
            if obj == ("source",) and index == ("key", state.domain):
                return ("value", state.domain)
            if obj[0] == "keys" and index == ("index",):
                _require(obj == state.indexed_keys)
                return ("key", state.domain)
            raise _Unsupported("Only the current source value or enumerated string key can be read.")
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = _children(node.child_by_field_name("arguments"))
            receiver = self._member(fn, "keys")
            if receiver is not None and receiver.type == "identifier" and self.text(receiver) == "Object":
                _require(len(args) == 1 and self._expr(args[0], state) == ("source",))
                return ("keys", frozenset(_DOMAINS))
            receiver = self._member(fn, "filter")
            _require(receiver is not None and len(args) == 1)
            keys = self._expr(receiver, state)
            _require(keys[0] == "keys")
            callback = _unwrap(args[0])
            _require(callback.type == "arrow_function" and not any(child.type == "async" for child in callback.children))
            parameters = self._parameters(callback)
            _require(len(parameters) == 1 and parameters[0] not in _RESERVED and parameters[0] not in self.parameters)
            predicate = callback.child_by_field_name("body")
            if predicate.type == "statement_block":
                parts = _children(predicate)
                _require(len(parts) == 1 and parts[0].type == "return_statement" and len(_children(parts[0])) == 1)
                predicate = _children(parts[0])[0]
            admitted = set()
            for domain in _DOMAINS:
                scope = _State({parameters[0]: ("key", domain)}, domain)
                if self._truth(predicate, scope):
                    admitted.add(domain)
            self.guards.append(self.text(node))
            return ("keys", keys[1] & frozenset(admitted))
        raise _Unsupported("Unexpected expression or helper call.")

    def _declaration(self, node, state):
        for declaration in _children(node):
            _require(declaration.type == "variable_declarator")
            name = self.text(declaration.child_by_field_name("name"))
            _require(name not in state.env)
            state.env[name] = self._expr(declaration.child_by_field_name("value"), state)

    def _write(self, expression, state):
        if expression.type == "assignment_expression":
            _require(_operator(expression) == "=")
            left, right = expression.child_by_field_name("left"), expression.child_by_field_name("right")
            _require(left.type == "subscript_expression")
            _require(self._expr(left.child_by_field_name("object"), state) == ("target",))
            _require(self._expr(left.child_by_field_name("index"), state) == ("key", state.domain))
            _require(self._expr(right, state) == ("value", state.domain))
            operation = "assign"
        else:
            _require(expression.type == "call_expression")
            receiver = self._member(expression.child_by_field_name("function"), "defineProperty")
            _require(receiver is not None and receiver.type == "identifier" and self.text(receiver) == "Object")
            args = _children(expression.child_by_field_name("arguments"))
            _require(len(args) == 3 and self._expr(args[0], state) == ("target",)
                     and self._expr(args[1], state) == ("key", state.domain) and args[2].type == "object")
            descriptor = {}
            for pair in _children(args[2]):
                _require(pair.type == "pair")
                key, value = pair.child_by_field_name("key"), pair.child_by_field_name("value")
                _require(key.type in ("property_identifier", "string"))
                name = self.text(key) if key.type == "property_identifier" else self._literal(key)
                _require(name not in descriptor)
                descriptor[name] = value
            _require(set(descriptor) == {"value", "writable", "enumerable", "configurable"})
            _require(self._expr(descriptor["value"], state) == ("value", state.domain)
                     and all(descriptor[name].type == "true" for name in ("writable", "enumerable", "configurable")))
            self.guards.append(self.text(expression))
            operation = "define"
        self.writes.append(self.text(expression))
        if state.flow is None:
            state.operations.append(operation)

    def _loop(self, node, state):
        _require(node == self.loop)
        local = state.clone()
        if node.type == "for_in_statement":
            # The legacy `for (var key = expression in source)` grammar has a
            # separate value field evaluated before iteration. No field may be
            # silently skipped, including an untrusted initializer expression.
            _require(_children(node) == [node.child_by_field_name(name) for name in ("left", "right", "body")])
            kinds = [child.type for child in node.children if child.type in ("in", "of")]
            _require(len(kinds) == 1 and not any(child.type == "await" for child in node.children))
            iterable = self._expr(node.child_by_field_name("right"), local)
            if kinds == ["in"]:
                _require(iterable == ("source",))
                admitted = frozenset(_DOMAINS)
            else:
                _require(iterable[0] == "keys")
                admitted = iterable[1]
            key = self.text(node.child_by_field_name("left"))
            _require(key not in local.env)
            local.env[key] = ("key", local.domain)
        else:
            _require(_children(node) == [node.child_by_field_name(name) for name in ("initializer", "condition", "increment", "body")])
            initial = node.child_by_field_name("initializer")
            _require(initial is not None and initial.type in ("lexical_declaration", "variable_declaration") and len(_children(initial)) == 1)
            _require(not any(child.type == "const" for child in initial.children))
            declaration = _children(initial)[0]
            _require(declaration.type == "variable_declarator")
            index = declaration.child_by_field_name("name")
            _require(index is not None and index.type == "identifier" and self.text(declaration.child_by_field_name("value")) == "0")
            name = self.text(index)
            _require(name not in local.env)
            condition = _unwrap(node.child_by_field_name("condition"))
            _require(condition is not None and condition.type == "binary_expression" and _operator(condition) == "<")
            left, right = condition.child_by_field_name("left"), condition.child_by_field_name("right")
            _require(left.type == "identifier" and self.text(left) == name)
            array = self._member(right, "length")
            _require(array is not None)
            keys = self._expr(array, local)
            _require(keys[0] == "keys")
            increment = node.child_by_field_name("increment")
            _require(increment is not None and increment.type == "update_expression" and _operator(increment) == "++"
                     and self.text(increment.child_by_field_name("argument")) == name)
            local.env[name] = ("index",)
            local.indexed_keys = keys
            admitted = keys[1]
        if local.domain not in admitted:
            local.flow = "filtered"
        self._statement(node.child_by_field_name("body"), local, inside_loop=True)
        # Per-key effects lift to the entire benign enumeration only when each
        # ordinary iteration finishes. An early return/throw would lose later
        # source entries even if the first copied property were correct.
        _require(local.domain != _OTHER or local.flow not in ("return", "throw"))
        state.operations = local.operations
        if local.flow == "return":
            state.flow = "return"

    def _statements(self, nodes, state, *, inside_loop):
        for node in nodes:
            # Even code after return/continue is checked, but has no effects.
            self._statement(node, state, inside_loop=inside_loop)

    def _statement(self, node, state, *, inside_loop):
        _require(node is not None)
        kind = node.type
        if kind == "statement_block":
            statements = _children(node)
            lexical = {self.text(declaration.child_by_field_name("name"))
                       for statement in statements if statement.type == "lexical_declaration"
                       for declaration in _children(statement)}
            self._statements(statements, state, inside_loop=inside_loop)
            for name in lexical:
                state.env.pop(name, None)
        elif kind in ("lexical_declaration", "variable_declaration"):
            self._declaration(node, state)
        elif kind in ("for_in_statement", "for_statement"):
            _require(not inside_loop)
            self._loop(node, state)
        elif kind == "expression_statement":
            parts = _children(node)
            _require(inside_loop and len(parts) == 1)
            self._write(parts[0], state)
        elif kind == "if_statement":
            _require(inside_loop)
            condition = self._truth(node.child_by_field_name("condition"), state)
            yes, no = state.clone(), state.clone()
            self._statement(node.child_by_field_name("consequence"), yes, inside_loop=inside_loop)
            alternative = node.child_by_field_name("alternative")
            if alternative is not None:
                _require(alternative.type == "else_clause" and len(_children(alternative)) == 1)
                self._statement(_children(alternative)[0], no, inside_loop=inside_loop)
            chosen = yes if condition else no
            state.env, state.operations, state.flow = chosen.env, chosen.operations, chosen.flow
            self.guards.append(self.text(node))
        elif kind == "continue_statement":
            _require(inside_loop and not _children(node))
            if state.flow is None:
                state.flow = "continue"
        elif kind == "return_statement":
            parts = _children(node)
            _require(len(parts) == 1 and self._expr(parts[0], state) == ("target",))
            if state.flow is None:
                state.flow = "return"
        elif kind == "throw_statement":
            parts = _children(node)
            _require(inside_loop and len(parts) == 1 and parts[0].type == "new_expression")
            expression = parts[0]
            _require(self.text(expression.child_by_field_name("constructor")) == "Error")
            args = _children(expression.child_by_field_name("arguments"))
            _require(len(args) == 1)
            self._literal(args[0])
            if state.flow is None:
                state.flow = "throw"
        elif kind in ("empty_statement", "comment"):
            pass
        else:
            raise _Unsupported("Unexpected statement, write, call, or control flow.")


def _cwe(record):
    values = record.get("cwe_ids")
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            return False
    return isinstance(values, (list, tuple)) and "CWE-1321" in values


def examine(record, mechanism):
    """Return newly derived closed-world source facts, never imported claims."""
    if mechanism != "prototype_pollution" or record.get("language") not in ("javascript", "js", "typescript", "ts") or not _cwe(record):
        return None
    before, after = record.get("vulnerable_code"), record.get("fixed_code")
    if not all(isinstance(code, str) and code and len(code.encode("utf-8")) <= 100000 for code in (before, after)):
        return None
    try:
        old, new = _Copy(before, record["language"]), _Copy(after, record["language"])
        # This is exhaustive for the accepted key predicates, not sampled input
        # behavior. All other strings share exactly the same abstract execution.
        if old.results[_OTHER] != ["assign"] or old.results["__proto__"] != ["assign"]:
            return None
        if new.results[_OTHER] not in (["assign"], ["define"]) or new.results["__proto__"] not in ([], ["define"]):
            return None
        if any(len(operations) > 1 for operations in (*old.results.values(), *new.results.values())) or not new.guards:
            return None
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return None
    scope = "Prototype-changing __proto__ writes in this shallow plain-data property copy; recursion, downstream use and custom object behavior are outside this scope."
    safeguard = "Every complete string-key domain path excludes __proto__ from assignment or uses Object.defineProperty with an own writable, enumerable, configurable data descriptor before any prototype-changing write."
    return {"method": "static_closed_world", "mechanism": mechanism, "rule_id": RULE_ID, "repair_valid": True,
            "violated_invariant": "An attacker-controlled property name must not invoke the inherited __proto__ setter on the destination.",
            "attacker_control": f"The {old.source} argument supplies enumerable property names and arbitrary plain data values.",
            "preconditions": ["Inputs are separate finite plain data objects, with primitive string property keys, standard unmodified Object and Array intrinsics, and no accessors, proxies or custom inherited enumerable properties.",
                              "The destination is an extensible ordinary object with writable, enumerable, configurable own data properties and the standard inherited __proto__ setter; it initially has no own __proto__ property.",
                              "The vulnerable input has an enumerable own __proto__ property whose value is an object or null. Ordinary benign copy inputs exclude __proto__, constructor and prototype keys.",
                              "The claim covers only this complete unit and shallow destination prototype changes, not recursive merges or later consumers."],
            "evidence_spans": [{"quote": quote} for quote in dict.fromkeys(old.writes)], "condition_scope": scope,
            "behavioral_intent": "Copy every ordinary enumerable source data property to the destination and return that destination while preserving its prototype.",
            "supported_impact": "The vulnerable shallow assignment can replace the destination object's prototype. Global Object.prototype pollution is not established.",
            "fixed_review": {"scope": scope, "safeguard": safeguard,
                             "evidence": [{"quote": quote} for quote in dict.fromkeys(new.guards)]}}
