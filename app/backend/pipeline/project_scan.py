"""Project model scan: every unit of a project's code reviewed by the fine-tuned reviewer in two passes.


1. Units: functions, windows of long functions, and module code outside every function.
2. Exposure: which units read external input, and how far each unit is from one in the project's call graph.
3. Discovery: the training prompt with the open scope, read leniently (the first JSON object of the answer).
4. Location: each finding is placed on a source line by its location and quotes, whitespace-insensitively.
5. Confirmation: each located finding is asked again in the single-class form; it is confirmed, rejected or
   left unconfirmed. Rejected and unlocated candidates are counted and kept, never shown as results.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import json
import re

from app.backend.pipeline import funcsplit, grounded_views, grounded_views_v3
from training.finetune import security_serialization_v5 as protocol

MAX_UNIT_CHARS = 4000          # the evaluation's longest views; a longer unit is reviewed in windows
WINDOW_LINES = 120
WINDOW_OVERLAP = 20
CONFIRM_DISTANCE = 2           # lines between the open finding and the scoped finding that still confirm it
MIN_QUOTE = 8                  # a shorter quote is too common to place a finding
STATE_ORDER = {"confirmed": 0, "unconfirmed": 1}
CWE_OF = {title: cwe for cwe, title in protocol.CATALOGUE}

_LOCATION = re.compile(r"(?:\blines?\s+|(?<=:))(\d+)(?:\s*(?:-|to)\s*(\d+))?", re.I)
_NOT_CODE = re.compile(r"\s*($|#|//|/\*|\*|--|import\s|from\s+\S+\s+import\s|package\s|using\s|use\s|require\b|[{}()\[\];,]+\s*$)")
_CALL = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
# Reads of data from outside the process, by kind; a unit that matches one is an entry point for exposure.
_INPUT = [
    ("an HTTP request", re.compile(r"\b(?:req|request|r|ctx)\.(?:body|query|params|headers|cookies|form|args|json|data|files|"
                                   r"GET|POST|FormValue|PostFormValue|URL|Body|Header|PathValue)\b|\$_(?:GET|POST|REQUEST|COOKIE|"
                                   r"SERVER|FILES)\b|http\.Request\b|HttpServletRequest\b|@RequestParam\b")),
    ("a network connection", re.compile(r"\b(?:net\.Conn|socket\.|recv\(|recvfrom\(|ReadFull\(|bufio\.NewReader\(|"
                                        r"\.Accept\(|WebSocket|websocket)")),
    ("command-line arguments", re.compile(r"\b(?:sys\.argv|process\.argv|os\.Args|flag\.String|argparse\b|ARGV\b)")),
    ("environment variables", re.compile(r"\b(?:os\.environ|os\.getenv|os\.Getenv|process\.env|getenv\()")),
    ("standard input", re.compile(r"\b(?:sys\.stdin|os\.Stdin|input\(|readline\()")),
    ("received data being decoded", re.compile(r"\b(?:json\.loads|pickle\.loads|yaml\.load|json\.Unmarshal|"
                                               r"JSON\.parse|unserialize\(|ObjectInputStream)")),
]


from app.backend.pipeline.reviewer import ReviewStopped  # noqa: E402  (one stop signal for every reviewer caller)


@dataclass(frozen=True)
class Unit:
    path: str
    language: str
    name: str
    kind: str          # function, window or module
    first: int         # first and last line in the file, 1-based
    last: int
    code: str

    @property
    def key(self) -> str:
        return f"{self.path}:{self.first}-{self.last}"

    @property
    def short_name(self) -> str:
        return re.split(r"[.:#]", self.name)[-1]


# ---------------------------------------------------------------- 1. units

def _windows(path, language, name, kind, lines, first, last):
    """The lines first..last as one unit, or as overlapping windows when they are too long for one view."""
    code = "\n".join(lines[first - 1:last])
    if len(code) <= MAX_UNIT_CHARS and last - first < WINDOW_LINES:
        yield Unit(path, language, name, kind, first, last, code)
        return
    start = first
    while start <= last:
        end = min(last, start + WINDOW_LINES - 1)
        while end > start and len("\n".join(lines[start - 1:end])) > MAX_UNIT_CHARS:
            end -= 1
        yield Unit(path, language, f"{name} (lines {start}-{end})", "window", start, end, "\n".join(lines[start - 1:end]))
        if end >= last:
            return
        start = max(start + 1, end - WINDOW_OVERLAP + 1)


def units_of(path: str, text: str, language: str) -> tuple[list[Unit], list[dict]]:
    """Every reviewable unit of one file, and what could not be split."""
    source = text.replace("\r\n", "\n")
    lines = source.split("\n")
    try:
        parsed = funcsplit.parse_units(language, path, source)
    except funcsplit.Unavailable as error:
        return [], [{"path": path, "reason": "unsupported_source", "detail": str(error)}]
    skipped = [{"path": path, "reason": "partial_source_parse"}] if parsed.errors or parsed.recovered else []
    units, covered = [], set()
    for name, details in parsed.info.items():
        first, last = details.lines
        covered.update(range(first, last + 1))
        if not grounded_views._numberable("\n".join(lines[first - 1:last])):
            skipped.append({"path": path, "function": name, "reason": "nonstandard_line_breaks"})
            continue
        units.extend(_windows(path, language, name, "function", lines, first, last))
    block: list[int] = []
    for number in range(1, len(lines) + 2):
        if number <= len(lines) and number not in covered:
            block.append(number)
            continue
        code_lines = [n for n in block if not _NOT_CODE.match(lines[n - 1])]
        if code_lines:
            units.extend(_windows(path, language, "module code", "module", lines, code_lines[0], code_lines[-1]))
        block = []
    return units, skipped


# ---------------------------------------------------------------- 2. exposure

def exposure(units: list[Unit]) -> dict[str, dict]:
    """For every unit: whether it reads external input, and its distance in calls from a unit that does.

    The call graph is name-based (a unit calls another when its code calls that unit's short name), so it can join
    two functions that only share a name; it orders the review and the results and never removes a finding."""
    reads = {}
    for unit in units:
        reason = next((label for label, pattern in _INPUT if pattern.search(unit.code)), None)
        if reason:
            reads[unit.key] = reason
    by_name: dict[str, list[Unit]] = {}
    for unit in units:
        if unit.kind == "function":
            by_name.setdefault(unit.short_name, []).append(unit)
    callees = {unit.key: [callee for name in set(_CALL.findall(unit.code)) for callee in by_name.get(name, [])
                          if callee.key != unit.key] for unit in units}
    distance, via = {key: 0 for key in reads}, {}
    queue = deque(reads)
    by_key = {unit.key: unit for unit in units}
    while queue:
        key = queue.popleft()
        for callee in callees.get(key, []):
            if callee.key not in distance:
                distance[callee.key] = distance[key] + 1
                via[callee.key] = by_key[key]
                queue.append(callee.key)
    out = {}
    for unit in units:
        if unit.key in reads:
            out[unit.key] = {"distance": 0, "reason": f"reads {reads[unit.key]}"}
        elif unit.key in distance:
            caller = via[unit.key]
            out[unit.key] = {"distance": distance[unit.key],
                             "reason": f"called from {caller.name} ({caller.path}:{caller.first})"
                                       + (", which reads external input" if distance[unit.key] == 1 else "")}
        else:
            out[unit.key] = {"distance": None, "reason": "no path from external input was found in this project"}
    return out


# ---------------------------------------------------------------- 3. discovery and reading answers

def read_answer(text: str) -> dict | None:
    """The protocol answer in a model reply: the first JSON object, with code fences or prose around it allowed."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        answer, problems = protocol.parse_response("discovery", json.dumps(value))
        return None if problems else answer
    return None


def ask_unit(unit: Unit, scope: str, ask: Callable[[list[dict]], str]) -> tuple[dict | None, str]:
    context = grounded_views._context(unit.path, unit.language, unit.code, scope, numbered=True)
    raw = ask(protocol.messages("discovery", context)) or ""
    return read_answer(raw), raw


# ---------------------------------------------------------------- 4. location

def _squash(text: str) -> str:
    return " ".join((text or "").split())


def locate(finding: dict, evidence: list[dict], unit: Unit) -> tuple[int | None, str]:
    """(line in the file, source text of that line) for one finding, or (None, "") when it cannot be placed.

    Quotes are compared without regard to spacing, and a quote may cover part of a line or run over several lines.
    The location text narrows the choice; a location alone places the finding when no quote matches."""
    shown = unit.code.split("\n")
    span = None
    match = _LOCATION.search(finding.get("location") or "")
    if match:
        first = int(match[1])
        last = int(match[2]) if match[2] else first
        if 1 <= first <= len(shown):
            span = (first, min(max(first, last), len(shown)))
    quotes = [_squash(item.get("quote")) for item in evidence if len(_squash(item.get("quote"))) >= MIN_QUOTE]
    hits = []
    for number, line in enumerate(shown, start=1):
        text = _squash(line)
        if text and any(quote in text or (len(text) >= MIN_QUOTE and text in quote) for quote in quotes):
            hits.append(number)
    if hits:   # a matching quote outranks the location number; the location picks among several matches
        chosen = min(hits, key=lambda n: 0 if span is None or span[0] <= n <= span[1] else min(abs(n - span[0]), abs(n - span[1])))
    else:
        chosen = span[0] if span else None
    if chosen is None:
        return None, ""
    return unit.first + chosen - 1, shown[chosen - 1].strip()


# ---------------------------------------------------------------- 5. confirmation

def confirm(candidate: dict, unit: Unit, ask) -> dict:
    """Ask again about the candidate's class only. confirmed / rejected / unconfirmed, with the reason."""
    # The single-class line of the training views, with the v5 catalogue title (grounded_views_v3.single_scope reads
    # the older catalogue and lacks classes the v40 model was trained on, such as CWE-20 and CWE-306).
    answer, raw = ask_unit(unit, f"Review only for: {candidate['class']}.", ask)
    if answer is None:
        return {"state": "unconfirmed", "reason": "The class-scoped re-check returned no valid answer."}
    if answer["status"] == "no_finding":
        return {"state": "rejected", "reason": "The class-scoped re-check found nothing."}
    if answer["status"] != "finding":
        return {"state": "unconfirmed", "reason": "The class-scoped re-check asked for more context."}
    lines = [locate(finding, answer.get("evidence") or [], unit)[0] for finding in answer["findings"]]
    if any(line is not None and abs(line - candidate["line"]) <= CONFIRM_DISTANCE for line in lines):
        return {"state": "confirmed", "reason": "The class-scoped re-check reported the same place."}
    return {"state": "unconfirmed", "reason": "The class-scoped re-check reported a different place."}


# ---------------------------------------------------------------- the scan

def scan(files: list[dict], ask: Callable[[list[dict]], str], *, progress=None) -> dict:
    """files: [{path, text, language, sha256}]. ask(messages) returns the model's raw reply or raises ReviewStopped."""
    units, skipped = [], []
    for item in files:
        found, problems = units_of(item["path"], item["text"], item["language"])
        units.extend(found)
        skipped.extend(problems)
    reach = exposure(units)
    hashes = {item["path"]: item.get("sha256") for item in files}
    order = sorted(units, key=lambda u: (reach[u.key]["distance"] is None, reach[u.key]["distance"] or 0, u.path, u.first))
    candidates, unlocated, invalid, reviewed = [], [], 0, 0
    stopped = None
    for index, unit in enumerate(order):
        if progress:
            progress({"phase": "model_review", "message": f"Reviewing {unit.path}:{unit.first} ({index + 1} of {len(order)})",
                      "path": unit.path, "done": index, "total": len(order)})
        try:
            answer, _ = ask_unit(unit, grounded_views_v3.OPEN_SCOPE, ask)
            reviewed += 1
            if answer is None:
                invalid += 1
                continue
            if answer["status"] != "finding":
                continue
            for finding in answer["findings"]:
                title = finding.get("violated_invariant") or ""
                line, quote = locate(finding, answer.get("evidence") or [], unit)
                row = {"path": unit.path, "function": unit.name, "unit": unit.kind, "line": line, "class": title,
                       "cwe": CWE_OF.get(title), "quote": quote, "explanation": finding.get("summary") or "",
                       "attacker_control": finding.get("attacker_control") or "", "sha256": hashes.get(unit.path),
                       "exposure": reach[unit.key]}
                if line is None or title not in CWE_OF:
                    unlocated.append({**row, "reason": "outside the review catalogue" if title not in CWE_OF
                                      else "not placed on a line of the reviewed code"})
                    continue
                row["confirmation"] = confirm(row, unit, ask)
                row["state"] = row["confirmation"]["state"]
                candidates.append(row)
        except ReviewStopped as error:
            stopped = str(error)
            break
    merged: dict[tuple, dict] = {}
    for row in candidates:
        key = (row["path"], row["line"], row["class"])
        if key not in merged or STATE_ORDER.get(row["state"], 9) < STATE_ORDER.get(merged[key]["state"], 9):
            merged[key] = row
    kept = [row for row in merged.values() if row["state"] != "rejected"]
    rejected = [row for row in merged.values() if row["state"] == "rejected"]
    for row in kept:
        row["gate_ok"], row["gate_reasons"] = True, []
    kept.sort(key=lambda r: (STATE_ORDER[r["state"]], r["exposure"]["distance"] is None, r["exposure"]["distance"] or 0,
                             r["path"], r["line"]))
    kinds = {}
    for unit in units:
        kinds[unit.kind] = kinds.get(unit.kind, 0) + 1
    result = {"scope": "model_review", "version": 2, "findings": kept, "rejected": rejected, "unlocated": unlocated,
              "skipped": skipped, "reviewed": reviewed,
              "coverage": {"units": len(units), "by_kind": kinds, "reviewed": reviewed, "invalid_answers": invalid,
                           "files": len(files), "confirmed": sum(r["state"] == "confirmed" for r in kept),
                           "unconfirmed": sum(r["state"] == "unconfirmed" for r in kept), "rejected": len(rejected),
                           "unlocated": len(unlocated)}}
    if stopped:
        result["error"] = stopped
    result["status"] = "partial" if stopped or reviewed < len(units) or skipped else "complete"
    return result
