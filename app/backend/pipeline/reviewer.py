"""Read-only source review using the v40 model's discovery protocol."""

from __future__ import annotations

from collections.abc import Callable
import difflib
import re

from app.backend.pipeline import funcsplit, grounded_views, grounded_views_v3
from training.finetune import grounded_claim_gate, repair_edits, security_serialization_v5 as protocol

MODEL_NAME = "aivf/reviewer-v40"
MAX_FUNCTION_CHARS = 4000
MAX_OUTPUT_TOKENS = 512
REPAIR_OUTPUT_TOKENS = 896  # the evaluation's repair limit (v40a report max_new_tokens)
_LOCATION = re.compile(r"(?:\blines?\s+|(?<=:))(\d+)(?:\s*(?:-|to)\s*(\d+))?", re.I)
# Lines outside every function that are not code worth a review: blank, comments, imports and package lines.
_NOT_REVIEWABLE = re.compile(r"\s*($|#|//|/\*|\*|--|import\s|from\s+\S+\s+import\s|package\s|using\s|use\s|require\b|[{}()\[\];,]+\s*$)")


class ReviewStopped(RuntimeError):
    """The caller stopped inference before all functions could be reviewed."""


def _findings(path: str, function: str, first_line: int, answer: dict, shown: str):
    normalized, _ = grounded_claim_gate.normalize_quotes(answer, shown)
    # Model answers use the evaluation's non-strict gate: a name that is not shown is reported by check() but
    # does not withhold the finding; a hard problem does.
    _, problems = grounded_claim_gate.gate(normalized, shown, strict=False)
    quotes = {quote for quote in (item["quote"].strip() for item in normalized["evidence"])
              if len(quote) >= grounded_claim_gate.MIN_QUOTE}
    matching_lines = {number: text.strip() for number, text in grounded_claim_gate.numbered_lines(shown).items()
                      if text.strip() in quotes}

    for finding in normalized["findings"]:
        location = _LOCATION.search(finding.get("location") or "")
        candidates = matching_lines
        if location:
            first = int(location[1])
            last = int(location[2]) if location[2] else first
            candidates = {line: quote for line, quote in matching_lines.items() if first <= line <= last}
        claimed, quote = next(iter(candidates.items())) if len(candidates) == 1 else (None, "")
        weakness = finding.get("violated_invariant") or ""
        reasons = list(problems)
        if not quote:
            reasons.append("Evidence does not identify a unique source line at the reported location.")
        if weakness not in protocol.CATALOGUE_TITLES:
            reasons.append("The reported class is outside the review catalogue.")
            weakness = ""
        yield {"path": path, "function": function, "line": first_line + claimed - 1 if quote else None,
               "class": weakness, "quote": quote, "explanation": finding["summary"] if not reasons else "",
               "gate_ok": not reasons, "gate_reasons": reasons}


def _uncovered_code(text: str, units) -> list[list[int]]:
    """Line ranges with code outside every function unit (module code is never shown to the reviewer)."""
    covered = set()
    for details in units.info.values():
        covered.update(range(details.lines[0], details.lines[1] + 1))
    lines = [number for number, line in enumerate(text.split("\n"), start=1)
             if number not in covered and not _NOT_REVIEWABLE.match(line)]
    ranges: list[list[int]] = []
    for number in lines:
        if ranges and ranges[-1][1] == number - 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    return ranges


def _unique(findings: list[dict]) -> list[dict]:
    """One finding per source line and class: a nested function is shown in its own unit and in its parent's."""
    kept, seen = [], set()
    for row in findings:
        key = (row["line"], row["class"]) if row["line"] is not None else None
        if key is not None and key in seen:
            continue
        if key is not None:
            seen.add(key)
        kept.append(row)
    return kept


def review_source(path: str, text: str, language: str, *, chat: Callable[[list[dict[str, str]]], str]) -> dict:
    """Review each supported function using only the code shown in its discovery view. Module code outside every
    function is reported as not reviewed."""
    findings, skipped = [], []
    reviewed = 0
    try:
        units = funcsplit.parse_units(language, path, text.replace("\r\n", "\n"))
    except funcsplit.Unavailable as error:
        return {"findings": findings, "skipped": [{"reason": str(error)}], "reviewed": reviewed}
    if units.errors or units.recovered:
        skipped.append({"reason": "partial_source_parse"})
    uncovered = _uncovered_code(text.replace("\r\n", "\n"), units)
    if uncovered:
        skipped.append({"reason": "top_level_code_not_reviewed", "lines": uncovered})

    scope = grounded_views_v3.scope_text("open", [])
    for function, (code, _) in units.items():
        if len(code) > MAX_FUNCTION_CHARS:
            skipped.append({"function": function, "reason": "too_long"})
            continue
        if not grounded_views._numberable(code):
            skipped.append({"function": function, "reason": "nonstandard_line_breaks"})
            continue
        context = grounded_views._context(path, language, code, scope, numbered=True)
        try:
            output = chat(protocol.messages("discovery", context))
        except ReviewStopped as error:
            skipped.append({"function": function, "reason": "review_stopped"})
            return {"findings": _unique(findings), "skipped": skipped, "reviewed": reviewed, "stopped": str(error)}
        answer, problems = protocol.parse_response("discovery", output or "")
        if problems:
            skipped.append({"function": function, "reason": "invalid_answer"})
            continue
        reviewed += 1
        if answer["status"] == "insufficient_context":
            skipped.append({"function": function, "reason": "insufficient_context"})
        elif answer["status"] == "finding":
            shown = grounded_views.number_lines(code)
            findings.extend(_findings(path, function, units.info[function].lines[0], answer, shown))
    return {"findings": _unique(findings), "skipped": skipped, "reviewed": reviewed}


def repair_source(path, original, finding, *, chat) -> str:
    """Propose a local repair in the exact reviewed function and bind its edit offsets."""
    from app.backend.pipeline.ingest.codemine import language_of

    source = original.replace("\r\n", "\n")
    language = language_of(path)
    location, title = finding["line"], finding["rule"]
    if not language or title not in protocol.CATALOGUE_TITLES or not grounded_views._numberable(source):
        raise ReviewStopped("This source or weakness is outside the reviewer's repair protocol.")
    try:
        units = funcsplit.parse_units(language, path, source)
    except funcsplit.Unavailable as error:
        raise ReviewStopped("The reviewed function could not be recovered.") from error
    expected = finding.get("function")
    targets = [name for name, details in units.info.items()
               if details.lines[0] <= location <= details.lines[1] and (not expected or name == expected)]
    if len(targets) != 1:
        raise ReviewStopped("The reviewed line no longer identifies one source function.")
    name = targets[0]
    first, last = units.info[name].lines
    code = units[name][0]
    if len(code) > MAX_FUNCTION_CHARS or code.splitlines() != source.splitlines()[first - 1:last]:
        raise ReviewStopped("The source function does not have exact editable line boundaries.")
    context = grounded_views._context(path, language, code, f"Review only for: {title}.", numbered=True)
    answer, problems = protocol.parse_response("repair", chat(protocol.messages("repair", context)) or "")
    if problems or answer.get("status") != "finding":
        raise ReviewStopped("The reviewer did not return a grounded repair.")
    proof = tuple(_findings(path, name, first, answer, grounded_views.number_lines(code)))
    if not any(row["gate_ok"] and row["line"] == location and row["class"] == title for row in proof):
        raise ReviewStopped("The repair answer did not cite the reviewed finding.")
    edits, problem = repair_edits.parse_edits(answer.get("patch"))
    if problem or repair_edits.check_edits(edits, code)["state"] != "applies_exactly":
        raise ReviewStopped("The suggested edits did not match the reviewed function exactly.")
    added = [line for edit in edits for line in edit["new"]]
    if grounded_views_v3._unseen_names(added, code, language):
        raise ReviewStopped("The suggested repair used identifiers absent from the reviewed source.")
    rebased = [{**edit, "start": edit["start"] + first - 1} for edit in edits]
    if repair_edits.check_edits(rebased, source)["state"] != "applies_exactly":
        raise ReviewStopped("The function edits did not match the saved source file.")
    updated = repair_edits.apply_edits(rebased, source)
    if not source.endswith("\n"):
        updated = updated.removesuffix("\n")
    diff = difflib.unified_diff(source.splitlines(keepends=True), updated.splitlines(keepends=True),
                                fromfile=f"a/{path}", tofile=f"b/{path}", n=2)
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff)


def local_reviewer_tag() -> str:
    return local_reviewer()["tag"]


def local_reviewer() -> dict:
    """The installed local reviewer: its Ollama tag, the digest of those weights and the prompt protocol."""
    from app.backend.pipeline.gateway import client as gateway
    from app.backend.pipeline.gateway.errors import GatewayError
    from app.backend.pipeline.ollama import catalog, client as ollama

    if not ollama.local_endpoint():
        raise ReviewStopped("Project review requires Ollama on this computer.")
    try:
        gateway.ensure_running()
        row = next((row for row in ollama.tags(timeout=3.0, include_metadata=True)
                    if isinstance(row.get("name"), str) and catalog.normalized_tag(row["name"]) == MODEL_NAME), None)
        if not row:
            raise ReviewStopped(f"Install {MODEL_NAME} first with python -m tools.export_reviewer_to_ollama.")
        details = ollama.show(row["name"], timeout=3.0)
        if (row.get("remote_host") or row.get("remote_model") or details.get("remote_host")
                or details.get("remote_model") or "completion" not in (details.get("capabilities") or [])):
            raise ReviewStopped(f"{MODEL_NAME} must be an installed local reviewer with text generation.")
    except (GatewayError, ollama.OllamaError) as error:
        raise ReviewStopped(str(error)) from error
    return {"tag": row["name"], "digest": str(row.get("digest") or "").removeprefix("sha256:"), "protocol": protocol.PROTOCOL}


def repair_local(path, original, finding) -> tuple[str, dict]:
    from app.backend.pipeline import llm_client
    from app.backend.pipeline.gateway import client as gateway
    from app.backend.pipeline.gateway.errors import GatewayError

    tag = local_reviewer_tag()
    try:
        schema = {"type": "json_schema", "json_schema": {"name": "repair", "schema": protocol.RESPONSE_SCHEMAS["repair"]}}
        with llm_client.local_client(timeout=180) as model, gateway.inference_session("a reviewer repair"):
            def ask(messages):
                answer = model.chat.completions.create(model=tag, messages=messages, temperature=0,
                                                       max_tokens=REPAIR_OUTPUT_TOKENS, response_format=schema)
                return answer.choices[0].message.content or ""

            diff = repair_source(path, original, finding, chat=ask)
    except GatewayError as error:
        raise ReviewStopped(str(error)) from error
    except llm_client.request_error_type() as error:
        raise ReviewStopped("Ollama did not finish the patch request.") from error
    return diff, {"model": tag, "host": "Ollama local", "local": True}
