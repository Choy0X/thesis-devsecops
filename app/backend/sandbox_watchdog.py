"""Independent process: stops owned Docker work after its application owner exits.

No service installation, global pruning, deletion of volumes or trust in PID alone.
The latest ownership session supersedes an old watchdog after application restart.
"""
from __future__ import annotations
import argparse
import json
import pathlib
import time


def owner_alive(pid, created):
    import psutil
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - created) < .01
    except (psutil.Error, TypeError, ValueError):
        return False


def decision(state, session, alive=owner_alive):
    if state.get('owner_session') != session:
        return 'superseded'
    owner = state.get('owner') or {}
    if alive(owner.get('pid'), owner.get('created')):
        return 'alive'
    if (state.get('restart_handoff_until') or 0) > time.time():
        return 'handoff'
    return 'cleanup'


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', required=True)
    parser.add_argument('--session', required=True)
    args = parser.parse_args(argv)
    path = pathlib.Path(args.state)
    from app.backend.pipeline import fsio, oslock
    retry_seconds = 2
    while True:
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return 1  # No ownership proof: leave Docker untouched.
        action = decision(document, args.session)
        if action == 'superseded':
            return 0
        if action in ('alive', 'handoff'):
            time.sleep(2)
            continue
        # The lock prevents a new owner handoff during the final identity check.
        try:
            with oslock.FileLock(str(path) + '.lock', note=False):
                document = json.loads(path.read_text(encoding='utf-8'))
                if decision(document, args.session) != 'cleanup':
                    continue
                try:
                    import docker
                    from app.backend.sandbox_cleanup import stop_owned
                    report = stop_owned(docker.from_env(timeout=3), installation_id=document['installation_id'],
                                        known_ids={cid for cid, item in document.get('containers', {}).items()
                                                   if item.get('session_id') == args.session})
                    document['cleanup_errors'] = report['errors']
                except Exception as error:
                    document['cleanup_errors'] = [str(error)]
                document['owner_exited'] = True
                fsio.write_json(path, document)
                if not document['cleanup_errors']:
                    return 0
            # Docker can return after the desktop has exited. Keep retrying with
            # backoff, releasing the state lock so a replacement can take over.
            time.sleep(retry_seconds)
            retry_seconds = min(30, retry_seconds * 2)
        except oslock.Busy:
            time.sleep(1)
            continue


if __name__ == '__main__':
    raise SystemExit(main())
