"""Policy-3 compositional proof for a bounded recursive own-key merge language.

Induction is over finite acyclic JSON source trees. Own primitive string keys
are partitioned into three reserved singleton domains and their complement.
The exact filter removes each reserved domain before destination access. For
other keys, an ordinary inherited Object method is a function, so the checked
object-only predicate cannot select it as a recursive destination. Own JSON
destinations contain no aliases to shared prototypes. Direct writes create own
ordinary properties; recursive writes preserve the invariant at smaller source
subtrees. The unchanged body preserves behavior for nonreserved keys.

This analyzer produces source facts only after exact-source and binding checks.
Only policy 3 consumes them. It does not execute subjects, issue receipts,
change source/context data or bypass source permission and partition gates.
"""
import copy
import json

from app.backend.pipeline import evidence_ast as ast
from app.backend.pipeline import evidence_binding_proof
from app.backend.pipeline import evidence_module_witness
from app.backend.pipeline import evidence_rules as r
from app.backend.pipeline.verify.closure_diagnostics import binding_origins

SCHEMA = 'recursive-own-key-proof/1'
RULE_ID = 'recursive-own-key-domains/1'
META = ('__proto__', 'constructor', 'prototype')


class Unsupported(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Unsupported(reason)


def unwrap(node):
    while node is not None and node.type == 'parenthesized_expression':
        values = r.children(node)
        require(len(values) == 1, 'ambiguous_parentheses')
        node = values[0]
    return node


def ident(node, name, raw):
    return node is not None and node.type == 'identifier' and r.text(node, raw) == name


def string(node, raw):
    node = unwrap(node)
    require(node is not None and node.type == 'string', 'literal_string_required')
    value = r.text(node, raw)
    require(len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'")
            and '\\' not in value, 'plain_literal_string_required')
    return value[1:-1]


def member(node, property_name, raw):
    node = unwrap(node)
    require(node is not None and node.type == 'member_expression', 'direct_member_required')
    prop = node.child_by_field_name('property')
    require(prop is not None and prop.type == 'property_identifier' and r.text(prop, raw) == property_name
            and not any(child.type == 'optional_chain' for child in node.children), 'member_operation_mismatch')
    return node.child_by_field_name('object')


def call(node, raw):
    node = unwrap(node)
    require(node is not None and node.type == 'call_expression'
            and not any(child.type == 'optional_chain' for child in node.children), 'direct_call_required')
    return node.child_by_field_name('function'), r.children(node.child_by_field_name('arguments'))


def one_parameter(node, raw):
    parameters = node.child_by_field_name('parameters') or node.child_by_field_name('parameter')
    values = r.children(parameters) if parameters is not None and parameters.type == 'formal_parameters' else [parameters]
    require(len(values) == 1 and values[0] is not None and values[0].type == 'identifier', 'simple_callback_parameter_required')
    return r.text(values[0], raw)


def unit(code):
    parsed = ast.parse('javascript', code)
    require(parsed is not None, 'complete_javascript_unit_required')
    tree, raw = parsed
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        require(count <= 4000 and depth <= 64, 'prototype_analysis_budget_exceeded')
        pending.extend((child, depth + 1) for child in node.named_children)
    statements = r.children(tree)
    require(len(statements) == 1 and statements[0].type == 'function_declaration', 'one_direct_function_required')
    function = statements[0]
    require(not any(child.type in ('async', '*') for child in function.children), 'synchronous_function_required')
    name = function.child_by_field_name('name')
    require(name is not None and name.type == 'identifier', 'named_function_required')
    return function, raw


def conjunction(node):
    node = unwrap(node)
    if node is not None and node.type == 'binary_expression' and r._operator(node) == '&&':
        return conjunction(node.child_by_field_name('left')) + conjunction(node.child_by_field_name('right'))
    return [node]


def comparison(node, operator):
    node = unwrap(node)
    require(node is not None and node.type == 'binary_expression' and r._operator(node) == operator, 'comparison_mismatch')
    return node.child_by_field_name('left'), node.child_by_field_name('right')


def helper_contract(text):
    function, raw = unit(text)
    parameter = one_parameter(function, raw)
    body = r.children(function.child_by_field_name('body'))
    require(len(body) == 1 and body[0].type == 'return_statement' and len(r.children(body[0])) == 1, 'pure_single_return_helper_required')
    terms = conjunction(r.children(body[0])[0])
    require(len(terms) in (2, 4), 'object_only_helper_contract_required')
    left, right = comparison(terms[0], '===')
    left = unwrap(left)
    require(left is not None and left.type == 'unary_expression' and r._operator(left) == 'typeof'
            and ident(left.child_by_field_name('argument'), parameter, raw) and string(right, raw) == 'object', 'helper_must_reject_functions')
    left, right = comparison(terms[1], '!==')
    require(ident(left, parameter, raw) and right is not None and right.type == 'null', 'helper_must_reject_null')
    if len(terms) == 4:
        require(ident(member(terms[2], 'constructor', raw), parameter, raw), 'constructor_refinement_mismatch')
        tag, value = comparison(terms[3], '===')
        require(string(value, raw) == 'Object', 'object_tag_required')
        slice_function, slice_args = call(tag, raw)
        tag_call = member(slice_function, 'slice', raw)
        require([r.text(arg, raw) for arg in slice_args] == ['8', '-1'], 'object_tag_slice_mismatch')
        tag_function, tag_args = call(tag_call, raw)
        object_to_string = member(tag_function, 'call', raw)
        require(len(tag_args) == 1 and ident(tag_args[0], parameter, raw), 'object_tag_argument_mismatch')
        object_prototype = member(object_to_string, 'toString', raw)
        require(ident(member(object_prototype, 'prototype', raw), 'Object', raw), 'intrinsic_object_tag_required')
    return {'name': r.text(function.child_by_field_name('name'), raw), 'shape': r.shape(function, raw),
            'rejects_functions': True, 'rejects_null': True, 'accepts_plain_witness': True}


def field(node, base, key, raw):
    node = unwrap(node)
    return (node is not None and node.type == 'subscript_expression'
            and ident(node.child_by_field_name('object'), base, raw)
            and ident(node.child_by_field_name('index'), key, raw))


def statement_value(node):
    if node is not None and node.type in ('statement_block', 'else_clause'):
        values = r.children(node)
        require(len(values) == 1, 'one_operation_per_branch_required')
        return statement_value(values[0])
    require(node is not None, 'complete_branch_required')
    return node


def assignment(node, target, source, key, raw):
    node = statement_value(node)
    require(node.type == 'expression_statement' and len(r.children(node)) == 1, 'direct_assignment_statement_required')
    value = r.children(node)[0]
    require(value.type == 'assignment_expression' and r._operator(value) == '='
            and field(value.child_by_field_name('left'), target, key, raw)
            and field(value.child_by_field_name('right'), source, key, raw), 'direct_matching_field_write_required')


def recursive_body(body, self_name, target, source, key, raw):
    statements = r.children(body)
    require(body is not None and body.type == 'statement_block' and len(statements) == 1, 'single_recursive_dispatch_required')
    outer = statements[0]
    require(outer.type == 'if_statement', 'undefined_destination_dispatch_required')
    left, right = comparison(outer.child_by_field_name('condition'), '===')
    left = unwrap(left)
    require(left is not None and left.type == 'unary_expression' and r._operator(left) == 'typeof'
            and field(left.child_by_field_name('argument'), target, key, raw)
            and string(right, raw) == 'undefined', 'undefined_destination_test_mismatch')
    assignment(outer.child_by_field_name('consequence'), target, source, key, raw)
    nested = statement_value(outer.child_by_field_name('alternative'))
    require(nested.type == 'if_statement', 'guarded_recursion_required')
    terms = conjunction(nested.child_by_field_name('condition'))
    require(len(terms) == 3, 'complete_recursive_object_guards_required')
    predicate = None
    for term, owner in zip(terms[:2], (source, target)):
        function, args = call(term, raw)
        require(function is not None and function.type == 'identifier' and len(args) == 1
                and field(args[0], owner, key, raw), 'recursive_predicate_flow_mismatch')
        current = r.text(function, raw)
        require(predicate is None or current == predicate, 'same_object_predicate_required')
        predicate = current
    keys_length, zero = comparison(terms[2], '>')
    keys_call = member(keys_length, 'length', raw)
    keys_function, keys_args = call(keys_call, raw)
    require(ident(member(keys_function, 'keys', raw), 'Object', raw) and len(keys_args) == 1
            and field(keys_args[0], source, key, raw) and r.text(zero, raw) == '0', 'nonempty_source_guard_mismatch')
    recursive = statement_value(nested.child_by_field_name('consequence'))
    require(recursive.type == 'expression_statement' and len(r.children(recursive)) == 1, 'direct_recursive_call_required')
    function, args = call(r.children(recursive)[0], raw)
    require(ident(function, self_name, raw) and len(args) == 2 and field(args[0], target, key, raw)
            and field(args[1], source, key, raw), 'recursive_target_source_arguments_mismatch')
    assignment(nested.child_by_field_name('alternative'), target, source, key, raw)
    require(len({self_name, target, source, key, predicate, 'Object'}) == 6, 'captured_recursive_binding')
    return predicate, r.text(r.children(recursive)[0], raw)


def semantic_side(code, *, protected):
    function, raw = unit(code)
    self_name = r.text(function.child_by_field_name('name'), raw)
    parameters = r.children(function.child_by_field_name('parameters'))
    require(len(parameters) == 2 and all(node.type == 'identifier' for node in parameters), 'two_simple_parameters_required')
    target, source = [r.text(node, raw) for node in parameters]
    statements = r.children(function.child_by_field_name('body'))
    require(len(statements) == (2 if protected else 1), 'extra_unit_effect_or_declaration')
    set_name, values, set_quote, filter_quote = None, None, None, None
    if protected:
        declaration = statements[0]
        values_in_declaration = r.children(declaration)
        require(declaration.type == 'lexical_declaration' and any(child.type == 'const' for child in declaration.children)
                and len(values_in_declaration) == 1 and values_in_declaration[0].type == 'variable_declarator', 'immutable_literal_set_required')
        declarator = values_in_declaration[0]
        name, array = declarator.child_by_field_name('name'), declarator.child_by_field_name('value')
        require(name is not None and name.type == 'identifier' and array is not None and array.type == 'array', 'literal_key_array_required')
        set_name = r.text(name, raw)
        values = [string(node, raw) for node in r.children(array)]
        require(len(values) == 3 and set(values) == set(META)
                and len([node for node in array.children if node.type == ',']) <= len(values), 'exact_reserved_key_set_required')
        set_quote = r.text(declaration, raw)
    expression = statements[-1]
    require(expression.type == 'expression_statement' and len(r.children(expression)) == 1, 'one_enumeration_pipeline_required')
    for_each_function, for_each_args = call(r.children(expression)[0], raw)
    enumeration = member(for_each_function, 'forEach', raw)
    require(len(for_each_args) == 1 and for_each_args[0].type == 'arrow_function', 'one_synchronous_callback_required')
    callback = for_each_args[0]
    require(not any(child.type == 'async' for child in callback.children), 'synchronous_callback_required')
    key = one_parameter(callback, raw)
    if protected:
        filter_function, filter_args = call(enumeration, raw)
        enumeration = member(filter_function, 'filter', raw)
        require(len(filter_args) == 1 and filter_args[0].type == 'arrow_function', 'pure_filter_callback_required')
        predicate = filter_args[0]
        require(not any(child.type == 'async' for child in predicate.children), 'synchronous_filter_required')
        filter_key = one_parameter(predicate, raw)
        index_call, zero = comparison(predicate.child_by_field_name('body'), '<')
        index_function, index_args = call(index_call, raw)
        require(ident(member(index_function, 'indexOf', raw), set_name, raw) and len(index_args) == 1
                and ident(index_args[0], filter_key, raw) and r.text(zero, raw) == '0', 'exact_indexof_domain_predicate_required')
        require(filter_key not in {target, source, self_name, set_name, 'Object'}, 'captured_filter_binding')
        filter_quote = r.text(predicate, raw)
    keys_function, keys_args = call(enumeration, raw)
    require(ident(member(keys_function, 'keys', raw), 'Object', raw) and len(keys_args) == 1
            and ident(keys_args[0], source, raw), 'own_source_keys_required')
    body = callback.child_by_field_name('body')
    helper_name, recursive_quote = recursive_body(body, self_name, target, source, key, raw)
    if protected:
        require(set_name not in {self_name, target, source, key, helper_name, 'Object'}, 'captured_literal_set_binding')
    return {'self': self_name, 'target': target, 'source': source, 'key': key, 'helper': helper_name,
            'body_shape': r.shape(body, raw), 'domains': {value: False for value in META} | {'<other>': True},
            'quotes': {'keys': r.text(enumeration, raw), 'recursion': recursive_quote,
                       'set': set_quote, 'filter': filter_quote}}


def exact_helper(record, side, name, *, provider):
    """Recover the same-module declaration from bounded exact proof source.

    Selected model context can omit it. Any supplied conflicting helper snippet
    remains unsupported. The recovered module/helper never enters prompt data.
    """
    witness = evidence_module_witness.resolve(record, side, provider=provider)
    require(witness['state'] == 'available', 'helper_main_' + witness.get('cause', 'source_unavailable'))
    parsed = ast.parse_snapshot(record.get('language'), witness['module_text'])
    require(parsed is not None, 'helper_module_parse_failure')
    tree, raw = parsed
    pending, count, matches = [(tree, 0)], 0, []
    while pending:
        node, depth = pending.pop()
        count += 1
        require(count <= 12000 and depth <= 96, 'helper_module_analysis_budget_exceeded')
        if node.type == 'function_declaration' and ident(node.child_by_field_name('name'), name, raw):
            matches.append(node)
        pending.extend((child, depth + 1) for child in node.named_children)
    require(len(matches) == 1, 'helper_definition_missing_or_ambiguous')
    helper = {'name': name, 'path': record['file_path_' + side], 'text': r.text(matches[0], raw)}
    context = record.get('context_' + side) or {}
    context = json.loads(context) if isinstance(context, str) else copy.deepcopy(context)
    require(isinstance(context, dict), 'helper_context_required')
    items = [item for item in context.get('items', []) if item.get('kind') in ('callee', 'module_symbol')
             and item.get('name') == name and item.get('path') == record.get('file_path_' + side)]
    require(not items or all(item.get('text') == helper['text'] for item in items), 'selected_helper_source_mismatch')
    context['items'] = [*context.get('items', []), {'kind': 'module_symbol', **helper}]
    snapshot = copy.deepcopy(context.get('snapshot') or {})
    snapshot['files'] = {**snapshot.get('files', {}), helper['path']: witness['binding']['module_sha256']}
    context['snapshot'] = snapshot
    return helper, context, witness['binding']


def coherent_witnesses(record, side, bindings):
    """Do not deduplicate conflicting witnesses for one exact source module."""
    require(isinstance(bindings, list) and len(bindings) == 2, 'main_and_helper_witnesses_required')
    expected = {'record_id': record.get('record_id'), 'repository': record.get('repository_identity'),
                'revision': record.get('relevant_parent_commit' if side == 'before' else 'fix_commit'),
                'side': side, 'path': record.get('file_path_' + side)}
    for binding in bindings:
        require(isinstance(binding, dict) and all(binding.get(key) == value for key, value in expected.items()),
                'module_witness_identity_inconsistent')
        require(isinstance(binding.get('module_sha256'), str) and len(binding['module_sha256']) == 64
                and all(value in '0123456789abcdef' for value in binding['module_sha256'])
                and isinstance(binding.get('grammar'), str) and bool(binding['grammar']), 'incomplete_module_witness')
        require((binding['module_sha256'], binding['grammar']) ==
                (bindings[0]['module_sha256'], bindings[0]['grammar']), 'module_witness_hash_or_grammar_inconsistent')


def implicit_dependencies(record, witness, bindings):
    """Conservative full-module boundary for implicit intrinsic dependencies.

    This does not assert that a deferred function ran. The prototype refuses
    unmodeled prototype/reflection routes outside its already checked helper,
    including literal/alias routes with no explicit intrinsic identifier. It
    still supports ordinary arrays, array methods, fresh returns and literal
    numeric or ordinary string indexing. Unknown computed keys and unmodeled
    destructuring outside the rigid consumed units fail closed.
    """
    source = witness['module_text']
    parsed = ast.parse_snapshot(record.get('language'), source)
    require(parsed is not None, 'implicit_dependency_module_parse_failure')
    tree, raw = parsed
    helper_span, main_span = bindings[1]['byte_span'], bindings[0]['byte_span']
    sensitive = {'__proto__', 'constructor', 'prototype', 'getPrototypeOf', 'setPrototypeOf',
                 'getOwnPropertyDescriptor', 'getOwnPropertyDescriptors', 'defineProperty', 'defineProperties',
                 '__defineGetter__', '__defineSetter__', '__lookupGetter__', '__lookupSetter__'}
    def within(node, span):
        return span[0] <= node.start_byte <= node.end_byte <= span[1]
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        require(count <= 12000 and depth <= 96, 'implicit_dependency_analysis_budget_exceeded')
        pending.extend((child, depth + 1) for child in node.named_children)
        if within(node, helper_span):
            # The authenticated helper contract has exactly the checked pure
            # constructor/tag reads. Its source shape was validated separately.
            continue
        if not within(node, main_span):
            # Destructuring reads properties without a member/subscript node.
            # Reject unmodeled patterns rather than infer arbitrary alias and
            # default-value flows into shared prototypes or constructors.
            require(node.type not in ('object_pattern', 'array_pattern', 'pair_pattern',
                                      'object_assignment_pattern', 'rest_pattern'),
                    'unmodeled_module_destructuring_pattern')
        if node.type == 'member_expression':
            property_name = r.text(node.child_by_field_name('property'), raw)
            require(property_name not in sensitive, 'unmodeled_module_prototype_or_reflection_route')
            require(not ident(node.child_by_field_name('object'), 'Reflect', raw), 'unmodeled_module_reflection_route')
        if node.type == 'subscript_expression' and not within(node, main_span):
            key = unwrap(node.child_by_field_name('index'))
            if key is not None and key.type == 'number':
                value = r.text(key, raw)
                require(value.isdecimal(), 'unmodeled_module_computed_property')
            elif key is not None and key.type == 'string':
                require(string(key, raw) not in sensitive, 'unmodeled_module_prototype_or_reflection_route')
            else:
                raise Unsupported('unmodeled_module_computed_property')
    facts = binding_origins(source, [0, len(raw)], record.get('language'),
                            {'Object', 'Array', 'String', 'Function', 'Reflect'})
    require(facts is not None and not any(fact['origin'] != 'global' or fact['binding_reassigned']
                                        or fact['object_mutated'] or fact['escaped'] for fact in facts.values()),
            'implicit_intrinsic_binding_unsupported')


def analyze(record, *, provider=None):
    result = {'schema': SCHEMA, 'state': 'unsupported', 'security_verdict': None, 'applies': False}
    try:
        require(record.get('language') in ('javascript', 'js') and 'CWE-1321' in (record.get('cwe_ids') or []), 'prototype_mechanism_scope')
        before = semantic_side(record.get('vulnerable_code'), protected=False)
        result['applies'] = True
        after = semantic_side(record.get('fixed_code'), protected=True)
        sides = {'before': before, 'after': after}
        require({key: value for key, value in before.items() if key != 'quotes'} ==
                {key: value for key, value in after.items() if key != 'quotes'}, 'recursive_body_or_binding_roles_changed')
        contracts, helpers, exact_bindings = {}, {}, {}
        proof_record = dict(record)
        for side in ('before', 'after'):
            helpers[side], context, exact_bindings[side] = exact_helper(record, side, sides[side]['helper'], provider=provider)
            proof_record['context_' + side] = json.dumps(context, sort_keys=True)
            contracts[side] = helper_contract(helpers[side]['text'])
            require(contracts[side]['name'] == sides[side]['helper'], 'helper_identity_mismatch')
        require(contracts['before'] == contracts['after'], 'helper_semantics_changed')
        bindings = {}
        for side in ('before', 'after'):
            bound = evidence_binding_proof.verify(proof_record, side, [helpers[side]], provider=provider, validated_rule=True)
            if not bound['valid']:
                return {**result, 'state': 'binding_rejected', 'reason': side + ':' + bound['cause'],
                        'binding_name': bound.get('binding_name')}
            coherent_witnesses(record, side, bound['module_bindings'])
            require(bound['module_bindings'][0] == exact_bindings[side], 'helper_preparation_source_changed')
            # Re-read through the exact source provider and bind the conservative
            # implicit-dependency check to the same main AND helper module.
            witness = evidence_module_witness.resolve(proof_record, side, provider=provider)
            require(witness['state'] == 'available' and witness['binding'] == bound['module_bindings'][0],
                    'implicit_dependency_source_changed')
            implicit_dependencies(record, witness, bound['module_bindings'])
            bindings[side] = bound['module_bindings']
        scope = ('Prototype pollution through this recursive own-string-key merge on separate finite acyclic plain JSON data. '
                 'Custom prototypes, accessors, proxies, symbols, preexisting shared-prototype aliases and arbitrary external intrinsic mutations are outside this scope.')
        preconditions = ['Inputs are separate finite acyclic plain JSON records or arrays with standard prototypes and ordinary data properties.',
                         'The destination itself is not a shared prototype and no own destination value aliases a shared prototype; no accessors, proxies, symbols or modified intrinsic methods participate.',
                         'Benign equivalence excludes __proto__, constructor and prototype. The authenticated same-module object predicate and standard Object/Array/String/Function operations apply.']
        facts = {'method': 'static_closed_world', 'mechanism': 'prototype_pollution', 'rule_id': RULE_ID, 'repair_valid': True,
                 'violated_invariant': 'Own untrusted string keys must not select a shared inherited prototype as the destination of a recursive merge.',
                 'attacker_control': 'The supplied ' + before['source'] + ' JSON record controls own property names and nested JSON values.',
                 'condition_scope': scope, 'preconditions': preconditions,
                 'supported_impact': 'An own JSON-parsed __proto__ entry with a nonempty object value makes the vulnerable merge recurse into Object.prototype and assign attacker-selected nested properties.',
                 'behavioral_intent': 'Preserve the unchanged recursive operations and their ordinary nonreserved-key behavior while preventing shared-prototype mutation.',
                 'evidence_spans': [{'quote': before['quotes']['keys']}, {'quote': before['quotes']['recursion']}],
                 'fixed_review': {'scope': scope,
                                  'safeguard': 'The immutable literal reserved-key set and indexOf predicate reject all three meta-key domains before every destination access, recursive call and write. The bound predicate rejects null and inherited functions; the unchanged recursive body preserves the invariant for ordinary JSON subtrees.',
                                  'evidence': [{'quote': after['quotes']['set']}, {'quote': after['quotes']['filter']}]}}
        return {**result, 'state': 'proved', 'record_id': record.get('record_id'),
                'before_binding_valid': True, 'after_binding_valid': True, 'module_bindings': bindings,
                'module_witness_consistency': True,
                'implicit_dependency_boundary': 'restricted-module-prototype-syntax/2',
                'reserved_key_domains': sides['after']['domains'],
                'helper_rejects_functions': True, 'helper_rejects_null': True,
                'facts': facts, 'preconditions': preconditions,
                'induction': 'The filter eliminates every reserved-key domain before all destination access. Other inherited Object methods are functions rejected by the bound predicate. Own destinations remain JSON data without shared-prototype aliases. The unchanged recursive call consumes smaller acyclic source subtrees; direct ordinary writes and recursive steps preserve this invariant.',
                'limitations': ['Only policy 3 may consume these checked source facts after its source permission, integrity and partition gates. The analyzer itself issues no label or receipt.',
                                'The conservative module syntax boundary rejects unmodeled prototype and reflection routes. It does not prove global intrinsic integrity against arbitrary external code; the unmodified-intrinsics precondition remains required.']}
    except (Unsupported, ValueError, TypeError) as error:
        return {**result, 'reason': str(error)}
