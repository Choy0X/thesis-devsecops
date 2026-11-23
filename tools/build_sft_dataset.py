"""Owned, checkpointed SFT audit phases. This tool never starts model training."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe(root, path):
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('SFT phase path is outside its workspace')
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('SFT phase paths cannot use symlinks or junctions')
    return path


def _write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    if temporary.is_symlink():
        raise ValueError('Unsafe SFT metadata temporary path')
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    # Windows readers can briefly deny delete sharing during an atomic replace.
    # Retain the complete old receipt until replacement succeeds; real denials
    # still fail after a bounded retry rather than becoming admission authority.
    for attempt in range(8):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(.02 * 2 ** attempt, .5))


def _read(path):
    if path.stat().st_size > 128 << 20:
        raise ValueError('SFT phase receipt exceeds its bounded size')
    return json.loads(path.read_text(encoding='utf-8'))


def run_action(args, root, report, check):
    meta = _safe(root, root / getattr(args, 'state_dir', '.recovery/sft-eligibility'))
    processed = root / 'data/processed'
    if args.phase == 'audit':
        from app.backend.pipeline import sft_dataset_build
        continuation = {'resume_incomplete': True} if getattr(args, 'resume_incomplete', False) else {}
        if getattr(args, 'migrate_output_layout', False):
            continuation['migrate_output_layout'] = True
        if getattr(args, 'admission_mode', 'all-mined') == 'evidence-grounded':
            from app.backend.pipeline import sft_admission, sft_views
            continuation.update(admit=sft_admission.admit_record, view_builder=sft_views.build_views)
        return sft_dataset_build.build(args.source_tag, root=processed, progress=report,
                                       cancelled=lambda: not check(), **continuation)
    staged = _read(_safe(root, meta / 'audit_result.json'))
    stage = _safe(processed / '.sft_build', Path(staged['path']))
    if args.phase == 'tokens':
        from transformers import AutoTokenizer
        from app.backend.pipeline.sft_token_audit import audit
        identity = _read(_safe(root, meta / 'tokenizer-identity.json'))
        tokenizer = AutoTokenizer.from_pretrained(str(_safe(root, meta / 'tokenizer')),
                                                  local_files_only=True, trust_remote_code=False, use_fast=True)
        return audit(stage, tokenizer=tokenizer, tokenizer_identity=identity, check=check,
                     progress=lambda event: report(**event))
    if args.phase == 'publish':
        from app.backend.pipeline import sft_dataset_build
        audit = _read(_safe(root, meta / 'tokens_result.json'))
        return sft_dataset_build.finalize(staged, audit, root=processed, cancelled=lambda: not check())
    from app.backend.pipeline.finetune_sft_run import prepare
    published = _read(_safe(root, meta / 'publish_result.json'))
    _safe(processed, Path(published['path']))
    return prepare(published['tag'], root=root,
                   work_key='sft-' + published['source_identity'], check=check)


def main(argv=None, *, run_action=None, budget_factory=None):
    from app.backend.pipeline.oslock import FileLock
    from app.backend.pipeline.verify.resource_budget import ResourceWait, scope
    import psutil

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('audit', 'tokens', 'publish', 'prepare'))
    parser.add_argument('--workspace', type=Path, default=PROJECT)
    parser.add_argument('--source-tag', default='v18')
    parser.add_argument('--state-dir', default='.recovery/sft-eligibility',
                        help='Workspace-relative directory for owned phase state and local tokenizer')
    parser.add_argument('--admission-mode', choices=('all-mined', 'evidence-grounded'), default='all-mined',
                        help='All mined records are eligible by default; evidence-grounded selects the historical narrow policy')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--resume-incomplete', action='store_true',
                        help='Continue an exact owned incomplete audit without retrying unchanged unknown evidence')
    mode.add_argument('--migrate-output-layout', action='store_true',
                      help='Reuse only the specifically approved storage-only checkpoint migration')
    args = parser.parse_args(argv)
    if args.resume_incomplete and args.phase != 'audit':
        parser.error('--resume-incomplete applies only to the audit phase')
    if args.migrate_output_layout and args.phase != 'audit':
        parser.error('--migrate-output-layout applies only to the audit phase')
    root = args.workspace.absolute()
    _safe(root, root)
    if not root.is_dir():
        raise ValueError('Workspace must already exist')
    meta = _safe(root, root / args.state_dir)
    meta.mkdir(parents=True, exist_ok=True)
    owner_path = _safe(root, meta / 'owner.json')
    progress_path = _safe(root, meta / 'progress.json')
    result_path = _safe(root, meta / (args.phase + '_result.json'))
    lock = FileLock(_safe(root, meta / '.owner.lock'))
    lock.acquire(wait=0)
    receipt = {'schema': 'sft-phase-owner/1', 'pid': os.getpid(),
               'created': psutil.Process().create_time(), 'phase': args.phase,
               'source_tag': args.source_tag, 'workspace': str(root),
               'started': _now(), 'state': 'running', 'training_started': False,
               'resume_incomplete': args.resume_incomplete,
               'migrate_output_layout': args.migrate_output_layout}
    last_sample = last_progress = 0.0
    try:
        _write(owner_path, receipt)
        factory = budget_factory or scope
        group = 'sft-audit:' + hashlib.sha256(str(root).casefold().encode()).hexdigest()
        with factory(group, directory=root / 'data/interim/verification-resources') as budget:
            def check(force=False):
                nonlocal last_sample
                if _safe(root, meta / 'cancel.request').exists():
                    raise InterruptedError('SFT phase cancellation requested; checkpoints retained')
                if force or time.monotonic() - last_sample >= 1:
                    resources = budget.check(require_runtime=False)
                    _write(_safe(root, meta / 'resources.json'), resources)
                    last_sample = time.monotonic()
                return True

            def report(**event):
                nonlocal last_progress
                check()
                if time.monotonic() - last_progress >= 1:
                    allowed = ('state', 'label', 'processed', 'total', 'counts', 'encoded', 'excluded')
                    value = {key: event[key] for key in allowed if key in event}
                    _write(progress_path, {**value, 'phase': args.phase, 'observed_at': _now()})
                    last_progress = time.monotonic()

            check(force=True)
            result = (run_action or globals()['run_action'])(args, root, report, check)
            check(force=True)
            _write(result_path, result)
        receipt.update(state='done', finished=_now(), result=str(result_path))
        _write(owner_path, receipt)
        print(json.dumps({'phase': args.phase, 'state': 'done', 'result': str(result_path),
                          'tag': result.get('tag'), 'training_started': False}))
        return 0
    except (KeyboardInterrupt, InterruptedError) as exc:
        receipt.update(state='cancelled', finished=_now(), error=str(exc))
        _write(owner_path, receipt)
        return 130
    except ResourceWait as exc:
        receipt.update(state='waiting', finished=_now(), code=exc.code, error=exc.reason,
                       resources=exc.resources)
        _write(owner_path, receipt)
        return 75
    except BaseException as exc:
        receipt.update(state='failed', finished=_now(), error=str(exc), error_type=type(exc).__name__)
        _write(owner_path, receipt)
        raise
    finally:
        lock.release()


if __name__ == '__main__':
    raise SystemExit(main())
