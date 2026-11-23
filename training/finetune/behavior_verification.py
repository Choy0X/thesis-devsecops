"""Trusted behavioural verification of security repairs and generated regression tests.

This module owns the procedures and receipts, not the sandbox. Code only ever runs through an injected *runner*; the hardened
container runner lives in ``behavior_container``. There is no host-execution fallback: with no usable runner a case that needs
execution is ``not_run`` (``sandbox_unavailable``), and that stays visible in the receipt and the sidecar.

Outcomes: ``passed``, ``failed``, ``rejected`` (a static stage refused it before anything ran), ``timeout``, ``env_error`` and
``not_run``. Generated tests (``generated:*`` runs) and trusted acceptance tests (``acceptance:*`` runs) are different runs on different
code and are never merged. Standard library only, so the same file can be shipped with the verifier entry point.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
import time

SCHEMA = 'security-behavior-verification/1'
VERIFIER_VERSION = 'security-behavior-verifier/1'
OUTCOMES = ('passed', 'failed', 'rejected', 'timeout', 'env_error', 'not_run')
CASE_TIMEOUT_SECONDS = 10
BATCH_SECONDS = 300
CAPABILITIES = ('repair_validation', 'generated_test_validation')
_HEX = re.compile(r'^[0-9a-f]{64}$')
_ERROR_LINE = re.compile(r'^(?:Uncaught )?([A-Za-z_][\w.]*(?:Error|Exception))\b')


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class RunSpec:
    label: str
    language: str
    files: dict
    entry: str
    mode: str = 'run'           # 'run' executes the entry; 'check' only parses it
    timeout: int = CASE_TIMEOUT_SECONDS
    nonce: str = ''             # per-run secret printed by the program only after its last assertion; '' for check runs
    program_sha256: str = ''    # hash of the program without the marker, for reproducible receipts


@dataclass(frozen=True)
class RunResult:
    outcome: str                 # ok | assertion | crash | timeout | env_error
    exit_code: int | None = None
    detail: str = ''
    seconds: float = 0.0


class UnavailableRunner:
    """Stands for a missing sandbox: reports itself honestly and never executes anything."""

    def __init__(self, reason):
        self.reason = reason

    def info(self, language=None):
        return {'kind': 'unavailable', 'reason': self.reason, 'image': None, 'image_digest': None, 'limits': None}

    def run(self, spec):
        return RunResult(outcome='env_error', detail=self.reason)


class BatchDeadline(Exception):
    """The verification batch has no time left for another run."""


MARKER = 'ZTSA_COMPLETE:'


def classify_process(exit_code, stderr, timed_out, *, stdout='', nonce=''):
    """ok, assertion, crash or timeout. Only an AssertionError counts as an assertion; any other exception is a crash.

    With a ``nonce`` an exit status of 0 is not enough: the program must also have printed the completion marker as its last output line,
    which is the last statement of the composed program. An early ``exit`` in the code under test (or in a generated test) therefore reads
    as a crash, not a pass. Residual risk, by design of one container per run: deliberately adversarial code that reads its own source can
    print the marker; stronger isolation would need the oracle in a separate container and is listed as a limitation."""
    if timed_out:
        return 'timeout'
    if exit_code == 0:
        lines = [line for line in (stdout or '').splitlines() if line.strip()]
        if nonce and (not lines or lines[-1] != MARKER + nonce):
            return 'crash'
        return 'ok'
    errors = [m.group(1).split('.')[-1] for m in (_ERROR_LINE.match(line) for line in (stderr or '').splitlines()) if m]
    if errors and all(name == 'AssertionError' for name in errors):
        return 'assertion'
    return 'crash'


def compose(language, source, test, nonce=None):
    """The exact program a run executes: the authenticated source, the test and, with a nonce, the completion marker.

    The test gets a final newline if it lacks one: the marker needs its own line, and a model-written test often ends without it (the first
    completed v5_1 run produced SQL tests ending in ``]``, which became ``...])print(...)``, a syntax error scored as a crash). Tests that already
    end with a newline, which is every authored test, compose to exactly the same bytes as before."""
    if test and not test.endswith('\n'):
        test += '\n'
    if language == 'python':
        program = source + '\n' + test
        return program + (f'print("{MARKER}{nonce}")\n' if nonce else '')
    program = "const assert = require('node:assert');\n" + source + '\n' + test
    return program + (f'console.log("{MARKER}{nonce}");\n' if nonce else '')


def _run_spec(label, language, source, test):
    """A run spec whose program ends with a fresh completion marker; the receipt hash covers the program without it."""
    nonce = secrets.token_hex(16)
    name = entry_name(language)
    return RunSpec(label, language, {name: compose(language, source, test, nonce)}, name, nonce=nonce,
                   program_sha256=_sha(compose(language, source, test)))


def entry_name(language):
    return 'case.py' if language == 'python' else 'case.js'


@dataclass(frozen=True)
class Case:
    family_id: str
    language: str
    source_path: str
    vulnerable_source: str
    fixed_source: str
    vulnerable_revision: str
    fixed_revision: str
    allowed_paths: tuple
    acceptance: tuple = field(default_factory=tuple)


def cases_from_views(views, files, oracles=None):
    """One case per family from dataset rows: authenticated sources and, where the family's oracle carries acceptance metadata, the semantic
    acceptance tests (ordinary, then boundary). A family without it (the legacy v1 corpus) has no acceptance tests and is not judged by a template."""
    from training.finetune.semantic_oracles import acceptance_tests
    meta = {o['family_id']: o['acceptance'] for o in (oracles or []) if o.get('acceptance')}
    families = {}
    for view in views:
        families.setdefault(view['family_id'], []).append(view)
    cases = {}
    for family, rows in families.items():
        by_id = {v['view_id']: v for v in rows}
        vulnerable = by_id.get(f'{family}:repair:vulnerable') or by_id.get(f'{family}:discovery:vulnerable')
        tests_view = by_id.get(f'{family}:regression_test_design:fixed')
        if vulnerable is None or tests_view is None:
            continue
        v_raw, f_raw = files[f'source/{family}/vulnerable.txt'], files[f'source/{family}/fixed.txt']
        language = vulnerable.get('language') or ('python' if vulnerable['allowed_patch_paths'][0].endswith('.py') else 'javascript')
        cases[family] = Case(family_id=family, language=language, source_path=vulnerable['allowed_patch_paths'][0],
                             vulnerable_source=v_raw.decode('utf-8'), fixed_source=f_raw.decode('utf-8'),
                             vulnerable_revision=hashlib.sha1(v_raw).hexdigest(), fixed_revision=hashlib.sha1(f_raw).hexdigest(),
                             allowed_paths=tuple(vulnerable['allowed_patch_paths']),
                             acceptance=tuple(acceptance_tests(meta[family])) if family in meta else ())
    return cases


# -- receipts ----------------------------------------------------------------------------------------------------------------------------------
def receipt_id(receipt):
    body = {k: v for k, v in receipt.items() if k != 'receipt_id'}
    return _sha(json.dumps(body, sort_keys=True, separators=(',', ':'), default=str))


def _runner_info(runner, language=None):
    if runner is None:
        return UnavailableRunner('no sandbox runner was supplied').info()
    try:
        return runner.info(language)
    except TypeError:
        return runner.info()


def _receipt(capability, case, arm, view_id, runner, *, artifact, response, outcome, reason, stages, checks):
    receipt = {'schema': SCHEMA, 'kind': 'receipt', 'capability': capability, 'arm': arm, 'view_id': view_id, 'family_id': case.family_id,
               'source_revision': case.vulnerable_revision, 'fixed_revision': case.fixed_revision,
               'artifact_sha256': _sha(artifact), 'response_sha256': _sha(response), 'verifier_version': VERIFIER_VERSION,
               'runner': _runner_info(runner, case.language), 'stages': stages, 'checks': checks, 'outcome': outcome, 'reason': reason}
    receipt['receipt_id'] = receipt_id(receipt)
    return receipt


def validate_receipt(receipt):
    """What is wrong with a receipt (empty when sound): schema, bound hashes, outcome vocabulary, id and the runner evidence."""
    if receipt.get('schema') != SCHEMA or receipt.get('kind') != 'receipt':
        return ['schema must be ' + SCHEMA]
    problems = []
    for key in ('artifact_sha256', 'response_sha256'):
        if not _HEX.match(str(receipt.get(key) or '')):
            problems.append(f'{key} must be a sha256 hex digest')
    for key in ('arm', 'view_id', 'source_revision', 'verifier_version', 'capability'):
        if not receipt.get(key):
            problems.append(f'{key} is required')
    if receipt.get('capability') not in CAPABILITIES:
        problems.append('capability must be one of ' + str(CAPABILITIES))
    if receipt.get('outcome') not in OUTCOMES:
        problems.append('outcome must be one of ' + str(OUTCOMES))
    runner = receipt.get('runner') or {}
    if receipt.get('outcome') in ('passed', 'failed', 'timeout'):
        limits = runner.get('limits') or {}
        if runner.get('kind') != 'docker':
            problems.append('a executed outcome needs a docker runner')
        if not str(runner.get('image_digest') or '').startswith('sha256:'):
            problems.append('an executed outcome needs the image digest')
        for key, want in (('network', 'none'),):
            if limits.get(key) != want:
                problems.append(f'limits.{key} must be {want}')
        for key in ('memory_mb', 'cpus', 'pids_limit', 'case_timeout_seconds'):
            if not isinstance(limits.get(key), (int, float)) or limits[key] <= 0:
                problems.append(f'limits.{key} must be positive')
    if runner.get('kind') == 'unavailable' and receipt.get('outcome') not in ('not_run', 'env_error', 'rejected'):
        problems.append('an unavailable sandbox cannot produce an executed outcome')
    if receipt.get('receipt_id') != receipt_id(receipt):
        problems.append('receipt_id does not match the content (the receipt was edited)')
    return problems


# -- shared helpers ------------------------------------------------------------------------------------------------------------------------------
class _Guarded:
    def __init__(self, runner, guard):
        self.runner, self.guard = runner, guard

    def info(self, language=None):
        return _runner_info(self.runner, language)

    def run(self, spec):
        self.guard(spec)
        return self.runner.run(spec)


def _usable(runner):
    return runner is not None and _runner_info(runner).get('kind') != 'unavailable'


def _stage(name, outcome, **extra):
    return {'name': name, 'outcome': outcome, **extra}


def _run(runner, spec, stages):
    result = runner.run(spec)
    program = spec.program_sha256 or {name: _sha(text) for name, text in sorted(spec.files.items())}
    stages.append(_stage(spec.label, result.outcome, exit_code=result.exit_code, program_sha256=program))
    return result.outcome


def _python_syntax(source):
    try:
        ast.parse(source)
        return None
    except (SyntaxError, ValueError, RecursionError, MemoryError) as error:       # deeply nested input raises RecursionError, not SyntaxError
        return f'{type(error).__name__}: {str(error)[:200]}'


def verify_repair(case, response_text, runner, *, arm, view_id):
    """Repair validation: parse the raw response, apply the patch in memory, check the patched source, run trusted acceptance tests."""
    from training.finetune.repair_validation import assess_patch
    from training.finetune.response_diagnostics import PATCH_CODES
    from training.finetune.security_serialization import parse_response
    stages, checks = [], {'functional_preservation': 'not_run', 'security_regression': 'not_run'}

    def done(outcome, reason, artifact=''):
        return _receipt('repair_validation', case, arm, view_id, runner, artifact=artifact, response=response_text, outcome=outcome,
                        reason=reason, stages=stages, checks=checks)

    answer, problems = parse_response('repair', response_text)
    if answer is None:
        stages.append(_stage('parse', 'rejected', problems=problems))
        return done('rejected', 'primary_response_invalid')
    patch = answer.get('patch') or ''
    if answer['status'] != 'finding' or not patch:
        stages.append(_stage('parse', 'rejected', problems=['no patch was proposed']))
        return done('rejected', 'no_patch')
    result = assess_patch(patch, {case.source_path: case.vulnerable_source}, allowed_paths=set(case.allowed_paths), keep_text=True)
    if result['state'] != 'applies_exactly':
        code = next((c for needle, c in PATCH_CODES if needle in result.get('detail', '')), 'other')
        stages.append(_stage('apply', 'rejected', detail=result.get('detail')))
        return done('rejected', 'patch_' + code, patch)
    patched = result['modified_sources'][case.source_path]
    stages.append(_stage('apply', 'passed', patched_sha256=_sha(patched)))
    if case.language == 'python':
        error = _python_syntax(patched)
        stages.append(_stage('syntax', 'rejected' if error else 'passed', **({'detail': error} if error else {})))
        if error:
            return done('rejected', 'patched_source_syntax_error', patch)
    if not case.acceptance:
        return done('not_run', 'acceptance_unavailable', patch)
    if not _usable(runner):
        return done('not_run', 'sandbox_unavailable', patch)
    try:
        if case.language != 'python':
            spec = RunSpec('syntax', case.language, {entry_name(case.language): patched}, entry_name(case.language), mode='check')
            if _run(runner, spec, stages) != 'ok':
                kind = stages[-1]['outcome']
                if kind in ('env_error', 'timeout'):
                    return done(kind, 'syntax_check_' + kind, patch)
                return done('rejected', 'patched_source_syntax_error', patch)
        statuses = {}
        for test in case.acceptance:
            outcome = _run(runner, _run_spec('acceptance:' + test['name'], case.language, patched, test['code']), stages)
            statuses[test['name']] = outcome
            key = 'functional_preservation' if test['name'] == 'ordinary' else 'security_regression'
            checks[key] = {'ok': 'passed', 'assertion': 'failed', 'crash': 'failed'}.get(outcome, outcome)
            if outcome in ('timeout', 'env_error'):
                return done(outcome, 'acceptance_' + outcome, patch)
    except BatchDeadline:
        return done('not_run', 'batch_deadline', patch)
    if checks['security_regression'] != 'passed' and checks['security_regression'] != 'failed':
        return done('not_run', 'security_check_missing', patch)
    if checks['security_regression'] == 'failed' or checks['functional_preservation'] == 'failed':
        both = checks['security_regression'] == checks['functional_preservation'] == 'failed'
        return done('failed', 'security_and_functional_regression' if both else
                    'security_regression' if checks['security_regression'] == 'failed' else 'functional_regression', patch)
    if checks['functional_preservation'] == 'not_run':
        return done('not_run', 'functional_check_unavailable', patch)
    return done('passed', 'all_checks_passed', patch)


def verify_generated_tests(case, response_text, runner, *, arm, view_id):
    """Generated-test validation: the same generated test runs on the authenticated fixed and vulnerable sources."""
    from training.finetune.security_serialization import parse_response
    stages, checks = [], {'fixed': 'not_run', 'vulnerable': 'not_run'}
    answer, problems = parse_response('regression_test_design', response_text)
    codes = [t['test_code'] for t in (answer or {}).get('tests', [])]

    def done(outcome, reason):
        return _receipt('generated_test_validation', case, arm, view_id, runner, artifact='\n--\n'.join(codes), response=response_text,
                        outcome=outcome, reason=reason, stages=stages, checks=checks)

    if answer is None:
        stages.append(_stage('parse', 'rejected', problems=problems))
        return done('rejected', 'primary_response_invalid')
    if not codes:
        stages.append(_stage('parse', 'rejected', problems=['no generated test']))
        return done('rejected', 'no_tests')
    if case.language == 'python':
        for index, code in enumerate(codes):
            error = _python_syntax(code)
            if error:
                stages.append(_stage(f'syntax:{index}', 'rejected', detail=error))
                return done('rejected', 'test_syntax_error')
    if not _usable(runner):
        return done('not_run', 'sandbox_unavailable')
    results = {'fixed': [], 'vulnerable': []}
    entry = entry_name(case.language)
    try:
        if case.language != 'python':
            for index, code in enumerate(codes):
                spec = RunSpec(f'syntax:{index}', case.language, {entry: compose(case.language, case.fixed_source, code)}, entry, mode='check')
                outcome = _run(runner, spec, stages)
                if outcome in ('env_error', 'timeout'):
                    return done(outcome, 'syntax_check_' + outcome)
                if outcome != 'ok':
                    return done('rejected', 'test_syntax_error')
        for index, code in enumerate(codes):
            for side, source in (('fixed', case.fixed_source), ('vulnerable', case.vulnerable_source)):
                outcome = _run(runner, _run_spec(f'generated:{index}:{side}', case.language, source, code), stages)
                if outcome in ('env_error', 'timeout'):
                    checks[side] = outcome
                    return done(outcome, f'generated_{side}_{outcome}')
                results[side].append(outcome)
    except BatchDeadline:
        return done('not_run', 'batch_deadline')
    fixed, vulnerable = results['fixed'], results['vulnerable']
    checks['fixed'] = 'passed' if all(o == 'ok' for o in fixed) else 'failed'
    checks['vulnerable'] = 'discriminated' if 'assertion' in vulnerable and 'crash' not in vulnerable else 'not_discriminated'
    if 'crash' in fixed:
        return done('failed', 'fixed_crash')
    if 'assertion' in fixed:
        return done('failed', 'fixed_rejected')
    if 'crash' in vulnerable:
        return done('failed', 'vulnerable_crash')
    if 'assertion' not in vulnerable:
        return done('failed', 'not_discriminating')
    return done('passed', 'discriminates_vulnerable_and_accepts_fixed')


START_ALLOWANCE_SECONDS = 5
CANARY = {'python': ('x = 1\n', 'assert x == 1\n', 'assert x == 2\n'),
          'javascript': ('const x = 1;\n', 'assert.strictEqual(x, 1);\n', 'assert.strictEqual(x, 2);\n')}


class _Fixed:
    """A runner that reports a failed positive control, so nothing is judged in a broken environment."""

    def __init__(self, runner, detail):
        self.runner, self.detail = runner, detail

    def info(self, language=None):
        return _runner_info(self.runner, language)

    def run(self, spec):
        return RunResult(outcome='env_error', detail=self.detail)


class _Expired:
    def __init__(self, runner):
        self.runner = runner

    def info(self, language=None):
        return _runner_info(self.runner, language)

    def run(self, spec):
        raise BatchDeadline()


def _canary(runner, languages):
    """Positive control: an authored correct program must pass and an authored wrong one must assert, before any model artifact runs."""
    for language in sorted(languages):
        source, good, bad = CANARY[language]
        for label, test, want in ((f'canary:fixed:{language}', good, 'ok'), (f'canary:vulnerable:{language}', bad, 'assertion')):
            outcome = runner.run(_run_spec(label, language, source, test)).outcome
            if outcome != want:
                return {'state': 'failed', 'languages': sorted(languages), 'detail': f'{label} returned {outcome}, expected {want}'}
    return {'state': 'passed', 'languages': sorted(languages), 'detail': 'authored fixed source passed and authored vulnerable source asserted'}


def verify_batch(items, runner, *, batch_seconds=BATCH_SECONDS, clock=time.monotonic, canary_report=None):
    """Verify many responses within one bounded batch. A positive-control canary runs first; a case that cannot finish in time is
    ``not_run`` (``batch_deadline``); an internal error in one case is an ``env_error`` receipt and the batch continues."""
    start = clock()

    def guard(spec):
        if clock() - start + spec.timeout + START_ALLOWANCE_SECONDS > batch_seconds:
            raise BatchDeadline()
    guarded = _Guarded(runner, guard) if runner is not None else None
    active, report = guarded, {'state': 'not_run', 'languages': [], 'detail': 'no usable sandbox'}
    if guarded is not None and _usable(runner) and items:
        try:
            report = _canary(guarded, {item['case'].language for item in items})
            if report['state'] == 'failed':
                active = _Fixed(runner, 'positive-control canary failed: ' + report['detail'])
        except BatchDeadline:
            report, active = {'state': 'not_run', 'languages': [], 'detail': 'batch deadline reached during the canary'}, _Expired(runner)
    if canary_report is not None:
        canary_report.update(report)
    receipts = []
    for item in items:
        function = verify_repair if item['capability'] == 'repair_validation' else verify_generated_tests
        try:
            receipts.append(function(item['case'], item['text'], active, arm=item['arm'], view_id=item['view_id']))
        except Exception as error:         # one bad case must not end the batch or leave a gap in the accounting
            receipts.append(_receipt(item['capability'], item['case'], item['arm'], item['view_id'], active, artifact='', response=item['text'],
                                     outcome='env_error', reason='verifier_error',
                                     stages=[_stage('internal', 'env_error', detail=f'{type(error).__name__}: {str(error)[:200]}')], checks={}))
    return receipts


def build_sidecar(receipts, planned, *, runner=None, elapsed_seconds=None, canary=None):
    """Account for every planned (arm, view, capability); anything without a receipt or not executed is listed with its reason."""
    by_key = {(r['arm'], r['view_id'], r['capability']): r for r in receipts}
    counts = {name: 0 for name in OUTCOMES}
    unexecuted = []
    for item in planned:
        receipt = by_key.get((item['arm'], item['view_id'], item['capability']))
        if receipt is None:
            counts['not_run'] += 1
            unexecuted.append({**{k: v for k, v in item.items() if k != 'missing_reason'}, 'reason': item.get('missing_reason', 'no_receipt')})
            continue
        counts[receipt['outcome']] += 1
        if receipt['outcome'] == 'not_run':
            unexecuted.append({**item, 'reason': receipt['reason']})
    return {'schema': SCHEMA, 'kind': 'sidecar', 'verifier_version': VERIFIER_VERSION, 'planned': len(planned), 'counts': counts,
            'unexecuted': unexecuted, 'denominator_complete': sum(counts.values()) == len(planned),
            'runner': _runner_info(runner), 'receipts': [by_key[(i['arm'], i['view_id'], i['capability'])] for i in planned
                                                         if (i['arm'], i['view_id'], i['capability']) in by_key],
            'elapsed_seconds': elapsed_seconds, 'canary': canary,
            'time_accounting': 'Verification time is reported here, separately from the 30-minute notebook budget.',
            'interpretation': 'Container receipts are the only execution evidence; synthetic authored acceptance tests do not establish independent security accuracy.'}
