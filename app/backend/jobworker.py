"""Entry point of a background job process (started by ``jobs.start``).

Usage: ``python -m app.backend.jobworker <stage>`` with the stage parameters as JSON
on stdin. Output goes to stdout, which the parent streams into the job log.
"""
import json
import os
import sys
import traceback


def main() -> int:
    stage = sys.argv[1]
    os.environ["ZTSA_JOB_STAGE"] = stage
    if stage == "campaign_run":
        # Before numpy is imported anywhere in this process (its parser and publication children inherit it): see memory_profile.SINGLE_THREAD_ENV.
        from app.backend.pipeline.acceleration.memory_profile import limit_threads
        limit_threads(os.environ)
    raw = sys.stdin.read()
    params = json.loads(raw) if raw.strip() else {}
    limit=os.environ.get("ZTSA_CAMPAIGN_RAM_CAP_BYTES")
    if stage=="campaign_run" and limit:
        from app.backend.pipeline.acceleration.windows_job import install_campaign_limit
        install_campaign_limit(int(limit))
        from app.backend.pipeline.acceleration.parser_tasks import initialize
        initialize()
    if stage in ('qualify_npm_dataset', 'evidence_recovery') and os.name == 'nt':
        from app.backend.pipeline.acceleration.windows_job import install_verification_limit
        from app.backend.pipeline.acceleration.resource_control import CAP_BYTES
        install_verification_limit(CAP_BYTES)
    from app.backend import compute, jobs
    compute.lower_priority()
    try:
        jobs.STAGES[stage](params)
    except KeyboardInterrupt:
        return 130
    except Exception:
        traceback.print_exc(file=sys.stdout)
        return 1
    finally:
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # A thread abandoned by a watchdog (e.g. a stuck network call) must not keep the
    # process - and the job - alive: exit without waiting for it.
    os._exit(code)
