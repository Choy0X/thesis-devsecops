"""Read named ES export identity without executing a source module."""
from __future__ import annotations


def bindings(source,language):
    from . import evidence_ast,evidence_rules as r
    parsed=evidence_ast.parse(language,source)
    if parsed is None:return {}
    root,data=parsed;result={}
    def add(name,value):
        result[name]=value if name not in result else None
    for statement in r.children(root):
        if statement.type!='export_statement':continue
        if any(n.type=='type' for n in statement.children):continue
        if any(n.type=='default' for n in statement.children):continue
        declaration=statement.child_by_field_name('declaration')
        if declaration is not None:
            if declaration.type=='function_declaration':
                name=r.text(declaration.child_by_field_name('name'),data)
                add(name,{'local':name})
            elif declaration.type=='lexical_declaration' and any(n.type=='const' for n in declaration.children):
                for node in r.children(declaration):
                    if node.type=='variable_declarator' and node.child_by_field_name('name').type=='identifier':
                        name=r.text(node.child_by_field_name('name'),data);add(name,{'local':name})
            continue
        module=r.text(statement.child_by_field_name('source'),data)
        for node in r.walk(statement):
            if node.type!='export_specifier' or any(n.type=='type' for n in node.children):continue
            local=r.text(node.child_by_field_name('name'),data)
            public=r.text(node.child_by_field_name('alias'),data) or local
            add(public,{'local':local,**({'module':module[1:-1]} if module else {})})
    return result


def local_definition(source,language,name):
    """Only an unambiguous same-name, unmodified direct named export qualifies."""
    from . import evidence_ast,evidence_rules as r
    binding=bindings(source,language).get(name)
    if binding!={'local':name}:return None
    parsed=evidence_ast.parse(language,source)
    if parsed is None:return None
    root,data=parsed;found=[]
    for statement in r.children(root):
        node=statement.child_by_field_name('declaration') if statement.type=='export_statement' else statement
        if node is None:continue
        if node.type not in ('function_declaration','lexical_declaration','import_statement','export_statement',
                             'interface_declaration','type_alias_declaration','empty_statement'):
            return None
        if node.type=='lexical_declaration':
            for declaration in r.children(node):
                value=declaration.child_by_field_name('value') if declaration.type=='variable_declarator' else None
                if value is not None and not _passive_initialization(value):return None
        if node.type=='function_declaration' and r.text(node.child_by_field_name('name'),data)==name:
            found.append(r.text(node,data))
        elif node.type=='lexical_declaration' and any(n.type=='const' for n in node.children):
            if any(n.type=='variable_declarator' and r.text(n.child_by_field_name('name'),data)==name for n in r.children(node)):
                found.append(r.text(node,data))
    for node in r.walk(root):
        if node.type in ('assignment_expression','augmented_assignment_expression','update_expression'):
            target=node.child_by_field_name('left') or node.child_by_field_name('argument')
            if target is not None and target.type=='identifier' and r.text(target,data)==name:return None
    return found[0] if len(found)==1 else None


def _passive_initialization(node):
    if node.type in ('arrow_function','function_expression'):return True
    if node.type in ('call_expression','new_expression','assignment_expression','augmented_assignment_expression',
                      'update_expression','await_expression','yield_expression'):return False
    return all(_passive_initialization(child) for child in node.named_children)
