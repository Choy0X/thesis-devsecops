"""Run authored reference fixtures, and only those, on this host.

This is NOT the behavioural verifier. Model-generated code is never run here; it goes to the hardened container
profile (``behavior_verification``), and when that is unavailable it stays ``not_run``/``env_error``.

The authored corpus is deterministic template code written by this repository: fake adapters that capture arguments,
no file, network or process access. Before anything runs, the pair (source, test) must be byte-identical to a member
of the authored allowlist, so a generated artifact cannot reach this runner by accident. JavaScript runs under
Node's permission model with every capability denied. Receipts carry ``host_trusted_authored_reference`` so they are
never mistaken for container receipts.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

ENVIRONMENT = 'host_trusted_authored_reference'
TIMEOUT_SECONDS = 10
_ALLOWLIST_CACHE = {}


class ReferenceRefused(ValueError):
    """The code is not byte-identical to an authored reference pair."""


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def authored_pairs(extra=()):
    """Allowed (source sha256, test sha256) pairs: every side of the v1 corpus against the authored test of its family."""
    if 'v1' not in _ALLOWLIST_CACHE:
        import json
        from training.finetune import security_trial_data as data
        _, views, files, _oracles = data.build_rows()
        pairs = set()
        for view in views:
            if view['task'] != 'regression_test_design':
                continue
            test = json.loads(view['response_json'])['tests'][0]['test_code']
            family = view['family_id']
            for side in ('vulnerable', 'fixed'):
                pairs.add((_sha(files[f'source/{family}/{side}.txt'].decode('utf-8')), _sha(test)))
        _ALLOWLIST_CACHE['v1'] = pairs
    return _ALLOWLIST_CACHE['v1'] | set(extra)


def _classify(returncode, stdout, stderr, timed_out):
    if timed_out:
        return 'timeout'
    if returncode == 0:
        return 'passed'
    if 'AssertionError' in stderr or 'AssertionError' in stdout:
        return 'failed_assertion'
    return 'crashed'


def run_reference(source, test_code, *, language, allowed=None):
    """Run one authored source with one authored test; returns passed, failed_assertion, crashed, timeout or env_error."""
    pairs = authored_pairs() if allowed is None else allowed
    if (_sha(source), _sha(test_code)) not in pairs:
        raise ReferenceRefused('only byte-identical authored reference code may run on the host')
    env = {key: os.environ[key] for key in ('SYSTEMROOT', 'PATH', 'TEMP', 'TMP') if key in os.environ}
    with tempfile.TemporaryDirectory(prefix='reference-') as folder:
        if language == 'python':
            path = os.path.join(folder, 'case.py')
            with open(path, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write(source + '\n' + test_code)
            command = [sys.executable, '-I', path]
        elif language == 'javascript':
            node = shutil.which('node')
            if node is None:
                return {'outcome': 'env_error', 'detail': 'node is not installed'}
            path = os.path.join(folder, 'case.js')
            with open(path, 'w', encoding='utf-8', newline='\n') as stream:
                stream.write("const assert = require('node:assert');\n" + source + '\n' + test_code)
            command = [node, '--permission', f'--allow-fs-read={path}', path]
        else:
            return {'outcome': 'env_error', 'detail': 'unsupported language'}
        try:
            done = subprocess.run(command, cwd=folder, env=env, capture_output=True, text=True,
                                  timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {'outcome': 'timeout', 'detail': f'no result within {TIMEOUT_SECONDS} s'}
        except OSError as error:
            return {'outcome': 'env_error', 'detail': str(error)}
    return {'outcome': _classify(done.returncode, done.stdout, done.stderr, False),
            'detail': (done.stderr or done.stdout).strip()[-400:]}
