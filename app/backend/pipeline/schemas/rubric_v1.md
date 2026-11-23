# Review rubric, version rubric_v1

This rubric tells a reviewer how to decide what a code pair supports. It is versioned: every review
decision records the rubric version it followed, and a change to these rules is a new version, never an
edit. Decisions are appended and never overwritten; the latest decision of a record is the current one.

## What is being reviewed

One record is one code pair: the vulnerable side (the code before a fix commit) and the fixed side (the
code after it), a function, a hunk or a file, with the fix commit, the diff, the advisory and the context
that was collected. The question is always the same: does the evidence in this record support a specific,
checkable security claim about the vulnerable side, and at what level of confidence?

## Label states

Pick exactly one label state.

- `vulnerability_supported`: the diff and the surrounding code show a concrete flaw on the vulnerable side
  that the fix removes, and you can name the violated invariant, the attacker-controlled input and the
  missing or wrong safeguard from the code you were shown.
- `no_supported_finding_in_scope`: within the scope that was checked (the named condition, in the code
  shown), the code does not contain the claimed flaw. Use it for the fixed side of a fix, for a
  protected use, and for a unit the fix commit touched only for another reason (a refactor, a rename).
  It is a statement about a scope, never about the whole program.
- `insufficient_context`: the claim might be true but the code shown cannot establish it (the decisive
  caller, configuration or dependency is missing). Write what is missing in `missing_facts`.
- `unreviewed`: no decision. Do not submit it.

When in doubt between `vulnerability_supported` and `insufficient_context`, choose `insufficient_context`.

## Evidence

A `vulnerability_supported` decision must name its evidence: the diff lines or code lines that carry the
flaw, the invariant that is violated, who controls the input, and the safeguard that the fix adds or that is
absent. The advisory text may help you understand the claim; it is not evidence by itself and must not be the
only reason for a label. If the advisory names a fact that cannot be seen in the code or context shown,
that fact is a hidden fact: mark the record `insufficient_context` or record the missing fact.

## Which unit

A fix commit often changes several functions. Decide for this unit only. If the unit is a formatting change,
a comment change, a test, a rename or an unrelated edit, it is not the vulnerable unit: label it
`no_supported_finding_in_scope` and say why. Never label a unit vulnerable only because it sits in a fix
commit.

## Review statuses and evidence grades

- `unreviewed`: nothing checked, supervision weight 0.0.
- `auto_checked`: deterministic checks passed (verified fix link with link evidence, a recorded parent, a
  clean parse, a production unit with an attribution, a resolved explanation, no hidden-fact risk, a clean
  leakage group, enough context). This is grade G0: the advisory, the fix commit and the diff agree. It is not
  a review.
- `llm_adjudicated`: a model compared the claim with the evidence under this rubric. Grade G1 at most, and
  never independent of the model that produced a label.
- `human_reviewed`: a person followed this rubric. Grade G1.
- Grades G2 and G3 need verification receipts (the exploit or test runs before and after the fix, and the
  functionality still works); a review decision never grants them.

## Severity and CWE

Severity is a separate task. Do not let a CVSS score, a severity word or a CWE label decide whether a
vulnerability is supported. Record a CWE only if the code shows it; a CWE copied from the advisory is
marked as such.

## Audit

A stratified sample of auto-checked records is reviewed to estimate how often the gate is wrong. For these
records decide independently of the gate: do not assume the label is right because the gate passed. A record
whose reviewed label is not `vulnerability_supported` counts as an error of the gate. Name the error class in
the notes: wrong fix link, tangled commit, wrong unit, formatting only, flaw not supported by the code, or
hidden fact.

## Decision fields

Each decision carries: the record, your reviewer id, this rubric version, the review status
(`human_reviewed` or `llm_adjudicated`), the label state, optional structured fields (condition scope, trust
boundary, attacker control, preconditions, violated invariant, evidence spans, decisive safeguard,
supported impact, missing facts) and free notes.
