"""The hardened container runner for behavioural verification (``security-behavior-profile/1``).

One short-lived container per run, with the same hardening conventions as ``app.backend.pipeline.verify.containers`` (no network, user
nobody, read-only root, every capability dropped, no new privileges, no host environment) and these profile limits: one CPU, 512 MiB
without swap, 64 processes, a 10 second case timeout and a small noexec tmpfs as the only writable path. The program is mounted
read-only; nothing is installed during a run. Images come from a pinned profile ``{language: {reference, image_id}}``; the runner only
looks images up locally, never pulls one, and refuses an image whose id differs from the pin.

Docker SDK only; no application imports, so the verifier entry point can run on its own.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from training.finetune.behavior_verification import BATCH_SECONDS, CASE_TIMEOUT_SECONDS, RunResult, UnavailableRunner, classify_process

PROFILE = 'security-behavior-profile/1'
NOBODY = '65534:65534'
TMPFS = {'/tmp': 'rw,noexec,nosuid,size=16m'}
MEMORY_MB, CPUS, PIDS = 512, 1.0, 64
LABEL = {'ztsa.security-verification': '1'}
LOG_TAIL_LINES, LOG_TAIL_CHARS = 200, 64 * 1024        # the driver caps the log at 1 MiB; the host reads only the tail
LIMITS = {'network': 'none', 'user': NOBODY, 'non_root': True, 'read_only_root': True, 'cap_drop': ['ALL'], 'no_new_privileges': True,
          'memory_mb': MEMORY_MB, 'cpus': CPUS, 'pids_limit': PIDS, 'case_timeout_seconds': CASE_TIMEOUT_SECONDS,
          'batch_seconds': BATCH_SECONDS, 'source_mount': 'read-only', 'scratch': 'tmpfs /tmp 16m noexec',
          'environment': 'minimal, no host variables', 'image_pull': 'never', 'dependency_installation': 'none'}


def command_for(spec):
    entry = '/work/' + spec.entry
    if spec.language == 'python':
        if spec.mode == 'check':
            return ['python', '-I', '-c', f'import ast; ast.parse(open({entry!r}, encoding="utf-8").read())']
        return ['python', '-I', entry]
    if spec.mode == 'check':
        return ['node', '--check', entry]
    return ['node', '--permission', f'--allow-fs-read={entry}', entry]


def container_kwargs(image_id, command, host_dir):
    """Every argument of the verification container; nothing else is passed."""
    return {'image': image_id, 'command': list(command), 'detach': True, 'labels': dict(LABEL), 'network_mode': 'none', 'read_only': True,
            'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges'], 'user': NOBODY, 'pids_limit': PIDS, 'mem_limit': f'{MEMORY_MB}m',
            'memswap_limit': f'{MEMORY_MB}m', 'nano_cpus': int(CPUS * 1e9), 'working_dir': '/work', 'tmpfs': dict(TMPFS),
            'log_config': {'type': 'json-file', 'config': {'max-size': '1m', 'max-file': '1'}},
            'environment': {'HOME': '/tmp', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'},
            'volumes': {str(host_dir): {'bind': '/work', 'mode': 'ro'}}}


def _is_timeout(error):
    """docker-py reports wait(timeout=) as a requests ConnectionError wrapping a read timeout, so look through the whole chain."""
    seen = 0
    while error is not None and seen < 6:
        if isinstance(error, TimeoutError) or 'Timeout' in type(error).__name__ or 'timed out' in str(error).lower():
            return True
        error, seen = error.__cause__ or error.__context__, seen + 1
    return False


class ContainerRunner:
    def __init__(self, client, profile):
        self.client, self.profile, self._checked = client, profile, {}

    def info(self, language=None):
        if language is not None and language not in self.profile:
            return {'kind': 'docker', 'profile': PROFILE, 'language': language, 'image': None, 'image_digest': None,
                    'images': {name: dict(value) for name, value in sorted(self.profile.items())}, 'limits': dict(LIMITS)}
        language = language if language in self.profile else sorted(self.profile)[0]
        pin = self.profile[language]
        return {'kind': 'docker', 'profile': PROFILE, 'language': language, 'image': pin['reference'], 'image_digest': pin['image_id'],
                'images': {name: dict(value) for name, value in sorted(self.profile.items())},
                'limits': dict(LIMITS)}

    def _image(self, language):
        """The pinned image id, or an error string. Looks up locally only; a missing image is never pulled."""
        if language in self._checked:
            return self._checked[language]
        pin = self.profile.get(language)
        if pin is None:
            result = (None, f'no pinned image for {language}')
        else:
            try:
                image = self.client.images.get(pin['reference'])
            except Exception:
                result = (None, f'image {pin["reference"]} is not present locally; images are never pulled by the verifier')
            else:
                result = ((image.id, None) if image.id == pin['image_id']
                          else (None, f'image {pin["reference"]} id {image.id} differs from the pinned {pin["image_id"]}'))
        self._checked[language] = result
        return result

    def run(self, spec):
        image_id, problem = self._image(spec.language)
        if problem:
            return RunResult(outcome='env_error', detail=problem)
        host_dir = Path(tempfile.mkdtemp(prefix='ztsa-verify-'))
        started, container = time.monotonic(), None
        try:
            os.chmod(host_dir, 0o755)           # mkdtemp is 0700; the container runs as nobody and must be able to read the mount on a Linux host
            for name, text in spec.files.items():
                (host_dir / name).write_text(text, encoding='utf-8', newline='\n')
                os.chmod(host_dir / name, 0o644)
            try:
                container = self.client.containers.run(**container_kwargs(image_id, command_for(spec), host_dir))
            except Exception as error:
                return RunResult(outcome='env_error', detail=f'the container could not be started: {error}')
            try:
                status = container.wait(timeout=spec.timeout)
            except Exception as error:
                try:
                    container.kill()
                except Exception:      # best effort: the run already failed
                    pass
                if _is_timeout(error):
                    return RunResult(outcome='timeout', detail=f'no result within {spec.timeout} s', seconds=time.monotonic() - started)
                return RunResult(outcome='env_error', detail=f'waiting for the container failed: {type(error).__name__}: {str(error)[:200]}')
            code = int(status.get('StatusCode', 1)) if isinstance(status, dict) else int(status)
            try:
                stdout = container.logs(stdout=True, stderr=False, tail=LOG_TAIL_LINES).decode('utf-8', 'replace')[-LOG_TAIL_CHARS:]
                stderr = container.logs(stdout=False, stderr=True, tail=LOG_TAIL_LINES).decode('utf-8', 'replace')[-LOG_TAIL_CHARS:]
            except Exception:
                stdout = stderr = ''
            outcome = classify_process(code, stderr, False, stdout=stdout, nonce=spec.nonce)
            return RunResult(outcome=outcome, exit_code=code, detail=(stderr or stdout).strip()[-400:], seconds=time.monotonic() - started)
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:      # best effort cleanup
                    pass
            shutil.rmtree(host_dir, ignore_errors=True)


def _docker_client():
    import docker
    return docker.from_env()


def build_runner(profile_path, *, client=None):
    """A ``ContainerRunner`` when the pinned profile is valid and the daemon answers; otherwise an ``UnavailableRunner`` saying why."""
    path = Path(profile_path)
    if not path.is_file():
        return UnavailableRunner(f'no pinned image profile at {path}')
    try:
        profile = json.loads(path.read_text(encoding='utf-8'))
        for language, pin in profile.items():
            if not (isinstance(pin, dict) and pin.get('reference') and str(pin.get('image_id', '')).startswith('sha256:')):
                return UnavailableRunner(f'the pinned image profile entry for {language} needs reference and a sha256 image_id')
    except (OSError, ValueError, AttributeError) as error:
        return UnavailableRunner(f'the pinned image profile cannot be read: {error}')
    if not profile:
        return UnavailableRunner('the pinned image profile is empty')
    try:
        client = client or _docker_client()
        client.ping()
    except ImportError:
        return UnavailableRunner('the docker SDK is not installed')
    except Exception as error:
        return UnavailableRunner(f'the docker daemon is not reachable: {error}')
    return ContainerRunner(client, profile)
