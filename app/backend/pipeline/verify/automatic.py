"""Source fingerprints and static analysis for evidence admission."""
from __future__ import annotations

import hashlib
import json
import re
from .proof_ledger import digest

SCHEMA = "trusted-execution/2"
MECHANISMS = ()
_SINKS = {"exec", "execSync", "execFile", "execFileSync", "spawn", "spawnSync"}


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def source_binding(record):
    before, after = record.get("vulnerable_code") or "", record.get("fixed_code") or ""
    binding = {"record_id": record.get("record_id"), "pair": record.get("raw_pair_key"),
               "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
               "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
               "parent": record.get("relevant_parent_commit"), "fix": record.get("fix_commit"),
               "before_context_sha256": digest(_json(record.get("context_before"))),
               "after_context_sha256": digest(_json(record.get("context_after"))),
               "symbol": record.get("symbol"), "language": record.get("language"),
               "path_before": record.get("file_path_before"), "path_after": record.get("file_path_after"),
               "repository_identity": record.get("repository_identity"), "ecosystem": record.get("ecosystem"),
               "cwe_ids": _json(record.get("cwe_ids")), "provenance_status": record.get("provenance_status"),
               "source_record_hashes": _json(record.get("source_record_hashes")), "file_blob_hashes": _json(record.get("file_blob_hashes"))}
    return {**binding, "source_sha256": digest(binding)}


def _walk(node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _text(node, data):
    return data[node.start_byte:node.end_byte].decode() if node is not None else ""


def _parse(code, language="javascript"):
    from app.backend.pipeline import evidence_ast
    parsed = evidence_ast.parse(language, code)
    return parsed if parsed is not None and not parsed[0].has_error else None


def _import_bindings(record, side):
    """Direct bindings in the actual subject file, never a similarly named helper."""
    context = _json(record.get("context_" + side))
    items = context.get("items", []) if isinstance(context, dict) else []
    result = {}
    for item in items:
        if not isinstance(item, dict) or item.get("kind") != "imports" or item.get("path") != record.get("file_path_" + side):
            continue
        text = item.get("text", "")
        parsed = _parse(text, "typescript" if record.get("language") in ("typescript", "ts") else "javascript")
        if not parsed:
            continue
        root, data = parsed
        for node in root.named_children:
            raw = _text(node, data)
            declarations = []
            if node.type == "import_statement":
                source = node.child_by_field_name("source")
                module = _text(source, data).strip("\"'").removeprefix("node:")
                match = re.match(r"import\s*\{([^}]+)\}", raw)
                if match:
                    for entry in match[1].split(","):
                        parts = entry.strip().split(" as ")
                        if parts[0] and len(parts) <= 2:
                            declarations.append((parts[-1].strip(), parts[0].strip()))
                else:
                    match = re.match(r"import\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s+from", raw)
                    if match:
                        declarations.append((match[1], "namespace"))
            elif node.type in ("lexical_declaration", "variable_declaration"):
                for declaration in node.named_children:
                    if declaration.type != "variable_declarator":
                        continue
                    value = declaration.child_by_field_name("value")
                    imported_member = None
                    if value is not None and value.type == "member_expression":
                        imported_member = _text(value.child_by_field_name("property"), data)
                        value = value.child_by_field_name("object")
                    if value is None or value.type != "call_expression" or _text(value.child_by_field_name("function"), data) != "require":
                        continue
                    args = value.child_by_field_name("arguments").named_children
                    if len(args) != 1 or args[0].type != "string":
                        continue
                    module = _text(args[0], data).strip("\"'").removeprefix("node:")
                    name = declaration.child_by_field_name("name")
                    if name.type == "identifier":
                        if imported_member is None or re.fullmatch(r"[A-Za-z_$][\w$]*", imported_member):
                            declarations.append((_text(name, data), "namespace" if imported_member == "posix" and module == "path" else imported_member or "namespace"))
                    elif name.type == "object_pattern" and imported_member is None:
                        for entry in _text(name, data)[1:-1].split(","):
                            parts = entry.strip().split(":")
                            if parts[0] and len(parts) <= 2:
                                declarations.append((parts[-1].strip(), parts[0].strip()))
                    for local, imported in declarations:
                        pair = (module, imported)
                        result[local] = pair if local not in result or result[local] == pair else None
                    declarations = []
            else:
                continue
            for local, imported in declarations:
                pair = (module, imported)
                result[local] = pair if local not in result or result[local] == pair else None
    return result

def plan_case(record, *, image_id=None):
    return None


def diagnose_case(record):
    from ..evidence_rules import _has_cwe
    codes = [record.get("vulnerable_code") or "", record.get("fixed_code") or ""]
    joined = "\n".join(codes)
    if re.search(r"\b(?:exec|execSync|execFile|execFileSync|spawn|spawnSync)\s*\(", joined) and any(_has_cwe(record, cwe) for cwe in ('CWE-77', 'CWE-78', 'CWE-88')):
        mechanism, names = "command_injection", _SINKS
    elif re.search(r"\b(?:path\.(?:resolve|join)|normalize|join|resolve)\s*\(", joined) and _has_cwe(record, 'CWE-22'):
        mechanism, names = "path_traversal", {"path", "join", "resolve", "relative", "isAbsolute", "normalize", "sep"}
    elif _has_cwe(record, 'CWE-1321') and re.search(r"\[[^]]+\]\s*=", joined):
        mechanism, names = "prototype_pollution", set()
    else:
        return {"case": None, "mechanism": None, "missing_facts": [], "reason": "unsupported_mechanism"}
    from .closure_diagnostics import missing_values
    gaps = []
    module = "child_process" if mechanism == "command_injection" else "path"
    for side in ("before", "after"):
        gaps.extend(missing_values(record, side, imports=_import_bindings(record, side),
                                   mechanism_names=names, module=module))
    return {"case": None, "mechanism": mechanism, "missing_facts": sorted(set(gaps)),
            "reason": "missing_mechanism_closure" if gaps else "execution_unavailable"}


def harness_sha256():
    return None


def trusted_receipt_for(record, binding=None, *, ledger=None):
    return None


def run_case(record, spec, **kwargs):
    raise RuntimeError("Execution-based vulnerability checks are unavailable in this distribution.")
