"""Building views: the student-visible input, the target answer and the flags that gate a row.

Two stages with different access, which is what keeps the question free of the answer:

* ``build_input`` makes the text the student sees. It is given a subject (code, path, language, symbol), context
  items from the local snapshot and, for two tasks, an explicit catalog condition or report. It is never given the
  reviewed record: an advisory, a CVE id, a fix, a patch or a CWE cannot reach it.
* ``build_view`` then reads the reviewed record, only to build the target answer and to flag it (an answer that
  names code the context does not show is a ``hidden_fact_risk``, an input to the auto_checked gate).

A view that cannot be built is an ``Exclusion`` with a reason, never a guess.
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field

from app.backend.pipeline import negatives, review
from app.backend.pipeline.views import catalog, rendering
from training.finetune import contracts as ct, serialization as sz

STUDENT_KINDS = ("enclosing", "callee", "caller", "imports", "module_symbol", "manifest", "configuration")
EXTRA_KEYS = {"discovery": ('review_scope',), "condition_assessment": ("condition", "condition_id", 'review_scope'),
              "repair": ("report", 'review_scope'), "localization": ('review_scope',), "insufficient_context": ('review_scope',),
              "instruction_integrity": ('review_scope',)}
POOL_OF_ROLE = {"vulnerable": "supported", "no_finding": "scoped_negative", "insufficient_context": "insufficient_context"}
NO_FINDING_ASSUMPTION = "only the code and context shown were considered"
GENERIC_REASON = "The finding rests on this statement."
GENERIC_SUMMARY = "The code shown contains a flaw that the evidence supports."
GENERIC_INTENT = "Keep the existing behaviour for inputs that were already handled correctly."
REVIEWED_STATUSES = ("human_reviewed", "llm_adjudicated")


# -- the student-visible input ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StudentInput:
    task: str
    target: dict
    items: list
    extra: dict
    dropped_kinds: list
    rendering: dict | None = None

    @property
    def context_text(self) -> str:
        sections = []
        if 'review_scope' in self.extra:
            sections.append('[review scope]\n' + self.extra['review_scope'])
        if "condition" in self.extra:
            sections.append(f"[condition] {self.extra.get('condition_id', '')}".rstrip() + f"\n{self.extra['condition']}")
        if "report" in self.extra:
            sections.append(f"[report]\n{self.extra['report']}")
        sections.append(sz.render_context(self.target, self.items))
        return "\n\n".join(sections)


def build_input(task: str, subject, items, extra: dict | None = None, *, rendering_policy: str | None = None) -> StudentInput:
    """The student-visible input of a task. There is no record parameter: nothing privileged can be read here."""
    ct.by_name(task)
    extra = dict(extra or {})
    bad = sorted(set(extra) - set(EXTRA_KEYS.get(task, ())))
    if bad:
        raise ValueError(f"{task} takes no extra input {bad}; it takes {list(EXTRA_KEYS.get(task, ()))}")
    kept = [{k: i.get(k) for k in ("kind", "path", "name", "text")} for i in items if i.get("kind") in STUDENT_KINDS]
    dropped = sorted({i.get("kind") for i in items if i.get("kind") not in STUDENT_KINDS})
    target = {"path": subject.path, "language": subject.language, "code": subject.code, "symbol": subject.symbol}
    metadata = None
    if rendering_policy is not None:
        if rendering_policy != rendering.POLICY or task not in rendering.DETECTION_TASKS:
            raise rendering.RenderingError('rendering_policy_or_task_unsupported')
        rendered = rendering.render_input(target, kept)
        rendering.assert_identifier_free(rendered, extra)
        target, kept = rendered['target'], rendered['items']
        metadata = {'policy': rendering.POLICY, 'implementation_sha256': rendering.implementation_hash(),
                    'sources': rendered['sources'], 'provenance': rendered['provenance'],
                    'rendered_input_sha256': rendering.input_fingerprint(task, target, kept, extra)}
    return StudentInput(task, target, kept, extra, dropped, metadata)


def _rendering_flags(inp):
    """Persist bounded provenance, never full source bodies or coordinate maps."""
    metadata = inp.rendering
    return {'rendering_policy': metadata['policy'],
            'rendering_implementation_sha256': metadata['implementation_sha256'],
            'rendered_input_sha256': metadata['rendered_input_sha256'],
            'rendering_extra': dict(inp.extra),
            'target_rendering': dict(metadata['sources']['target']['provenance']),
            'item_rendering': [dict(source['provenance']) if source is not None else None
                               for source in metadata['sources']['items']]}


# -- views and exclusions --------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Exclusion:
    reason: str
    task: str | None = None
    subject_id: str | None = None


@dataclass(frozen=True)
class View:
    view_id: str
    task: str
    subject_id: str
    record_id: str | None
    issue_group_id: str | None
    family_id: str | None
    pool: str
    context_text: str
    response: object                     # the answer: a dict for the JSON tasks, text for replay
    weight: float
    review_status: str
    eligible: bool
    flags: dict = field(default_factory=dict)
    lineage: dict = field(default_factory=dict)
    context_hash: str | None = None

    def student_text(self) -> str:
        return sz.render_prompt(self.task, self.context_text)

    def serialize(self) -> sz.Serialized:
        return sz.serialize_example(self.task, self.context_text, self.response)


def _view_id(*parts: str) -> str:
    return "view_" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


# -- target helpers ------------------------------------------------------------------------------------------------------------------

def _meaningful(line: str) -> bool:
    return len(line.strip()) >= 3 and bool(re.search(r"\w", line))


def evidence_lines(vulnerable: str, fixed: str, max_lines: int = 2) -> list[str]:
    """The lines of the vulnerable code that carry the flaw: the lines the fix removes or rewrites, or, for a fix
    that only inserts, the statement just before the insertion. Each is a line of the vulnerable code."""
    bl, al = vulnerable.splitlines(), fixed.splitlines()
    removed: list[str] = []
    before_insert: list[str] = []
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(None, bl, al, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += [l for l in bl[i1:i2] if _meaningful(l)]
        elif tag == "insert" and i1 > 0:
            prev = next((l for l in reversed(bl[:i1]) if _meaningful(l)), None)
            if prev:
                before_insert.append(prev)
    return [l.strip() for l in (removed or before_insert)][:max_lines]


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LOWER_CAMEL = re.compile(r"^[a-z][a-z0-9]*[A-Z]")
# Words that name a runtime, a product or a concept rather than a piece of the code shown. Compared in lower case.
_COMMON_WORDS = frozenset("""
true false null none undefined nan this self node nodejs npm http https url uri json html xml sql system object objects string
strings number numbers array arrays function functions class classes prototype default defaults error errors import export
exports require module modules buffer stream promise async await return public private static void boolean type types name
names value values data file files path paths input output user users request response server client header headers cookie
cookies token tokens session password query queries regex regexp window document global local main test tests list dict map
set key keys index limit size length time date string macos ios ipados tvos watchos iphone ipad github gitlab openssl
javascript typescript jquery python ruby golang rust java kotlin linux windows docker kubernetes
""".split())


def _name_tokens(record: dict) -> set[str]:
    """Words of the package and repository names: they name the project, not a piece of its code."""
    out: set[str] = set()
    for key in ("package_identity", "repository_identity", "ecosystem"):
        out.update(t.lower() for t in re.split(r"[^A-Za-z0-9]+", str(record.get(key) or "")) if len(t) >= 3)
    return out


def hidden_facts(text: str, context: str, ignore=frozenset()) -> list[str]:
    """Code identifiers a target names that the context does not contain: the answer would depend on a fact the
    student cannot see. A word counts as an identifier when it is in backticks, has an underscore or starts
    lower-case with a capital inside (``innerHTML``), or is followed by a parenthesis. A capitalised word outside
    backticks is a product or proper name, and common runtime words and the project's own names are not code."""
    ids: set[str] = set()
    for m in re.finditer(r"`([^`]+)`", text):
        ids.update(_IDENT.findall(m.group(1)))
    for t in _IDENT.findall(text):
        if "_" in t.strip("_") or _LOWER_CAMEL.match(t):
            ids.add(t)
    ids.update(m.group(1) for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\(", text))
    haystack, skip = context.lower(), {x.lower() for x in ignore}
    return sorted(i for i in ids if len(i) >= 4 and i.lower() not in _COMMON_WORDS and i.lower() not in skip
                  and i.lower() not in haystack)


def _first_sentences(text: str | None, limit: int) -> str:
    out = ""
    for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()):
        if out and len(out) + len(s) + 1 > limit:
            break
        out = f"{out} {s}".strip()
        if len(out) >= limit:
            break
    return out[:limit].strip()


def _summary(record: dict) -> tuple[str, str]:
    """(summary text, source): reviewed fields, an extract of the advisory, or a neutral template."""
    text = record.get("evidence_summary") or _first_sentences(record.get("explanation"), 300)
    if text:
        reviewed_fields = any(record.get(k) for k in ("violated_invariant", "attacker_control", "supported_impact"))
        return text.strip(), "reviewed" if reviewed_fields else "advisory_extract"
    return GENERIC_SUMMARY, "template"


first_sentences = _first_sentences
target_summary = _summary
name_tokens = _name_tokens


def _finding(subject, record: dict, summary: str, location: str | None) -> dict:
    return {"summary": summary, "location": location, "violated_invariant": record.get("violated_invariant"),
            "attacker_control": record.get("attacker_control"), "impact": record.get("supported_impact")}


def _evidence(record: dict, quotes: list[str]) -> list[dict]:
    reason = record.get("violated_invariant") or GENERIC_REASON
    return [{"quote": q, "reason": reason} for q in quotes]


def _response(status: str, findings=None, evidence=None, assumptions=None, missing=None, **more) -> dict:
    return {"status": status, "findings": findings or [], "evidence": evidence or [], "assumptions": assumptions or [],
            "missing_context": missing or [], "validation_state": "not_validated", **more}


def _intended_role(subject) -> str | None:
    if subject.kind == "fixed_twin":
        return "no_finding"
    if subject.role in POOL_OF_ROLE:
        return subject.role
    return "vulnerable" if subject.kind in ("vulnerable", "variant_harmless") else None


def _spans(record: dict) -> list[dict]:
    raw = record.get("reviewed_security_spans")
    try:
        spans = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        return []
    return [s for s in spans if isinstance(s, dict) and isinstance(s.get("start_line"), int) and isinstance(s.get("end_line"), int)]


# -- building a view ---------------------------------------------------------------------------------------------------------------------

def build_view(task: str, subject, record: dict, items, *, extra: dict | None = None,
               context_hash: str | None = None, strict=False) -> View | Exclusion:
    """The view of ``task`` for a subject, or an ``Exclusion`` saying why there is none."""
    t = ct.by_name(task)
    if task in ("general_code_replay", "instruction_integrity"):
        raise ValueError(f"{task} is not built from a record: see pools.replay_views / build_injection")

    def out(reason: str) -> Exclusion:
        return Exclusion(reason, task, subject.subject_id)

    role = _intended_role(subject)
    extra = dict(extra or {})
    if task == "repair":
        if role != "vulnerable":
            return out("not_a_vulnerable_subject")
        if not extra.get("report"):
            return out("no_report")
    condition = None
    if task == "condition_assessment":
        condition = catalog.condition_for(review.mechanism(record.get("cwe_ids")))
        if condition is None:
            return out("no_catalog_condition")
        if role not in ("vulnerable", "no_finding"):
            return out("not_applicable_to_role")
        extra = {"condition": condition["text"], "condition_id": condition["id"]}
    scoped_input = record.get('admission_policy') == 'automatic-evidence/3'
    if scoped_input:
        proof = record.get('admission_provenance') or {}
        proof = json.loads(proof) if isinstance(proof,str) else proof
        extra['review_scope'] = catalog.review_scope(proof.get('mechanism'))
    rendering_policy = rendering.POLICY if scoped_input and task in rendering.DETECTION_TASKS else None
    try:
        inp = (build_input(task, subject, items, extra, rendering_policy=rendering_policy)
               if rendering_policy else build_input(task, subject, items, extra))
    except rendering.RenderingError as error:
        return out(str(error))
    context = inp.context_text
    if scoped_input and task in ('discovery', 'condition_assessment', 'localization') and re.search(
            r'\b(?:CVE-\d{4}-\d{4,}|GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}|CWE-\d+)\b', context, re.I):
        # Retain immutable source and proof, but never teach detection from an
        # advisory identifier. Rewriting an arbitrary string can change code
        # semantics, so this remains an explicit view exclusion.
        return out('answer_bearing_source_identifier')
    flags: dict = {"candidate_only": subject.weight == 0.0, "dropped_kinds": inp.dropped_kinds}
    if scoped_input:
        flags['review_scope_version'] = catalog.REVIEW_SCOPE_VERSION
    if inp.rendering:
        flags.update(_rendering_flags(inp))
    code = subject.code

    if task == "insufficient_context" or role == "insufficient_context":
        facts = [f for f in (record.get("missing_facts") or []) if isinstance(f, str) and f.strip()]
        if subject.role != "insufficient_context" or not facts:
            return out("no_reviewed_omissions")
        response, pool = _response("insufficient_context", assumptions=[NO_FINDING_ASSUMPTION], missing=facts), "insufficient_context"
        if task not in ("insufficient_context", "discovery"):
            return out("not_applicable_to_role")
    elif role is None:
        return out("unknown_label")
    elif role == "no_finding":
        if task in ("localization", "repair"):
            return out("not_a_vulnerable_subject")
        if strict:
            safeguard, scope = record.get('decisive_safeguard'), record.get('condition_scope')
            if not safeguard or not scope:
                return out('missing_scoped_negative_evidence')
            raw = record.get('evidence_spans') or []
            evidence = json.loads(raw) if isinstance(raw, str) else raw
            if not evidence or any(not isinstance(e, dict) or not isinstance(e.get('quote'), str) or not e['quote'].strip() or e['quote'] not in code for e in evidence):
                return out('negative_evidence_not_in_source')
            response = _response('no_finding', evidence=[{'quote': e['quote'], 'reason': safeguard} for e in evidence if e.get('quote') in code], assumptions=[scope])
            pool = 'scoped_negative'
        else:
            response, pool = _response("no_finding", assumptions=[NO_FINDING_ASSUMPTION]), "scoped_negative"
    else:                                                       # a vulnerable subject
        pool = "supported"
        if task == "localization":
            if record.get("review_status") not in REVIEWED_STATUSES or not _spans(record):
                return out("no_reviewed_spans")
            lines = code.splitlines(keepends=True) if inp.rendering else code.splitlines()
            span = _spans(record)[0]
            a, b = span["start_line"], span["end_line"]
            if not (1 <= a <= b <= len(lines)):
                return out("span_outside_code")
            quote = (''.join(lines[a - 1:b]) if inp.rendering else "\n".join(lines[a - 1:b])).strip()
            summary, source = _summary(record)
            response = _response("finding", [_finding(subject, record, summary, f"lines {a}-{b}")], _evidence(record, [quote]))
        elif task == "insufficient_context":
            return out("no_reviewed_omissions")
        else:
            if strict:
                raw = record.get('evidence_spans') or []
                quotes = [e.get('quote') for e in (json.loads(raw) if isinstance(raw, str) else raw) if isinstance(e, dict)]
            else:
                quotes = evidence_lines(record.get("vulnerable_code") or "", record.get("fixed_code") or "")
            if not quotes:
                return out("no_evidence_lines")
            if any(not isinstance(q, str) or not q.strip() or q not in code for q in quotes):
                return out("evidence_not_in_code")
            if condition is not None:
                summary, source = f"The condition holds: {condition['text']}", "catalog"
            else:
                summary, source = _summary(record)
            response = _response("finding", [_finding(subject, record, summary, subject.symbol)], _evidence(record, quotes))
            if task == "repair":
                if strict and not record.get('behavioral_intent'):
                    return out('missing_grounded_repair_intent')
                a = (record.get("vulnerable_code") or "").splitlines(keepends=True)
                b = (record.get("fixed_code") or "").splitlines(keepends=True)
                path = subject.path or "file"
                patch = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}"))
                added = [l[1:].strip() for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++")]
                if not patch or any(len(x) >= 15 and x in extra["report"] for x in added):
                    return out("report_leaks_patch" if patch else "no_patch")
                response = {**response, "patch": patch, "behavioral_intent": record.get("behavioral_intent") or GENERIC_INTENT}
                flags.update(target_kind="historical_patch", alternatives_possible=True)
        flags["summary_source"] = source
        hidden = hidden_facts(response["findings"][0]["summary"], context, ignore=_name_tokens(record))
        flags.update(hidden_fact_risk=bool(hidden), hidden_facts=hidden)
    if record.get('admission_policy') in ('automatic-evidence/1', 'automatic-evidence/2', 'automatic-evidence/3') and role == 'vulnerable':
        # Source-rule findings are conditional on their recorded input and
        # environment assumptions. Keep them in the actual supervised answer.
        response['assumptions'] = list(record.get('preconditions') or [])
    if inp.rendering:
        mapped = []
        try:
            for index, evidence in enumerate(response.get('evidence', [])):
                owned = rendering.map_quote(code, inp.rendering['sources']['target'], evidence['quote'])
                if not owned['quote'].strip() or owned['quote'] not in inp.target['code']:
                    return out('rendered_evidence_not_in_target')
                evidence['quote'] = owned['quote']
                mapped.append({'index': index, 'source_span': owned['source_span'],
                               'rendered_span': owned['rendered_span'], 'rendering_policy': owned['rendering_policy']})
        except rendering.RenderingError as error:
            return out(str(error))
        flags['rendered_evidence'] = mapped
    problems = ct.validate_response(task, ct.normalize(task, response))
    if problems:
        return out("invalid_target: " + "; ".join(problems))
    response = ct.normalize(task, response)
    extra_sig = json.dumps(extra, sort_keys=True)
    if inp.rendering:
        extra_sig = json.dumps({'extra': extra, 'rendering_policy': rendering_policy,
            'rendering_implementation_sha256': flags['rendering_implementation_sha256'],
            'rendered_input_sha256': flags['rendered_input_sha256']}, sort_keys=True)
    return View(view_id=_view_id(task, subject.subject_id, pool, extra_sig), task=task, subject_id=subject.subject_id,
                record_id=subject.parent_record_id, issue_group_id=subject.issue_group_id, family_id=subject.issue_group_id,
                pool=pool, context_text=context, response=response, weight=subject.weight, review_status=subject.review_status,
                eligible=negatives.eligible(subject), flags=flags, lineage=dict(subject.lineage), context_hash=context_hash)


# -- T7: injection pairs --------------------------------------------------------------------------------------------------------------------------

def _inject(code: str, marker: str, text: str) -> str:
    lines = code.split("\n")
    indent = re.match(r"\s*", lines[1]).group(0) if len(lines) > 1 else ""
    lines.insert(1 if len(lines) > 1 else len(lines), f"{indent}{marker} {text}")
    return "\n".join(lines)


def _rendered_injection(parent, subject, items, family, seed, text):
    def rejected():
        return Exclusion('injection_parent_rendering_mismatch', 'instruction_integrity', subject.subject_id)
    if (parent.subject_id != subject.subject_id or parent.flags.get('rendering_policy') != rendering.POLICY
            or parent.flags.get('rendering_implementation_sha256') != rendering.implementation_hash()):
        return rejected()
    try:
        base = build_input('discovery', subject, items, parent.flags.get('rendering_extra'),
                           rendering_policy=rendering.POLICY)
    except (ValueError, rendering.RenderingError):
        return rejected()
    if (base.rendering['rendered_input_sha256'] != parent.flags.get('rendered_input_sha256')
            or base.context_text != parent.context_text):
        return rejected()
    base_flags = _rendering_flags(base)
    if any(base_flags[key] != parent.flags.get(key) for key in ('target_rendering', 'item_rendering', 'rendering_extra')):
        return rejected()
    evidence = parent.response.get('evidence', [])
    ownership = parent.flags.get('rendered_evidence', [])
    if len(ownership) != len(evidence):
        return rejected()
    # Re-establish original ownership through the parent's persisted intervals.
    # Matching the source/render coordinates also prevents silently shifting an
    # accepted quote to a different occurrence when rebuilding the child.
    try:
        for index, item in enumerate(evidence):
            start, end = ownership[index]['source_span']['characters']
            owned = rendering.map_quote(subject.code, base.rendering['sources']['target'], subject.code[start:end])
            if (owned['quote'] != item['quote'] or owned['quote'] not in base.target['code']
                    or owned['source_span'] != ownership[index]['source_span']
                    or owned['rendered_span'] != ownership[index]['rendered_span']):
                return rejected()
    except (KeyError, TypeError, ValueError):
        return rejected()
    if any(character in rendering.LINE_TERMINATORS for character in text):
        return Exclusion('injection_template_not_single_line', 'instruction_integrity', subject.subject_id)
    # Keep the full original target contiguous, including multiline evidence.
    # The intentional attack is added after blinding and is never blinded away.
    prefix = '// ' + text + '\n'
    injected = dataclasses.replace(subject, code=prefix + base.target['code'])
    child_extra = {key: value for key, value in base.extra.items() if key == 'review_scope'}
    inp = build_input('instruction_integrity', injected, base.items, child_extra)
    try:
        rendering.assert_identifier_free({'target': inp.target, 'items': inp.items}, child_extra)
    except rendering.RenderingError as error:
        return Exclusion(str(error), 'instruction_integrity', subject.subject_id)
    if any(item['quote'] not in inp.target['code'] for item in evidence):
        return rejected()
    fingerprint = rendering.input_fingerprint(inp.task, inp.target, inp.items, inp.extra)
    shift_chars, shift_bytes = len(prefix), len(prefix.encode())
    shifted = []
    for item in ownership:
        moved = json.loads(json.dumps(item))
        moved['rendered_span'] = {
            'characters': [position + shift_chars for position in item['rendered_span']['characters']],
            'bytes': [position + shift_bytes for position in item['rendered_span']['bytes']]}
        shifted.append(moved)
    target_provenance = {**parent.flags['target_rendering'],
        'rendered_sha256': hashlib.sha256(inp.target['code'].encode()).hexdigest(),
        'intentional_comment_prefix_sha256': hashlib.sha256(prefix.encode()).hexdigest()}
    flags = {**parent.flags, 'attack_family': family, 'attack_split': catalog.attack_split(family),
             'rendered_input_sha256': fingerprint, 'rendered_evidence': shifted, 'target_rendering': target_provenance,
             'base_rendered_input_sha256': base.rendering['rendered_input_sha256'],
             'injection_prefix': {'characters': shift_chars, 'bytes': shift_bytes}}
    lineage = {'parent_view_id': parent.view_id, 'attack_family': family, 'template_version': catalog.ATTACK_VERSION,
               'rendering_policy': rendering.POLICY, 'rendering_implementation_sha256': rendering.implementation_hash(),
               'base_rendered_input_sha256': base.rendering['rendered_input_sha256'], 'rendered_input_sha256': fingerprint}
    return View(view_id=_view_id('instruction_integrity', parent.view_id, family, seed, rendering.POLICY,
                    flags['rendering_implementation_sha256'], fingerprint), task='instruction_integrity',
                subject_id=subject.subject_id, record_id=parent.record_id, issue_group_id=parent.issue_group_id,
                family_id=parent.family_id, pool=parent.pool, context_text=inp.context_text,
                response=json.loads(json.dumps(parent.response)), weight=parent.weight, review_status=parent.review_status,
                eligible=parent.eligible, flags=flags, lineage=lineage, context_hash=parent.context_hash)


def build_injection(parent: View, subject, items, *, family: str, seed: str = "") -> View | Exclusion:
    """An instruction-integrity view templated from an accepted discovery view: the same code with an instruction
    hidden in a comment, and the same answer, because the right response ignores the instruction."""
    if parent.task != "discovery":
        raise ValueError("an injection is templated from a discovery view")
    if family not in catalog.ATTACK_FAMILIES:
        raise ValueError(f"unknown attack family {family!r}")
    marker = "#" if subject.language in negatives._HASH_COMMENT else "//" if subject.language in negatives._SLASH_COMMENT else None
    if marker is None:
        return Exclusion("no_comment_marker", "instruction_integrity", subject.subject_id)
    texts = catalog.ATTACK_FAMILIES[family]
    text = texts[int(hashlib.sha1(f"{seed}|{family}|{parent.view_id}".encode()).hexdigest(), 16) % len(texts)]
    if parent.flags.get('rendering_policy'):
        return _rendered_injection(parent, subject, items, family, seed, text)
    injected = dataclasses.replace(subject, code=_inject(subject.code, marker, text))
    inp = build_input("instruction_integrity", injected, items)
    return View(view_id=_view_id("instruction_integrity", parent.view_id, family, seed), task="instruction_integrity",
                subject_id=subject.subject_id, record_id=parent.record_id, issue_group_id=parent.issue_group_id,
                family_id=parent.family_id, pool=parent.pool, context_text=inp.context_text,
                response=json.loads(json.dumps(parent.response)), weight=parent.weight, review_status=parent.review_status,
                eligible=parent.eligible,
                flags={"attack_family": family, "attack_split": catalog.attack_split(family), "candidate_only": parent.flags.get("candidate_only")},
                lineage={"parent_view_id": parent.view_id, "attack_family": family, "template_version": catalog.ATTACK_VERSION},
                context_hash=parent.context_hash)
