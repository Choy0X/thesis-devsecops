"""Portable logical collection options and recorded source engine provenance."""
import json
from .operations import OperationFailure

LOGICAL = frozenset({"validator", "validationLevel", "validationAction"})
PHYSICAL = frozenset({"storageEngine"})


def pack_options(options):
    from .codec import encode_document
    text, types = encode_document(options)
    validator, _ = encode_document(options.get("validator", {}))
    return validator, json.dumps({"format": "ztsa-typed-options/1", "doc": text, "types": types})


def unpack_options(raw, validator_raw="{}"):
    from .codec import decode_document
    options = json.loads(raw)
    if options.get("format") == "ztsa-typed-options/1":
        options = decode_document(options["doc"], options["types"])
    # Old databases held the validator separately with plain JSON options.
    if "validator" not in options:
        validator = json.loads(validator_raw)
        if validator: options["validator"] = validator
    return options


def check_options(options):
    if not isinstance(options, dict):
        raise OperationFailure("Collection options must be a document")
    unknown = set(options) - LOGICAL - PHYSICAL
    if unknown:
        raise OperationFailure("Unsupported collection options: " + ", ".join(sorted(unknown)))
    if not isinstance(options.get("validator", {}), dict):
        raise OperationFailure("Collection validator must be a document")
    if options.get("validationLevel", "strict") not in ("strict", "moderate", "off"):
        raise OperationFailure("Unsupported collection validationLevel")
    if options.get("validationAction", "error") != "error":
        raise OperationFailure("Unsupported collection validationAction")
    from .validation import validate_schema
    validate_schema(options.get("validator", {}))
    return options


def logical_signature(options):
    check_options(options)
    return {"validator": options.get("validator", {}),
            "validationLevel": options.get("validationLevel", "strict"),
            "validationAction": options.get("validationAction", "error")}
