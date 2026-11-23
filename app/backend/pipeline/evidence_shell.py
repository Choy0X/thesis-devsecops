"""Restricted source proof for a shell-string to literal argv transformation.

This proves shell interpretation only. It does not certify the invoked tool,
its option handling, permissions, repository policy, or the containing package.
No subject code is executed by this module.
"""
from __future__ import annotations

import json
import re

from . import evidence_ast


def examine(row):
    from . import evidence_rules as rules
    if not any(rules._has_cwe(row, c) for c in ('CWE-78', 'CWE-77', 'CWE-88')):
        return None
    before = _parse_unit(row.get('language'), row.get('vulnerable_code'))
    after = _parse_unit(row.get('language'), row.get('fixed_code'))
    if before is None or after is None:
        return None
    br, bd = before; ar, ad = after
    old = _sinks(row,'before',br,bd,('execSync','exec'))
    new = _sinks(row,'after',ar,ad,('execFileSync','spawnSync','execFile'))
    if len(old) != 1 or len(new) != 1:
        return None
    (old,old_name,bound_old),(new,new_name,bound_new)=old[0],new[0]
    if (old_name == 'exec') != (new_name == 'execFile'):
        return None
    of, nf = _owner(old), _owner(new)
    if (of is None or nf is None or not _unit_owner(of) or not _unit_owner(nf)
            or not _reachable(old, of, bd) or not _reachable(new, nf, ad)):
        return None
    params = _parameters(of, bd)
    sink_bindings={rules.text(n.child_by_field_name('function'),data).split('.')[0] for n,data in [(old,bd),(new,ad)]}
    reserved=sink_bindings|{'String','require'}
    if not params or params & reserved:
        return None
    # Binding writes, destructuring and additional calls are not data-flow proof.
    for root, data in ((of,bd),(nf,ad)):
        for n in rules.walk(root):
            if n.type in ('assignment_expression', 'augmented_assignment_expression', 'update_expression'):
                return None
            if n.type=='variable_declarator':
                pattern=n.child_by_field_name('name')
                names={rules.text(x,data) for x in rules.walk(pattern) if x.type in ('identifier','shorthand_property_identifier_pattern')}
                if names & (params|reserved):return None
            if n.type in ('function_declaration','function_expression','class_declaration') and rules.text(n.child_by_field_name('name'),data) in reserved:
                return None
    oa = rules.children(old.child_by_field_name('arguments'))
    na = rules.children(new.child_by_field_name('arguments'))
    if old_name == 'exec' and bound_old:
        if not bound_new or len(oa) < 2 or len(na) < 3 or _shape(oa[-1],bd) != _shape(na[-1],ad):
            return None
        callback = oa[-1]
        if callback.type not in ('identifier','arrow_function','function_expression'):
            return None
        oa,na = oa[:-1],na[:-1]
    if len(oa) not in (1, 2) or len(na) not in (2, 3) or na[1].type != 'array':
        return None
    old_options, new_options = (oa[1] if len(oa) == 2 else None), (na[2] if len(na) == 3 else None)
    if not _options(old_options, bd) or not _options(new_options, ad):
        return None
    if _shape(old_options, bd) != _shape(new_options, ad):
        return None
    parts = _parts(oa[0], bd)
    if parts is None:
        return None
    words = _shell_words(parts)
    argv = [_parts(n, ad) for n in [na[0], *rules.children(na[1])]]
    if not words or any(a is None for a in argv) or words != argv:
        return None
    # argv alone does not make a shell interpreter or an opaque executable
    # factory safe. This proof knows only these direct process tools.
    executable = argv[0]
    if len(executable) != 1 or executable[0][0] != 'literal':
        return None
    program=executable[0][1]
    if program not in ('git','cat','lsof'):
        return None
    if program=='git' and (len(argv)<2 or len(argv[1])!=1 or argv[1][0][0]!='literal'
                           or argv[1][0][1] not in ('merge-base','fetch','checkout','show','log','ls-remote','diff','rev-parse','rev-list')):
        return None
    # At least one argument is a direct parameter or its data field. Constants,
    # installed-tool checks and unrelated changed helpers cannot be positives.
    controlled = sorted({atom[1][1] for arg in argv[1:] for atom in arg
                         if atom[0] == 'value' and _root_name(atom[1]) in params})
    if not controlled or any(atom[0] == 'value' and _root_name(atom[1]) not in params
                             and not _local_literal(of, bd, _root_name(atom[1]))
                             for arg in argv[1:] for atom in arg):
        return None
    # No intervening sanitizer, shadowing or computed value may masquerade as
    # the direct parameter flow. Other function calls are confined to handling
    # the process result or the catch block, and must be identical on both sides.
    ignored = {old.id: ('process',), new.id: ('process',)}
    if new_name == 'spawnSync':
        result = _result_name(new, ad)
        if not result and (old.parent.type != 'expression_statement' or new.parent.type != 'expression_statement'):
            return None
        for node in rules.walk(ar):
            if (result and node.type == 'member_expression' and rules.text(node.child_by_field_name('object'), ad) == result
                    and rules.text(node.child_by_field_name('property'), ad) == 'stdout'):
                ignored[node.id] = ('identifier', result)
    if _shape(br, bd, ignored) != _shape(ar, ad, ignored):
        return None
    scope = ('Shell interpretation of the supplied arguments in this process invocation. '
             'Tool-specific option injection, executable selection, repository policy and all other operations are outside this scope.')
    return {'method': 'static_closed_world', 'mechanism': 'command_injection', 'rule_id': 'shell-data-to-argv/1',
            'violated_invariant': 'Untrusted argument text must remain process data rather than become shell syntax.',
            'attacker_control': 'The supplied parameter fields ' + ', '.join(controlled) + ' enter shell command arguments directly.',
            'preconditions': ['The identified parameters are attacker-supplied primitive strings or plain data fields; no proxies, accessors or modified intrinsics.',
                              'The imported Node child_process functions and String retain standard semantics; the configured executable and working directory are trusted.',
                              'A POSIX shell is used by the exec operation. For benign equivalence the arguments are ordinary nonempty shell words without whitespace, quotes, expansions or operators.',
                              'The called executable supports the literal arguments shown, and process execution succeeds; tool-specific behavior and error-state differences are outside this proof.'],
            'evidence_spans': [{'quote': rules.text(old, bd)}], 'condition_scope': scope,
            'supported_impact': 'Shell substitution, quoting escapes or shell operators in a supplied argument can execute an unintended command.',
            'behavioral_intent': 'Pass the same benign argument words to the same executable, preserving the surrounding source control flow and result handling.',
            'fixed_review': {'scope': scope, 'safeguard': 'The process receives an explicit executable and matching argument array; supported literal options cannot enable a shell.',
                             'evidence': [{'quote': rules.text(new, ad)}]}, 'repair_valid': True}


def _owner(node):
    while node is not None:
        if node.type in ('function_declaration', 'function_expression', 'arrow_function', 'method_definition'):
            return node
        node = node.parent


def _parse_unit(language,code):
    parsed = evidence_ast.parse(language,code)
    if parsed is not None or not isinstance(code,str):
        return parsed
    return evidence_ast.parse(language,'class Subject {\n'+code+'\n}')


def _unit_owner(node):
    allowed = {'program','export_statement','expression_statement','assignment_expression',
               'variable_declarator','lexical_declaration','variable_declaration'}
    parent = node.parent
    if node.type == 'method_definition' and parent is not None and parent.type == 'class_body':
        if len([n for n in parent.named_children if n.type != 'comment']) != 1:
            return False
        outer = parent.parent
        if outer is None or outer.parent is None or outer.parent.type != 'program':
            return False
        return len([n for n in outer.parent.named_children if n.type != 'comment']) == 1
    while parent is not None:
        if parent.type not in allowed:
            return False
        if parent.type == 'program' and len([n for n in parent.named_children if n.type != 'comment']) != 1:
            return False
        parent = parent.parent
    return True


def _parameters(node, data):
    from .evidence_rules import children, text
    result = set()
    for param in children(node.child_by_field_name('parameters')):
        if param.type == 'identifier':
            result.add(text(param, data))
        elif param.type == 'required_parameter':
            name = param.child_by_field_name('pattern')
            if name is None or name.type != 'identifier':
                return set()
            result.add(text(name, data))
        else:
            return set()
    return result


def _reachable(call, owner, data):
    from .evidence_rules import children
    node = call
    allowed = {'arguments', 'call_expression', 'member_expression', 'await_expression',
               'return_statement', 'expression_statement', 'variable_declarator',
               'lexical_declaration', 'variable_declaration', 'statement_block', 'try_statement'}
    while node != owner:
        if node.type not in allowed:
            return False
        parent = node.parent
        if parent is None:
            return False
        if parent.type == 'statement_block':
            for earlier in children(parent):
                if earlier == node:
                    break
                if earlier.type not in ('lexical_declaration', 'variable_declaration'):
                    return False
                # Prefix setup is limited to passive literals. It cannot call a
                # sanitizer, throw, return early or replace the imported sink.
                for decl in children(earlier):
                    if _passive(decl.child_by_field_name('value'), data) is None:
                        return False
        node = parent
    return True


def _passive(node, data):
    from .evidence_rules import children, text
    if node is None:
        return None
    if node.type in ('number', 'string', 'true', 'false', 'null', 'identifier', 'member_expression'):
        if node.type == 'member_expression' and _atom(node, data) is None:
            return None
        return _shape(node, data)
    if node.type in ('array', 'object', 'pair') and all(_passive(n, data) is not None for n in children(node)):
        return _shape(node, data)
    if node.type == 'property_identifier':
        return text(node, data)
    return None


def _string(node, data):
    from .evidence_rules import text
    value = text(node, data)
    if node.type != 'string' or len(value) < 2 or '\\' in value or value[0] not in ('"', "'") or value[-1] != value[0]:
        return None
    return value[1:-1]


def _shape(node, data, overrides=None):
    from .evidence_rules import text
    if node is None:
        return None
    if overrides and node.id in overrides:
        return overrides[node.id]
    if node.type in ('comment', ';', ','):
        return None
    if node.type == 'string':
        value = _string(node, data)
        return ('string', value) if value is not None else ('raw_string',text(node,data))
    parts = tuple(v for n in node.children if (v := _shape(n, data, overrides)) is not None)
    return (node.type, parts) if parts else (node.type, text(node, data))


def _atom(node, data):
    from .evidence_rules import text
    if node.type == 'identifier' and re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*', text(node, data)):
        return ('identifier', text(node, data))
    if node.type == 'member_expression':
        obj, prop = node.child_by_field_name('object'), node.child_by_field_name('property')
        base = _atom(obj, data)
        if base is not None and prop.type == 'property_identifier':
            return ('member', text(node, data), base)
    if node.type == 'call_expression' and text(node.child_by_field_name('function'), data) == 'String':
        args = node.child_by_field_name('arguments').named_children
        return _atom(args[0], data) if len(args) == 1 else None
    return None


def _root_name(atom):
    return atom[1] if atom[0] == 'identifier' else _root_name(atom[2])


def _parts(node, data):
    from .evidence_rules import children, text
    if node.type == 'string':
        value = _string(node, data)
        return [('literal', value)] if value is not None else None
    if node.type == 'template_string':
        parts = []
        for child in children(node):
            if child.type == 'string_fragment':
                parts.append(('literal', text(child, data)))
            elif child.type == 'template_substitution' and len(children(child)) == 1:
                value = _atom(children(child)[0], data)
                if value is None:
                    return None
                parts.append(('value', value))
            else:
                return None
        return _merge(parts)
    if node.type == 'binary_expression' and any(n.type == '+' for n in node.children):
        left, right = _parts(node.child_by_field_name('left'), data), _parts(node.child_by_field_name('right'), data)
        return _merge(left + right) if left is not None and right is not None else None
    atom = _atom(node, data)
    return [('value', atom)] if atom is not None else None


def _merge(parts):
    result = []
    for typ, value in parts:
        if typ == 'literal' and not value:
            continue
        if typ == 'literal' and result and result[-1][0] == typ:
            result[-1] = (typ, result[-1][1] + value)
        else:
            result.append((typ, value))
    return result


def _shell_words(parts):
    words, word, quote = [], [], None
    for typ, value in parts:
        if typ == 'value':
            word.append((typ, value)); continue
        literal = ''
        for char in value:
            if char in ('\\', '$', '`', ';', '|', '&', '>', '<', '\n', '\r', '(', ')', '*', '?', '[', ']'):
                return None
            if char in ('"', "'"):
                if quote is None:
                    quote = char
                elif quote == char:
                    quote = None
                else:
                    literal += char
            elif char.isspace() and quote is None:
                if literal:
                    word.append(('literal', literal)); literal = ''
                if word:
                    words.append(_merge(word)); word = []
            else:
                literal += char
        if literal:
            word.append(('literal', literal))
    if quote is not None:
        return None
    if word:
        words.append(_merge(word))
    return words


def _options(node, data):
    from .evidence_rules import children, text
    if node is None:
        return True
    if node.type != 'object':
        return False
    seen = set()
    for pair in children(node):
        if pair.type != 'pair':
            return False
        key, value = pair.child_by_field_name('key'), pair.child_by_field_name('value')
        name = text(key, data)
        if key.type != 'property_identifier' or name in seen or name not in ('cwd', 'stdio', 'encoding', 'timeout', 'maxBuffer', 'shell'):
            return False
        seen.add(name)
        if name == 'shell' and value.type != 'false':
            return False
        if _passive(value, data) is None:
            return False
    return True


def _result_name(node, data):
    from .evidence_rules import text
    parent = node.parent
    return text(parent.child_by_field_name('name'), data) if parent.type == 'variable_declarator' else None


def _local_literal(owner, data, name):
    from .evidence_rules import children, text
    values = []
    for stmt in children(owner.child_by_field_name('body')):
        if stmt.type != 'lexical_declaration' or not any(n.type == 'const' for n in stmt.children):
            continue
        for decl in children(stmt):
            if decl.type == 'variable_declarator' and text(decl.child_by_field_name('name'), data) == name:
                value = decl.child_by_field_name('value')
                values.append(value is not None and value.type in ('number', 'string'))
    return values == [True]


def _context_statements(row, side):
    from .evidence_rules import children
    value = row.get('context_' + side)
    try:
        context = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    if not isinstance(context,dict):return []
    result = []
    for item in (context or {}).get('items', []):
        if item.get('path') != row.get('file_path_' + side) or item.get('kind') not in ('imports','callee','module_symbol'):
            continue
        parsed = evidence_ast.parse(row.get('language'), item.get('text'))
        if parsed is None and item.get('kind')=='imports':return None
        if parsed:
            root,data = parsed
            result.extend((stmt,data) for stmt in children(root))
    return result


def _sinks(row,side,root,data,methods):
    from .evidence_rules import walk,text
    result=[]
    for node in walk(root):
        if node.type!='call_expression':continue
        fn=node.child_by_field_name('function')
        if fn.type=='identifier':
            local=text(fn,data)
            for method in methods:
                direct=_binding(row,side,local,method)
                if direct or (method in ('exec','execFile') and _binding(row,side,local,method,promisified=True)):
                    result.append((node,method,direct))
        elif fn.type=='member_expression':
            obj,prop=fn.child_by_field_name('object'),fn.child_by_field_name('property')
            method=text(prop,data)
            if obj.type=='identifier' and method in methods and _binding(row,side,text(obj,data),'namespace'):
                result.append((node,method,True))
    return result


def _binding(row, side, local, expected, *, module='child_process', promisified=False):
    from .evidence_rules import _builtin_import, children, text
    if not promisified and local == expected and module == 'child_process' and _builtin_import(row, side, local):
        return True
    matches = []
    statements=_context_statements(row,side)
    if statements is None:return False
    for stmt,data in statements:
        if stmt.type=='import_statement' and not promisified:
            origin=text(stmt.child_by_field_name('source'),data) in ('"'+module+'"',"'"+module+"'",'"node:'+module+'"',"'node:"+module+"'")
            type_only=any(n.type=='type' for n in stmt.children)
            from .evidence_rules import walk
            for node in walk(stmt):
                if node.type=='import_clause':
                    for child in children(node):
                        if child.type=='identifier' and text(child,data)==local:
                            matches.append(origin and expected=='namespace' and not type_only)
                if node.type=='import_specifier':
                    imported=text(node.child_by_field_name('name'),data)
                    alias=text(node.child_by_field_name('alias'),data) or imported
                    if alias==local:
                        matches.append(origin and imported==expected and not type_only and not any(n.type=='type' for n in node.children))
                elif node.type=='namespace_import' and any(text(n,data)==local for n in children(node)):
                    matches.append(origin and expected=='namespace' and not type_only)
            continue
        if stmt.type != 'lexical_declaration' or not any(n.type == 'const' for n in stmt.children):
            continue
        for decl in children(stmt):
            if decl.type != 'variable_declarator':
                continue
            name,value = decl.child_by_field_name('name'),decl.child_by_field_name('value')
            if value is None:
                continue
            if promisified and name.type == 'identifier' and text(name,data) == local:
                args = children(value.child_by_field_name('arguments')) if value.type == 'call_expression' else []
                matches.append(len(args) == 1 and args[0].type == 'identifier'
                               and text(value.child_by_field_name('function'),data) == 'promisify'
                               and _binding(row,side,'promisify','promisify',module='util')
                               and _binding(row,side,text(args[0],data),expected))
                continue
            member = None
            if value.type == 'member_expression':
                member = text(value.child_by_field_name('property'),data)
                value = value.child_by_field_name('object')
            args = children(value.child_by_field_name('arguments')) if value.type == 'call_expression' else []
            origin = (len(args) == 1 and _string(args[0],data) in (module,'node:'+module)
                      and text(value.child_by_field_name('function'),data) == 'require')
            if name.type == 'identifier' and text(name,data) == local:
                matches.append(origin and (member or 'namespace') == expected)
            elif name.type == 'object_pattern' and member is None:
                for entry in children(name):
                    if entry.type == 'shorthand_property_identifier_pattern' and text(entry,data) == local:
                        matches.append(origin and local == expected)
                    elif entry.type == 'pair_pattern':
                        key,val = entry.child_by_field_name('key'),entry.child_by_field_name('value')
                        if val.type == 'identifier' and text(val,data) == local:
                            matches.append(origin and text(key,data) == expected)
    return matches == [True]
