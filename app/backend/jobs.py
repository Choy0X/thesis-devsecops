"""Background job runner with structured logging.

Each pipeline stage runs in its own **child process** (``app.backend.jobworker``).
Its stdout/stderr is read line by line into the job's log (which the WebSocket
streams to the UI) and the finished job is written to a per-job log file under
experiments/logs/. This is the thesis's "structured logging module": every run is
recorded with its stage, parameters, status and timing.

Running stages out of process keeps the API and the UI responsive: a stage that
parses a multi-GB file or holds the GIL for long stretches can no longer freeze
the app, and a running job can be cancelled by terminating its process. A stage
reports structured progress (batch, counts, a wait deadline) with ``progress()``;
those lines are kept out of the log and exposed as ``job.progress``.

Pipeline modules are imported lazily inside each stage so the API starts even when
an optional heavy dependency (openai, chromadb, sentence-transformers, docker) is
not installed.
"""
from __future__ import annotations
import atexit
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import datetime
import pathlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.backend import termtext
from app.backend.joblog import JobLog

LOG_DIR = pathlib.Path("experiments/logs")
MAX_FINISHED = 200       # finished jobs kept in the registry (the newest); their records stay on disk
RECORD_TAIL = 2000       # log lines copied into a job's record; the whole log is in <id>.log
RECORD_EVERY = 5.0       # seconds between rewrites of a running job's record (its progress)
_RECORD_NAME = re.compile(r"^[0-9a-f]{12}\.json$")       # the folder also holds phase_transitions.jsonl and campaign-*.log
PROGRESS_MARK = "\x1e@progress "
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# A job is background work: it runs below normal priority so a CPU-bound stage cannot starve the window and the API that serves it.
_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
MAX_RUNNING_DEFAULT = 4          # jobs running at once; ZTSA_MAX_JOBS changes it
_ROOT = pathlib.Path(__file__).resolve().parents[2]      # project root (holds app/)


@dataclass
class Job:
    id: str
    stage: str
    params: dict
    status: str = "running"          # running | done | error | cancelled | interrupted (the app closed while it ran)
    logs: Any = field(default_factory=JobLog)       # a JobLog (a list given here is wrapped)
    started: str = ""
    finished: str = ""
    error: str = ""
    progress: dict = field(default_factory=dict)
    compute: dict = field(default_factory=dict)      # the device asked for and the one that ran it (``used``), and the measured seconds, for a job that has a choice of device
    proc: Any = field(default=None, repr=False)
    cancelled: bool = False
    saved_at: float = field(default=0.0, repr=False)        # when the record on disk was last written
    thread: Any = field(default=None, repr=False)           # the background thread running _run(); join() is the only race-free way to know its finally: block (incl. save_log) is done

    def __setattr__(self, name, value):
        if name == "logs" and not isinstance(value, JobLog):        # a plain list of lines is wrapped, whenever it is assigned
            value = JobLog(value)
        object.__setattr__(self, name, value)

    def public(self) -> dict:
        """Summary without the log lines (GET /api/jobs/{id} adds them)."""
        out = {"id": self.id, "stage": self.stage, "params": self.params,
               "status": self.status, "started": self.started, "finished": self.finished,
               "error": self.error, "log_count": len(self.logs), "progress": self.progress}
        if self.compute:
            out["compute"] = self.compute
        return out


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


class TooManyJobs(RuntimeError):
    """Raised by ``start`` when the limit on jobs running at once is reached (the API answers 409)."""


class Blocked(RuntimeError):
    """Raised by ``start`` when a campaign holds an ecosystem the job would work on (the API answers 409)."""


def max_running() -> int:
    """How many jobs may run at once: ``ZTSA_MAX_JOBS`` (1 to 16), else 4. Each is a process; the cores and the disk are shared."""
    try:
        return max(1, min(16, int(os.environ.get("ZTSA_MAX_JOBS", MAX_RUNNING_DEFAULT))))
    except ValueError:
        return MAX_RUNNING_DEFAULT


def report_device(used: str) -> None:
    """From inside a stage: the device that actually ran the job ("cpu" or "gpu"). The job record keeps it beside the device that was asked for and the measured time."""
    progress(device_used=used)


def progress(**state) -> None:
    """Report structured progress from inside a stage (shown above the job log).

    Typical keys: ``state`` (mining | pause | rate_limited | done), ``label``,
    ``processed``/``total``, ``batch``/``batches``, ``counts``, ``until`` (epoch
    seconds of the end of a wait) and ``reason``."""
    state.setdefault("ts", time.time())
    sys.stdout.write(PROGRESS_MARK + json.dumps(state, default=str) + "\n")
    sys.stdout.flush()


# ── Stage registry (lazy imports inside) ───────────────────────────────────────

def _stage_osv_download(p):
    """Download (or, with ``refresh``, update) the OSV dump of one ecosystem and nothing else; the dump is kept for the next ingest."""
    from app.backend.pipeline.ingest import osv, osv_catalog
    eco = str(p.get("ecosystem") or "").strip()
    if not eco:
        print("osv_download needs an ecosystem.")
        raise SystemExit(1)
    known, _source = osv_catalog.remote_list()
    if known and eco not in known:                       # the name becomes part of the address, so only names OSV publishes are used
        print(f"{eco} is not an ecosystem OSV publishes.")
        raise SystemExit(1)
    progress(state="downloading", label=f"Downloading the {eco} dump")
    path = osv.download(eco, refresh=bool(p.get("refresh", False)))
    progress(state="done", label=f"The {eco} dump is ready", processed=1, total=1)
    print(f"{eco}: {path.name}, {path.stat().st_size / 1e6:.1f} MB")


def _stage_osv_install(p):
    """Download (or, with ``refresh``, update) one ecosystem's OSV dump and read it into the table Find an advisory searches: two steps, each with real numbers (bytes, then files). Nothing is
    written to the working store; that is the ingest of a campaign. With ``skip_download`` only the second step runs. Both steps keep their progress: a download that was cut continues from the bytes
    already on the disk and a read that was cut from the last checkpoint ("Continuing ..."); ``fresh`` throws that saved work away first (Start over)."""
    from app.backend.pipeline.ingest import osv, osv_catalog, osv_read
    eco = str(p.get("ecosystem") or "").strip()
    if not eco:
        print("osv_install needs an ecosystem.")
        raise SystemExit(1)
    known, _source = osv_catalog.remote_list()
    if known and eco not in known:                       # the name becomes part of the address, so only names OSV publishes are used
        print(f"{eco} is not an ecosystem OSV publishes.")
        raise SystemExit(1)
    skip_download = bool(p.get("skip_download"))
    fresh = bool(p.get("fresh"))
    steps = 1 if skip_download else 2
    dump = osv.dump_path(eco)
    outcome = "cached"
    percent = lambda done, total: f" from {min(99, int(100 * done / total))}%" if total else ""      # noqa: E731
    try:
        if fresh:
            osv.discard_partial(eco)                     # the saved work of a read
        osv.recover_download(eco)                        # a download that was cut after its last byte but before its files were written
        if skip_download:
            if not dump.is_file():
                print(f"The {eco} dump is not on this computer, so there is nothing to read.")
                raise SystemExit(1)
        else:
            started = time.monotonic()
            resumed = {"from": 0}

            def on_resume_download(offset, total):
                resumed["from"] = offset
                print(f"{eco}: continuing the download{percent(offset, total)}")
                progress(state="downloading", phase="download", step=1, steps=steps, label=f"Continuing the {eco} download{percent(offset, total)}", unit="bytes", processed=offset,
                         total=total, resumed_from=offset, resumed=True)

            def on_bytes(done, total):
                elapsed = max(time.monotonic() - started, 0.001)
                rate = max(0, done - resumed["from"]) / elapsed                # only what this run fetched counts for the speed
                progress(state="downloading", phase="download", step=1, steps=steps, label=f"{'Continuing the' if resumed['from'] else 'Downloading the'} {eco} {'download' if resumed['from'] else 'dump'}",
                         unit="bytes", processed=done, total=total, rate=round(rate), eta=round((total - done) / rate) if total and rate > 0 and total >= done else None,
                         resumed_from=resumed["from"] or None)
            progress(state="downloading", phase="download", step=1, steps=steps, label=f"Downloading the {eco} dump", unit="bytes", processed=0, total=None)
            result = osv.download_result(eco, refresh=bool(p.get("refresh", False)), progress=on_bytes, on_resume=on_resume_download, fresh=fresh)
            dump, outcome = result["path"], result["outcome"]
            if outcome in ("downloaded", "unchanged"):
                osv_catalog.mark_current(eco)
            print(f"{eco}: {dump.name}, {dump.stat().st_size / 1e6:.1f} MB ({'already the newest copy' if outcome == 'unchanged' else 'on this computer' if outcome == 'cached' else 'downloaded'})")
        table = osv.table_path(eco)
        if not fresh and outcome != "downloaded" and table.is_file() and table.stat().st_mtime >= dump.stat().st_mtime:
            try:
                osv_read.discard(table)                  # a work folder that a finished read left behind
            except (OSError, osv.ReadBusy):
                pass                                     # ignored: it is removed by the next read
            progress(state="done", phase="read", step=steps, steps=steps, label=f"The {eco} dump is ready and already read", processed=1, total=1)
            print(f"{eco}: already read, nothing to do")
            return
        read_from = {"at": 0}

        def on_resume_read(nxt, total):
            read_from["at"] = nxt
            print(f"{eco}: continuing to read the dump{percent(nxt, total)}")
            progress(state="reading", phase="read", step=steps, steps=steps, label=f"Continuing to read the {eco} dump{percent(nxt, total)}", unit="files", processed=nxt, total=total,
                     resumed_from=nxt, resumed=True)

        def on_file(done, total, rows, phase="read"):
            # once every file is read, "reading" would otherwise go quiet while the table is actually put together (a real wait on a large ecosystem): the count keeps climbing past the
            # file total instead, on a label that says what is really happening (see osv_read.run), so a long dump is never silently indistinguishable from a stuck one.
            label = f"Putting the {eco} dump together" if phase == "assembling" else f"{'Continuing to read' if read_from['at'] else 'Reading'} the {eco} dump"
            progress(state="assembling" if phase == "assembling" else "reading", phase=phase, step=steps, steps=steps, label=label, unit="files", processed=done, total=total,
                     counts={"rows": rows}, resumed_from=read_from["at"] or None)
        progress(state="reading", phase="read", step=steps, steps=steps, label=f"Reading the {eco} dump", unit="files", processed=0, total=None)
        ref = osv.parse(dump, eco, progress=on_file, frame=False, fresh=fresh, on_resume=on_resume_read)
        progress(state="done", phase="read", step=steps, steps=steps, label=f"The {eco} dump is ready: {ref.rows:,} rows read", processed=1, total=1)
    except (osv.ReadBusy, osv.DownloadBusy) as e:
        print(f"{e}. Wait for it to finish, then run this again.")
        raise SystemExit(1)
    except osv.DumpUnreadable as e:
        print(f"The {eco} dump has a record that cannot be read: {e}. Everything read before it is kept; Continue starts there again, Start over reads the dump from its beginning.")
        raise SystemExit(1)


def _stage_ingest(p):
    """OSV advisories per ecosystem (+ NVD). With ``since`` and ``only_since``, only
    CVEs published on or after that date are kept: older OSV advisories are not
    ingested and older records already in the store are removed (hand-made and
    imported records are kept; mined code stays in the code store)."""
    import functools
    from app.backend.pipeline.ingest import osv, fixrefs, report as ingest_report
    from app.backend.pipeline import store
    import pandas as pd
    ecosystems = p.get("ecosystems") or osv.default_ecosystems()
    refresh = bool(p.get("refresh"))
    fetch = functools.partial(osv.fetch_table, refresh=True) if refresh else osv.fetch_table        # a worker runs it: top level function, picklable; it only writes the table and answers where it is
    ran: list[dict] = []
    since = str(p.get("since") or "")[:10] or None
    only_since = bool(p.get("only_since")) and since is not None
    if only_since:
        print(f"keeping only CVEs published since {since}")
    from app.backend import compute
    width = max(1, min(compute.cpu_workers(p.get("workers")), len(ecosystems), 4))       # downloads and parses at once; a window bounds the memory
    with compute.Pool(width) as pool:        # one set of workers for every window: each worker imports the pipeline once, not once per window
        for window in compute.chunks(ecosystems, width):
            # download and parse in worker processes (the zips are large and parsing is pure Python); the store is written here, one at a time
            for eco, table, error in pool.map(fetch, window, min_items=2):
                if error is not None:
                    print(f"{eco}: OSV ingest failed (non-fatal): {error}")
                    ran.append(ingest_report.ecosystem_row(eco, error=error))
                    continue
                rows_read = osv.row_count(table)
                print(f"{eco}: ingest complete: {rows_read} rows read")
                if osv.layout_of(eco) == "advisory":      # no adapter: its advisories list dozens of packages each; the table is for searching, the store is not written
                    print(f"{eco}: has no adapter, so it is kept as a table to search and is not ingested into the working store")
                    ran.append(ingest_report.ecosystem_row(eco, rows=rows_read, kept=0, advisories=0, count_before=store.count(), count_after=store.count()))
                    continue
                before = store.count()
                n = kept = left_out = 0
                for df in osv.frames(table):              # whole advisories at a time, so a huge dump is never one frame
                    if only_since and len(df):
                        pub = pd.to_datetime(df["published"], errors="coerce", utc=True, format="mixed")
                        keep = pub.isna() | (pub >= pd.Timestamp(since, tz="UTC"))
                        left_out += int((~keep).sum())
                        df = df[keep]
                    kept += len(df)
                    n += store.upsert_many(df, source="osv")
                if only_since:
                    print(f"{eco}: {left_out:,} rows published before {since} left out")
                after = store.count()
                ran.append(ingest_report.ecosystem_row(eco, rows=rows_read, kept=kept, advisories=n, count_before=before, count_after=after))
                print(f"{eco}: working store updated: {n} advisories, {ran[-1]['new']} new and {ran[-1]['updated']} updated (total now {after})")

    if ran:
        ingest_report.record(ran, since=since, refresh=refresh)
    if only_since:
        removed = store.prune_before(since)
        print(f"removed {removed:,} records published before {since} "
              "(hand-made / imported records kept; their mined code stays in the code store)")
    if since:
        # the collection window of the dataset starts at the "since" date
        store.set_meta("collection_since", since)
        store.set_meta("collection_at", datetime.datetime.utcnow().date().isoformat())
    if p.get("nvd"):
        try:
            from app.backend.pipeline.ingest import nvd
            nvd.flatten(nvd.harvest(since or "2016-01-01"), since=since if only_since else None)
            print("NVD enrichment written to data/interim/nvd.parquet "
                  "(merged on the next dataset build)")

        except Exception as e:
            print(f"NVD skipped (non-fatal): {e}")
    print("queueing the fix commits of the Dataset records ...")
    fixrefs.queue_from_records()


def _stage_extract_fixes(p):
    """Queue the fix commits of the Dataset records (their OSV pointers plus NVD
    references of their CVEs); nothing outside the records is queued."""
    from app.backend.pipeline.ingest import fixrefs
    fixrefs.queue_from_records(progress=progress)


def _stage_mine_code(p):
    from app.backend.pipeline.ingest import codemine
    from app.backend.pipeline import mining_run
    try:
        _mine(codemine, p)
    except Exception:
        rec = mining_run.load()
        if rec and rec.get("pid") == os.getpid() and rec.get("state") == "running":
            mining_run.finish("error")
        raise


def _mine(codemine, p):
    codemine.mine(limit=int(p["limit"]) if p.get("limit") else None,
                  workers=int(p.get("workers", 4)),
                  retry_failed=bool(p.get("retry_failed", False)),
                  max_files=int(p.get("max_files", 40)),
                  max_functions=int(p.get("max_functions", 20)),
                  batch_size=int(p.get("batch_size", 200)),
                  pause_seconds=float(p.get("pause_seconds", 60)),
                  until_done=bool(p.get("until_done", True)),
                  commit_timeout=float(p.get("commit_timeout", 600)),
                  progress=progress)
    # fix-commit check and function / hunk split of everything mined (no re-mining)
    codemine.maintain(progress=progress)
    if p.get("collect_deps", True):
        # then affected / fixed / safe versions of the records whose fix was mined;
        # saved per record, so a stopped run continues with the unchecked ones
        from app.backend.pipeline.ingest import depversions
        depversions.collect(workers=int(p.get("deps_workers", 24)), progress=progress,
                            only_mined=True)


def _stage_verify_fixes(p):
    """Release-date check of every mined fix commit (nothing is re-mined)."""
    from app.backend.pipeline.ingest import fixcheck
    fixcheck.verify_all(only_unchecked=bool(p.get("only_unchecked", False)),
                        workers=int(p.get("workers", 8)), progress=progress)


def _stage_resplit_pairs(p):
    """Function / hunk pairs from the stored file-level pairs (no re-mining)."""
    from app.backend.pipeline.ingest import codemine
    codemine.resplit(max_functions=int(p.get("max_functions", 20)), progress=progress, workers=p.get("workers"))


def _stage_dep_versions(p):
    """Only the dependency-version check (affected -> fixed / safe version), by
    default for the records whose fix was mined, like Mine."""
    from app.backend.pipeline.ingest import depversions
    only_mined = bool(p.get("only_mined", True))
    if p.get("refresh"):
        from app.backend.pipeline import store
        store.reset_deps_checked(only_mined)
    depversions.collect(workers=int(p.get("workers", 24)), progress=progress, only_mined=only_mined)


def _gpu_job(kind, p):
    from app.backend.pipeline import finetune_prep as fp, gpu_local
    run = fp.check_run_name(p.get("run") or "pilot")
    device = p.get("device") or "gpu"
    report_device(device)
    report = gpu_local.run(kind, run_dir=fp.RUNS / run, model=p.get("model") or gpu_local.DEFAULT_MODEL, steps=int(p.get("steps", 3)),
                           max_views=int(p.get("max_views", 8)), device=device)
    print(f"local {'GPU' if device == 'gpu' else 'CPU'} {kind} of {run} on {report['gpu']['name'] if device == 'gpu' else 'the processor'}: {'passed' if report['passed'] else 'FAILED'} in {report['seconds']} s"
          + (f", peak {report['peak_vram_mb']} MB, {report['tokens_per_second']} tokens/s" if report.get("peak_vram_mb") else ""))
    if not report["passed"]:
        print(report["output_tail"])
        raise RuntimeError(f"the local GPU {kind} failed")


def _stage_gpu_smoke(p):
    """A few QLoRA steps of a small public model on the local GPU (software check, dev-only model; see gpu_local)."""
    _gpu_job("smoke", p)


def _stage_gpu_eval(p):
    """The untouched small model answers a few validation views on the local GPU (software check; see gpu_local)."""
    _gpu_job("eval", p)


def _stage_train_local(p):
    """Train a prepared run on the local GPU in a stable folder, so that a cancelled or stopped run resumes from its last checkpoint (see gpu_local.train).
    With ``import`` set, a run that finished is checked and kept like an outputs.zip from Colab."""
    from app.backend.pipeline import finetune_import as fi, finetune_prep as fp, gpu_local
    run = fp.check_run_name(p.get("run") or "pilot")
    report_device("gpu")
    report = gpu_local.train(run_dir=fp.RUNS / run, model=p.get("model") or None, steps=int(p["steps"]) if p.get("steps") else None,
                             stage=int(p.get("stage", 1)), eval_max=int(p["eval_max"]) if p.get("eval_max") else 16,
                             session_minutes=float(p["session_minutes"]) if p.get("session_minutes") else None, loss_mode=p.get("loss_mode") or None,
                             on_line=print, on_progress=progress)
    print(f"local training of {run} ({report['model']}): {report['state']}, step {report['steps_done']} of {report['steps']} in {report['seconds']} s"
          + (f", resumed from step {report['resumed_from_step']}" if report.get("resumed_from_step") else ""))
    if not report["passed"]:
        print(report["output_tail"])
        raise RuntimeError("the local training failed")
    if not report["complete"]:
        print("The run stopped before its last step and left a checkpoint: start it again to continue from there.")
        return
    if p.get("import") and report.get("outputs_zip"):
        try:
            rec = fi.import_outputs(report["outputs_zip"], fp.RUNS / run)
        except fi.ImportRejected as e:
            for f in e.failures:
                print(f"REJECTED {f['check']}: {f['detail']}")
            raise
        print(f"imported {rec['import_id']} into {run}: type {rec['type']}" + (" (already imported)" if rec["already_imported"] else ""))


def _stage_db_backup(p):
    """Dump the application database inside its container, prove the archive readable and write a receipt beside it."""
    from app.backend.pipeline.db import backup
    progress(state="backup", label="Dumping the database")
    receipt = backup.backup(p.get("database") or None)
    print(f"backup {receipt['file']}: {receipt['documents']} documents in {len(receipt['collections'])} collections, {receipt['bytes']} bytes, "
          f"sha256 {receipt['sha256'][:16]}, read back and verified")


def _stage_build_code_dataset(p):
    """Build the code dataset from the Dataset records with mined code; the
    filters are those of build_code_dataset.DEFAULTS."""
    from app.backend.pipeline.ingest import build_code_dataset, codemine
    codemine.maintain(progress=progress)            # results of an older check / split
    filters = {k: v for k, v in (p.get("filters") or {}).items()
               if k in build_code_dataset.DEFAULTS}
    build_code_dataset.main(freeze=bool(p.get("freeze", False)), filters=filters)


def _stage_context_review_export(p):
    from app.backend.pipeline.context_json_export import default
    result = default().prepare(p['export_id'], progress=progress)
    print(json.dumps(result, default=str), flush=True)
    if result['state'] != 'complete':
        raise RuntimeError('JSON export is incomplete; completed files are retained. Retry the export to resume.')


def _stage_review_exchange_export(p):
    from app.backend.pipeline.review_exchange import default
    result = default().prepare(p['export_id'], progress=progress)
    if result['state'] != 'complete':
        raise RuntimeError('Review export is incomplete; completed files remain available. Resume to retry.')


def _stage_review_exchange_validate(p):
    _stage_review_exchange_task('validation', p)


def _stage_review_exchange_import(p):
    _stage_review_exchange_task('import', p)


def _stage_review_exchange_task(kind, p):
    from app.backend.pipeline.review_exchange_jobs import ReviewTasks
    result = ReviewTasks().run(kind, p['task_id'], progress=progress)
    if result['state'] != 'complete':
        raise RuntimeError(result.get('error') or 'Review task did not complete')


def _stage_context_review_local(p):
    from app.backend.pipeline import context_exchange
    ids = p.get('ids')
    if not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids):
        raise ValueError('Select positive integer pair IDs for local context generation.')
    result = context_exchange.generate_local(ids, progress=progress, expected_model=p.get('model'), force=bool(p.get('force')),
                                             campaign_id=p.get('campaign_id'))
    print(json.dumps(result))
    if result.get('failed') and not result.get('completed'):
        raise SystemExit(1)


def _stage_enrich_pairs(p):
    """LLM enrichment: the pairs chosen in the candidate list (``ids``), or up to
    ``limit`` candidates matching ``filters`` (weakest context first)."""
    from app.backend.pipeline.ingest import enrich
    from app.backend.pipeline import context_exchange
    if p.get('ids'):
        ids = [int(i) for i in p['ids']]
    else:
        filters = dict(p.get('filters') or {})
        if p.get('languages'):
            filters['languages'] = p['languages']
        selected, _ = enrich.select_candidates(filters)
        ids = selected.head(min(1000, int(p.get('limit', 50)))).pair_id.astype(int).tolist() if len(selected) else []
    result = context_exchange.generate_configured(ids, progress=progress, campaign_id=p.get('campaign_id'),
                                                  expected_model=p.get('model'), force=bool(p.get('overwrite')))
    print(json.dumps(result))
    if result.get('failed'):
        raise SystemExit(1)
    return

def _stage_patch_eval(p):
    """Patch-generation baseline with the configured model over the code-pair validation split."""
    from app.backend.pipeline.patch import api_baseline
    langs = p.get("languages") or None
    if isinstance(langs, str):
        langs = [x.strip() for x in langs.split(",") if x.strip()]
    api_baseline.run(n=int(p.get("n", 100)), split=p.get("split", "val"), languages=langs,
                     granularity=p.get("granularity", "function") or None,
                     tag=p.get("tag") or None, dataset=p.get("dataset") or None,
                     workers=int(p.get("workers", 2)))


def _stage_eval_patch(p):
    """Score imported patch predictions (e.g. the fine-tuned model from Colab)."""
    import pandas as pd
    from app.backend.pipeline.eval import patch_metrics
    tag = p["tag"]
    df = pd.read_csv(pathlib.Path("experiments") / tag / "predictions.csv")
    if "prediction" not in df.columns and "pred" in df.columns:
        df = df.rename(columns={"pred": "prediction"})
    patch_metrics.evaluate_patches(df, tag, p.get("dataset") or None)


def _stage_eval_detection(p):
    """Score imported detection predictions (vulnerable / safe)."""
    import pandas as pd
    from app.backend.pipeline.eval import detection_metrics
    tag = p["tag"]
    df = pd.read_csv(pathlib.Path("experiments") / tag / "predictions.csv")
    detection_metrics.evaluate(df, tag, p.get("dataset") or None)


def _stage_compare(p):
    """Original vs fine-tuned model: two scored runs side by side."""
    from app.backend.pipeline.eval import compare
    compare.compare(p["original"], p["fine_tuned"], p.get("tag") or None)


def _stage_dataset(p):
    from app.backend.pipeline.ingest import build_dataset
    build_dataset.main(freeze=bool(p.get("freeze", False)),
                       val_share=float(p.get("val_share", 0.05)),
                       train_all=bool(p.get("train_all", False)))


def _stage_rag_index(p):
    raise ValueError("RAG training and evaluation workflows are retired; use agentic Projects retrieval.")
    from app.backend.pipeline.rag import index
    index.main(p.get("kb_version") or None, progress=progress)


def _stage_rag_index_project(p):
    """Refresh the project's disposable local search cache."""
    from app.backend.pipeline import legacy_project_search, projects, util
    project = projects.get(str(p.get("project") or ""))
    started = util.now_zulu()
    state = legacy_project_search.refresh(project)
    summary = f"Local search cache covers {state['files']} files."
    projects.audit(project["id"], tool="project index", summary=summary,
                   started=started, ended=util.now_zulu(), exit_status=0)
    print(summary)


def _stage_rag_recall(p):
    raise ValueError("RAG training and evaluation workflows are retired; use agentic Projects retrieval.")
    """recall@k of the retrieval index on validation rows, as its own run."""
    from app.backend.pipeline import runs
    from app.backend.pipeline.rag import kb, retrieve
    k, n = int(p.get("k", 5)), int(p.get("n", 200))
    score = retrieve.recall_at_k(n=n, k=k)
    tag, d = runs.new_run(p.get("tag") or f"rag_recall_k{k}")
    state = kb.status()
    runs.write_config(d, {"task": "rag_recall", "k": k, "n": n, "exact_search": True,
                          "retrieval": {"embedder": (state.get("embedder") or {}).get("key"), "kb_version": state.get("kb_version"),
                                        "collection": state.get("collection")}})
    (d / "metrics.json").write_text(json.dumps({"task": "rag_recall", "tag": tag, "k": k,
                                                "n": n, "recall_at_k": score}, indent=2))
    print(f"written to experiments/{tag}/metrics.json")


def _stage_classify(p):
    if p.get("rag"):
        raise ValueError("RAG training and evaluation workflows are retired; use agentic Projects retrieval.")
    from app.backend.pipeline.models import arm_a_api
    arm_a_api.run(n=int(p.get("n", 50)), split=p.get("split", "val"),
                  tag=p.get("tag"), rag=bool(p.get("rag", False)),
                  k=int(p.get("k", 5)),
                  dataset=p.get("dataset", _default_dataset()), workers=p.get("workers"))


def _stage_detect(p):
    """Detection with the model configured in Settings (original or fine-tuned)."""
    from app.backend.pipeline.models import detect_api
    thr = p.get("threshold")
    detect_api.run(n=int(p.get("n", 300)), split=p.get("split", "val"), tag=p.get("tag") or None,
                   dataset=p.get("dataset") or None,
                   threshold=float(thr) if thr not in (None, "") else None, progress=progress, workers=p.get("workers"))


def _stage_eval(p):
    import pandas as pd
    from app.backend.pipeline.eval import metrics
    tag = p["tag"]
    df = pd.read_csv(pathlib.Path("experiments") / tag / "predictions.csv")
    metrics.evaluate(df, tag)            # unanswered rows are counted, not dropped


def _stage_recover_fixes(p):
    """Follow pull requests, closing commits and fixed-version ranges of advisories with no commit
    pointer to a fix commit (GitHub token required), then queue what was found."""
    from app.backend.pipeline.ingest import linkrecovery
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("recover_fixes needs a GitHub token (Settings).")
        raise SystemExit(1)
    out = linkrecovery.run(api=linkrecovery.GitHubClient(token), limit=p.get("limit"),
                           ecosystems=p.get("ecosystems") or None, progress=progress)
    print(f"recovery: {out}")


def _stage_verify_case(p):
    """Verify one case of the saved pilot again in the hardened container (no network for the checks; the environment is built once, which needs the
    internet to fetch the project's packages). The result goes to a folder of its own."""
    from app.backend import sandbox
    from app.backend.pipeline.verify import pilot
    case_id = str(p.get("case_id") or "").strip()
    if not case_id:
        print("verify_case needs a case id.")
        raise SystemExit(1)
    if not sandbox.available():
        print("Docker is not running, so nothing can be verified. Start Docker Desktop and try again.")
        raise SystemExit(1)
    progress(state="verifying", label=f"Verifying {case_id}", processed=0, total=1)
    try:
        out = pilot.rerun_case(case_id)
    except KeyError as e:
        print(str(e).strip("'\""))
        raise SystemExit(1)
    progress(state="done", label=f"{case_id}: {out['result']['outcome']}", processed=1, total=1)


def _stage_hf_download(p):
    """Download one model from the Hugging Face Hub into the cache the Model Hub reads: pinned to the commit the Hub reports now, refused when the disk is too
    small, and for a GGUF-only model only the chosen quantisation. A broken download resumes where it stopped."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")          # before the Hub library is imported: its warning that this computer cannot make symlinks only costs space, and the person cannot act on it
    from app.backend.pipeline import hub_download
    repo_id = str(p.get("repo_id") or "").strip()
    if not repo_id or "/" not in repo_id:
        print("hf_download needs a model name such as org/model.")
        raise SystemExit(1)
    started = time.monotonic()
    try:
        out = hub_download.download(repo_id, p.get("quant") or None, progress=progress, say=print)
    except (ValueError, RuntimeError) as e:
        print(str(e))
        raise SystemExit(1)
    seconds = time.monotonic() - started
    took = f"{seconds:.1f} s" if seconds < 60 else f"{int(seconds // 60)}:{int(seconds % 60):02d}"
    speed = f" ({hub_download.size_text(out['bytes'] / seconds)}/s)" if seconds >= 1 else ""
    progress(state="done", label=f"{repo_id} is downloaded", unit="bytes", processed=out["bytes"], total=out["bytes"], files=out["files"], files_total=out["files"], seconds=round(seconds, 1))
    print(f"Finished  {hub_download.size_text(out['bytes'])} in {took}{speed}, saved to {out['folder']}")


def _stage_install_model(p):
    """Install a model so that it is ready to run: fetch the converter scripts when needed, download it (unless it is already here) and convert it to GGUF, in steps (pipeline/model_install.py).
    The safetensors copy is kept; every reason it cannot be done is said before anything is fetched."""
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")          # before the Hub library is imported (see hf_download)
    from app.backend import device
    from app.backend.config import Settings
    from app.backend.pipeline import derived_cache, model_install
    try:
        model_install.run(p, progress=progress, say=lambda line: print(line, flush=True), device_profile=device.snapshot(), room=derived_cache.for_job(Settings.load().derived_cache_gb))
    except (ValueError, RuntimeError) as e:                       # the install's own refusals, the converter's, the installer's and the Hub's are all sentences
        print(str(e))
        raise SystemExit(1)


def _stage_install_ollama_model(p):
    from app.backend.pipeline.ollama import client, install
    try:
        install.run(p, progress=progress)
    except (ValueError, RuntimeError, OSError, client.OllamaError) as error:
        print(str(error), flush=True)
        raise SystemExit(1) from error


def _stage_phase_audit(p):
    """Aggregate audit of the repository and the mined data -> notes/finetuning_phase_audit.md."""
    from app.backend.pipeline import audit
    audit.run(network=bool(p.get("network", True)))


def _stage_build_reviewed_dataset(p):
    """Records, groups, split manifest, views and a manifest, into data/processed/dataset_<tag>/ (preview by
    default; ``freeze`` takes the next unused version and registers its hashes)."""
    from app.backend.pipeline import build_reviewed
    if p.get("build_id"):
        from app.backend.pipeline import build_catalog
        build_catalog.execute(p["build_id"], root=p.get("build_root"), progress=progress)
        return
    from app.backend.pipeline.automatic_evidence import POLICY
    policy = p.get('admission_policy') or POLICY
    minimum = p.get('min_tier') or ('human_reviewed' if policy == 'legacy-independent/1' else 'auto_checked')
    out = build_reviewed.build(freeze=bool(p.get("freeze", False)), tag=p.get("tag") or None,
                               injection_rate=float(p.get("injection_rate", 0.1)),
                               min_tier=minimum, progress=progress, workers=p.get("workers"),
                               temporal_cutoff=p.get('temporal_cutoff'), admission_policy=policy)
    print(f"built {out['path']}: {out['counts']['records']:,} records in {out['counts']['groups']:,} groups, "
          f"{out['counts']['views']:,} views; tiers {out['tier_mix']}")


def _stage_evidence_recovery(p):
    """Explicit source repair or trusted verification, with durable checkpoints."""
    if p.get('qualification_root_exclusive'):
        from app.backend.pipeline import npm_qualification_workflow, oslock
        lock = oslock.FileLock(npm_qualification_workflow.lock_path(p.get('build_root')), note=False)
        while True:
            try:
                lock.acquire(wait=0)
                break
            except oslock.Busy:
                progress(state='waiting', label='Waiting for the automatic evidence source lock', retry_after_seconds=30)
                time.sleep(30)
        try:
            return _stage_evidence_recovery({**p, 'qualification_root_exclusive': False})
        finally:
            lock.release()
    from app.backend.pipeline import evidence_recovery
    history_cancelled = None
    if p.get('recovery_workflow_id'):
        from contextlib import contextmanager
        from app.backend.pipeline import npm_qualification_workflow as workflow
        def history_cancelled():
            record = workflow.get(p['recovery_workflow_id'], root=p.get('build_root'))
            return record is None or record.get('cancel_requested') is True
        @contextmanager
        def history_guard():
            with workflow._connect(p.get('build_root')) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT cancel_requested FROM workflows WHERE id=?', (p['recovery_workflow_id'],)).fetchone()
                if row is None or row['cancel_requested']:
                    raise InterruptedError('The evidence workflow was cancelled before this item was saved.')
                yield
        history_cancelled.workflow_id = p['recovery_workflow_id']
        history_cancelled.publication_guard = history_guard
    source_capture = None
    if p.get('include_collected_candidates'):
        from app.backend.pipeline import frozen_evidence_build
        source_capture = frozen_evidence_build.capture(p['tag'], root=p.get('build_root'))
    while True:
        result = evidence_recovery.execute(p['tag'], action=p.get('action', 'repair'),
                                          root=p.get('build_root'), attempt_id=p.get('attempt_id'), progress=progress,
                                          source_capture=source_capture, record_ids=p.get('record_ids'),
                                          expected_sources=p.get('expected_sources'), cancelled=history_cancelled)
        print(json.dumps(result), flush=True)
        if result.get('state') != 'waiting':
            return
        # Waiting is a durable capability state, not a failed security verdict.
        # The child remains cancellable by the ordinary job process controls.
        progress(**{**result, 'label': 'Waiting for verification runtime; automatic retry in 30 seconds',
                    'retry_after_seconds': 30})
        time.sleep(30)


def _stage_qualify_npm_dataset(p):
    """Complete evidence waves and publication; full model training never starts."""
    from app.backend.pipeline import npm_qualification_workflow as workflow, finetune_qualified_run
    services = workflow.Services()
    root = pathlib.Path(p.get('build_root') or workflow._root()).resolve()
    def prepare_run(tag, *, root, work_key, check):
        processed = pathlib.Path(root).resolve()
        if processed.name != 'processed' or processed.parent.name != 'data':
            raise ValueError('Qualified run preparation requires the managed workspace data/processed folder.')
        return finetune_qualified_run.prepare(tag, root=processed.parent.parent, work_key=work_key, check=check)
    services.prepare_run = prepare_run
    while True:
        result = workflow.execute(p['workflow_id'], root=root, progress=progress, services=services)
        print(json.dumps(result), flush=True)
        state = result.get('state')
        if state == 'failed':
            raise RuntimeError(result.get('reason') or 'Automatic evidence workflow failed; completed work is retained.')
        if state != 'waiting':
            return
        progress(state='waiting', label=result.get('reason') or 'Waiting for evidence capability',
                 workflow_id=p['workflow_id'], next_retry_at=result.get('next_retry_at'))
        # Only the managed root has automatic scheduler reconciliation. Release
        # its waiting worker and analysis heaps; preserve custom-root ownership.
        if root == workflow._root().resolve():
            return
        due = result.get('next_retry_at') or 0
        delay = max(1, min(30, due-time.time())) if type(due) in (float, int) and due > time.time() else 30
        time.sleep(delay)


def _stage_campaign_run(p):
    """One campaign of the new system: the stages of each of its ecosystems, pausable at safe points (pipeline/campaigns/runner.py). Everything but the id is read from the campaign's
    record, so resuming is starting this stage again."""
    from app.backend.pipeline import campaign_ctx
    from app.backend.pipeline.campaigns import runner
    cid = str(p.get("campaign_id") or "")
    if not cid:
        print("campaign_run needs a campaign id.")
        raise SystemExit(1)
    os.environ[campaign_ctx.ENV] = cid                 # what the stores stamp on the commits this campaign queues and mines
    out = runner.run(cid, progress=progress)
    print(f"campaign {cid}: {out['state']}")


def _stage_export_training_bundle(p):
    """Resolve and pin a fine-tune run, export its bundle and notebook into training/runs/<run>/ (finetune_prep.prepare)."""
    from app.backend.pipeline import finetune_prep as fp, versions
    from app.backend.pipeline.oslock import FileLock
    run = fp.check_run_name(p.get("run") or "pilot")
    with FileLock(pathlib.Path(fp.RUNS) / '.catalog' / f'export-{run}.lock', note=False):
        # Recheck under the writer fence; another queued request may have
        # completed between admission and this process starting.
        fp.export_data_preflight(p)
        tag = p.get("tag") or "preview"
        info = fp.prepare(dataset_dir=pathlib.Path(versions.PROC) / f"dataset_{tag}", run_dir=fp.RUNS / run, stage=int(p.get("stage", 1)),
                          purpose=p.get("purpose") or "train", session_minutes=float(p["session_minutes"]) if p.get("session_minutes") else None,
                          loss_mode=p.get("loss_mode") or None, eval_views=int(p["eval_views"]) if p.get("eval_views") else None,
                          steps=int(p["steps"]) if p.get("steps") else None, eval_max=int(p["eval_max"]) if p.get("eval_max") else None,
                          overrides=p.get("overrides") or None, profile=p.get("profile") or None,
                          mode=p.get('mode', 'full'), total_budget_minutes=p.get('total_budget_minutes', 30),
                          revision=p.get("revision") or None, tokenizer_revision=p.get("tokenizer_revision") or None)
    print(f"prepared {run}: dataset {tag} ({info['dataset']['status']}), base {info['model_revision'][:12]}, bundle "
          f"{info['bundle']['bytes']:,} bytes sha256 {info['bundle']['sha256'][:12]}; warnings {[w['code'] for w in info['bundle']['warnings']]}")


def _stage_finetune_validate(p):
    """The CPU check of a prepared run's bundle: a fresh process, a tiny random model, a few steps (finetune_prep.validate)."""
    from app.backend.pipeline import finetune_prep as fp
    run = fp.check_run_name(p.get("run") or "pilot")
    report = fp.validate(run_dir=fp.RUNS / run, steps=int(p.get("steps", 2)), stage=int(p.get("stage", 2)),
                         eval_max=int(p["eval_max"]) if p.get("eval_max") else 16)
    print(f"cpu check of {run}: {'passed' if report['passed'] else 'FAILED'} in {report['seconds']} s")
    if not report["passed"]:
        print(report["output_tail"])
        raise RuntimeError("the CPU check failed")


def _stage_export_full_model(p):
    from app.backend.pipeline import finetune_distribution, finetune_prep
    name = finetune_prep.check_run_name(p.get('run') or '')
    result = finetune_distribution.export(finetune_prep.RUNS / name, p.get('import_id'), format=p.get('format', 'huggingface'))
    print(json.dumps(result))


def _stage_import_trial_outputs(p):
    from app.backend.pipeline import trial_import, finetune_prep as fp
    run = fp.check_run_name(p.get('run') or 'pilot')
    if not p.get('path'):
        raise ValueError('path: the trial_outputs.zip to import')
    record = trial_import.import_outputs(p['path'], fp.RUNS / run)
    print(f"Imported experimental trial {record['import_id']}: {record['state']}. This does not count as full training or security evaluation.")


def _stage_import_finetune_outputs(p):
    """Check an outputs.zip from a GPU run against the prepared run and keep it under training/runs/<run>/imports/ (finetune_import)."""
    from app.backend.pipeline import finetune_import as fi, finetune_prep as fp
    run = fp.check_run_name(p.get("run") or "pilot")
    if not p.get("path"):
        raise ValueError("path: the outputs.zip to import")
    try:
        rec = fi.import_outputs(p["path"], fp.RUNS / run)
    except fi.ImportRejected as e:
        for f in e.failures:
            print(f"REJECTED {f['check']}: {f['detail']}")
        raise
    print(f"imported {rec['import_id']} into {run}: type {rec['type']}, adapter {rec['adapter']['parameters']:,} parameters"
          + (" (already imported)" if rec["already_imported"] else ""))
    try:                                                  # the adapter can be served as soon as it is a GGUF adapter: when the model it was trained on is installed that happens here, by itself
        from app.backend.pipeline import gguf_convert, model_files
        for line in gguf_convert.adapter_words(gguf_convert.convert_pending_adapters(str(model_files.models_dir()), runs_root=fp.RUNS, log=lambda text: print(text, flush=True))):
            print(line)
    except Exception as e:  # ignored: the import is done and kept whatever happens here; the adapter can be converted later and the line says why it was not now
        print(f"Adapter    was not converted now: {type(e).__name__}: {str(e)[:200]}")


def _stage_collection_report(p):
    """notes/collection_report.md: measured aggregates per ecosystem and the campaign state (collection_report.write)."""
    from app.backend.pipeline import collection_report
    out = collection_report.write(p.get("path") or "notes/collection_report.md")
    print(f"collection report written: {out['path']}")


def _stage_import_finetune_eval(p):
    """Check an eval_outputs.zip against the prepared run and the bundle's dataset, score it and keep it as an evaluation run (finetune_eval_import)."""
    from app.backend.pipeline import finetune_eval_import as fe, finetune_prep as fp
    run = fp.check_run_name(p.get("run") or "pilot")
    if not p.get("path"):
        raise ValueError("path: the eval_outputs.zip to import")
    try:
        rec = fe.import_eval(p["path"], fp.RUNS / run,
            **({'evaluation_dataset_dir': p['evaluation_dataset_dir']} if p.get('evaluation_dataset_dir') else {}))
    except fe.EvalRejected as e:
        for f in e.failures:
            print(f"REJECTED {f['check']}: {f['detail']}")
        raise
    print(f"imported evaluation {rec['import_id']} into {run}: {rec['baseline']} ({rec['arm']}), {rec['attempts']:,} attempts, run {rec['tag']}"
          + ("" if rec["counts_as_evaluation"] else " (kept as evidence: a software or partial evaluation does not count)")
          + (" (already imported)" if rec["already_imported"] else ""))


def _stage_scan_project_deps(p):
    """Scan the dependencies of a project: read its manifests, match each exact version against the advisories on this computer, and with ``outdated`` ask the registries for newer
    releases. The project's files are only read; the report is kept with the project (pipeline/scan/project_deps.py)."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import project_deps
    project = projects.get(str(p.get("project") or ""))
    report = project_deps.run(project, check_outdated=bool(p.get("outdated")), progress=progress)
    s = report["summary"]
    print(f"{project['name']}: {s['dependencies']} dependencies, {s['vulnerable']} vulnerable, {s['no_finding']} with no finding by advisory matching, "
          f"{s['inconclusive']} inconclusive, {s['env_error']} not checked; {s['manifests_not_read']} manifests not read")
    for eco, e in report["ecosystems"].items():
        if not e.get("checked"):
            print(f"{eco}: {e['reason']}")


def _stage_scan_project_code(p):
    """Scan the source code of a project with the static rules: the files are only read; every match is a candidate with its evidence (pipeline/scan/project_code.py)."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import project_code
    project = projects.get(str(p.get("project") or ""))
    report = project_code.run(project, progress=progress)
    s = report["summary"]
    sev = s["severity"]
    print(f"{project['name']}: {s['files']} source files read, {s['findings']} candidates ({sev['critical']} critical, {sev['high']} high, {sev['medium']} medium, {sev['low']} low); "
          f"{s['skipped']} files skipped")
    for lang in s["secrets_only_languages"]:
        print(f"{lang}: no vulnerability rules yet, checked for hard-coded secrets only")
    if s["cut_short"]:
        print("The scan stopped at its limits; more candidates may exist.")


def _stage_scan_project_codebase(p):
    """The full Scan Codebase: the static rules, then the approved toolkit, the project's own tests and the loopback probes inside a closed container on a copy of the project. The host folder is
    only read; every tool run is in the project's audit log with its evidence (pipeline/scan/deep_scan.py)."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import deep_scan, sandbox_run
    project = projects.get(str(p.get("project") or ""))
    try:
        report = deep_scan.run(project, options={"start_command": p.get("start_command"), "port": p.get("port"),
                              'model_review': p.get('model_review', False), 'model_max_files': p.get('model_max_files', 32)}, progress=progress)
    except (sandbox_run.ToolkitMissing, sandbox_run.CopyRefused) as e:
        raise SystemExit(str(e))
    s = report["summary"]
    print(f"{project['name']}: {s['findings']} findings ({s['by_state']['vulnerable']} vulnerable, {s['by_state']['inconclusive']} inconclusive); "
          f"checks that ran: {', '.join(s['checks_ran']) or 'none'}")
    for name in s["checks_failed"]:
        print(f"{name} did not run to its end")
    for name in s["checks_skipped"]:
        print(f"{name} was not attempted")
    print(s["words"])


def _stage_plan_project_update(p):
    """Plan an update of a project's dependencies from its newest dependency scan, make a candidate on a copy of the manifests and predict the scan afterwards. Nothing in the project
    changes; applying the candidate is a separate step the person confirms (pipeline/scan/update_plan.py)."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import update_plan
    project = projects.get(str(p.get("project") or ""))
    try:
        u = update_plan.make(project, str(p.get("mode") or "security"), progress=progress)
    except projects.ProjectError as e:
        raise SystemExit(str(e))
    pre = u["predicted"]
    print(f"update {u['id']}: {len(u['plan']['changes'])} dependencies in {len(u['files'])} files ({u['mode_words'].lower()}); a candidate, nothing in {project['name']} changed")
    print(f"predicted by advisory matching: {len(pre['advisories_fixed'])} advisories fixed, {len(pre['advisories_remaining'])} still affecting, {len(pre['advisories_introduced'])} newly affecting")
    for lock in u["locks_to_regenerate"]:
        print(f"{lock} is not edited: regenerate it with your package manager")


def _stage_convert_gguf(p):
    """Convert an installed model to GGUF and quantise it with llama.cpp's own scripts, offline, into data/models/converted (pipeline/gguf_convert.py). The model is only read."""
    from app.backend.pipeline import gguf_convert, model_files
    entry = gguf_convert.find(str(p.get("model_id") or ""), str(p.get("source") or ""), str(model_files.models_dir()))
    if entry is None:
        raise SystemExit("That model is not installed on this computer, or it is a GGUF file already.")
    try:
        from app.backend.config import Settings
        from app.backend.pipeline import derived_cache
        record = gguf_convert.convert(entry, str(p.get("outtype") or "f16"), p.get("quant") or None, keep_intermediate=bool(p.get("keep_intermediate")), log=lambda line: print(line, flush=True),
                                      room=derived_cache.for_job(Settings.load().derived_cache_gb))
    except gguf_convert.ConvertError as e:
        raise SystemExit(str(e))
    f = record["files"][-1] if not record["quant"] else next(x for x in record["files"] if x["name"].endswith(f"{record['quant']}.gguf"))
    print(f"{f['name']}: {f['bytes'] / 1024 ** 3:.2f} GB, sha256 {f['sha256'][:16]}..., architecture {record['architecture']}, scripts {record['scripts_tag']}")


def _stage_convert_adapter(p):
    """Convert the LoRA adapter of a fine-tuning run to a GGUF adapter the local server can load on top of its base model (pipeline/gguf_convert.py). The run's files are only read."""
    from app.backend.pipeline import gguf_convert, model_files
    run = next((a for a in gguf_convert.run_adapters() if a["run"] == str(p.get("run") or "")), None)
    if run is None:
        raise SystemExit("That fine-tuning run has no adapter here.")
    base = gguf_convert.base_entry(run["base"], str(model_files.models_dir()))
    if base is None:
        raise SystemExit(f"The model this adapter was trained on ({run['base'] or 'not named'}) is not installed as safetensors.")
    try:
        record = gguf_convert.convert_adapter(run["folder"], base, str(p.get("outtype") or "f16"), name=run["name"], log=lambda line: print(line, flush=True))
    except gguf_convert.ConvertError as e:
        raise SystemExit(str(e))
    f = record["files"][0]
    print(f"{f['name']}: {f['bytes'] / 1024 ** 2:.1f} MB, sha256 {f['sha256'][:16]}..., made from the run's weights {record['adapter']['sha256'][:16]}..., on {record['base']['id']}")


def _stage_import_external(p):
    """Turn an uploaded file of training examples into a dataset of its own (pipeline/external_data.py). A file with a problem imports nothing and the log lists the problems."""
    from app.backend.pipeline import external_data as ed
    path = ed.upload_path(str(p.get("upload") or ""))
    if path is None:
        raise SystemExit("That upload is not there any more. Choose the file again.")
    try:
        out = ed.import_file(path, str(p.get("name") or ""), original_name=ed.upload_name(str(p.get("upload"))), source=ed.upload_source(str(p.get("upload"))), progress=lambda done: progress(state="reading", label=f"Checked {done:,} rows", processed=done))
    except ed.ExternalDataError as e:
        raise SystemExit(str(e))
    ed.discard_upload(str(p.get("upload")))
    print(f"{out['tag']}: {out['views']:,} examples from {out['rows']:,} rows of {out['original_file']} ({out['size_bytes'] / 1048576:.1f} MB, sha256 {out['sha256'][:16]}...), imported by {out['imported_by']}")
    for w in out["warnings"]:
        print(f"Note: {w}")


def _stage_fetch_hub_dataset(p):
    """Fetch one named file of a Hugging Face dataset, at one commit, into an upload (pipeline/hub_datasets.py). The person asked for this file after seeing its size and licence; it
    then goes through the same checks as a file of their own."""
    from app.backend.pipeline import external_data as ed, hub_datasets as hd
    try:
        out = hd.fetch(str(p.get("repo") or ""), str(p.get("revision") or ""), str(p.get("path") or ""), str(p.get("upload") or ""),
                       progress=lambda done, total: progress(state="copying", label=f"Copied {done / 1048576:.0f} of {total / 1048576:.0f} MB", processed=done, total=total))
    except ed.ExternalDataError as e:
        raise SystemExit(str(e))
    print(f"{out['filename']}: {out['size_bytes'] / 1048576:.1f} MB from {p['repo']} at {str(p['revision'])[:12]} (sha256 {out['sha256'][:16]}...), kept as an upload to check")


def _stage_sandbox_build_image(p):
    """Build an image from a Dockerfile the person wrote, with the build log as the job's output; the steps have no network unless the person asked for it (sandbox_images.build)."""
    from app.backend import sandbox_images
    try:
        out = sandbox_images.build(str(p.get("dockerfile") or ""), str(p.get("tag") or ""), bool(p.get("network")), on_line=print)
    except sandbox_images.ImageError as e:
        raise SystemExit(str(e))
    print(f"built {out['tag']} ({out['id']}, {out['size_mb']} MB), labelled as this app's; network during the build: {'yes' if out['network'] else 'no'}")


def _stage_scan_deps(p):
    from app.backend.pipeline.scan import deps
    deps.scan(p.get("manifest", "requirements.txt"), p.get("output"))


def _stage_scan_codebase(p):
    from app.backend.pipeline.scan import codebase
    codebase.scan(p.get("project", "."), p.get("output"))


def _stage_patch(p):
    from app.backend.pipeline.patch import generate
    generate.run(p["cases"], tag=p.get("tag") or "patch_feasibility")


def _stage_trial_update(p):
    """Try a candidate update in the sandbox: fetch the packages as wheels on this computer (the only use of the network), install them offline in closed containers next to a copy of the
    project, and compare the project's tests and the toolkit before and after (pipeline/scan/update_trial.py)."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.scan import sandbox_run, update_trial
    project = projects.get(str(p.get("project") or ""))
    try:
        trial = update_trial.run(project, str(p.get("update") or ""), progress=progress)
    except (sandbox_run.ToolkitMissing, sandbox_run.CopyRefused, projects.ProjectError) as e:
        raise SystemExit(str(e))
    print(trial["words"])
    for label in ("before", "after"):
        got = trial["prefetch"].get(label)
        if got:
            print(f"{label}: {len(got['files'])} wheels, {got['bytes'] // 1024:,} KB" + ("" if got["ok"] else f" - {got['note']}"))


def _stage_respond_project(p):
    """Collect the findings of a project (its dependencies, its code, or both), classify them, and respond by mode (log, email, or a patch candidate). Candidates are tested in the closed
    container and are never applied to the project's folders (pipeline/agent/orchestrator.py)."""
    from app.backend.pipeline import projects
    from app.backend.pipeline.agent import orchestrator
    from app.backend.pipeline.scan import sandbox_run
    project = projects.get(str(p.get("project") or ""))
    try:
        report = orchestrator.run(project, scope=str(p.get("scope") or "both"), mode=str(p.get("mode") or "log"), depth=str(p.get("depth") or "quick"), assist=bool(p.get("assist")),
                                  allow_remote=bool(p.get("allow_remote")), test=p.get("test") is not False, options={"start_command": p.get("start_command"), "port": p.get("port")}, progress=progress)
    except (sandbox_run.ToolkitMissing, sandbox_run.CopyRefused, ValueError) as e:
        raise SystemExit(str(e))
    s = report["summary"]
    c = s["by_class"]
    print(f"{project['name']}: {s['findings']} findings ({c['Critical']} Critical, {c['Warning']} Warning, {c['Recommendation']} Recommendation, {c['Unknown']} Unknown); mode {report['mode']}")
    for action, n in sorted(s["by_action"].items()):
        print(f"  {action}: {n}")
    for note in report["notes"]:
        print(f"note: {note}")
    if report.get("email"):
        print("email: " + ("sent" if report["email"]["sent"] else report["email"].get("reason", "not sent")))


def _stage_propose_project_patch(p):
    from app.backend import paths
    from app.backend.pipeline.project_ide import staging
    from app.backend.pipeline.project_ide.store import Store

    store = Store(paths.INTERIM / "projects_agent_ide" / "projects.sqlite3")
    try:
        result = staging.propose_candidate(store, str(p.get("chat_id") or ""), str(p.get("finding_id") or ""),
                                           run_checks=p.get("run_checks") is True)
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from error
    if result.get("stopped"):
        raise SystemExit(result["stopped"])
    suffix = "isolated checks passed" if result.get("staging_receipt") else result["tests"]["words"]
    print(f"Patch candidate {result['candidate_id']}: {suffix}.")


def _stage_repair_insufficient_context(p):
    from app.backend.pipeline import insufficient_context_repair
    result = insufficient_context_repair.execute(p['tag'], p['ids'], root=p.get('build_root'), progress=progress, attempt_id=p.get('attempt_id'))
    print(json.dumps(result))


def _stage_delete_finetune_runs(p):
    from app.backend.pipeline import finetune_prep as fp, finetune_deletion
    result = finetune_deletion.execute(fp.RUNS, p['preview_token'], progress=progress)
    progress(state='done' if not result['failed'] else 'partial_failure', **result)
    print(json.dumps(result))
    if result['failed']:
        raise RuntimeError(f"{result['failed']} run deletions failed; inspect the per-run outcomes and retry")


STAGES = {
    'qualify_npm_dataset': _stage_qualify_npm_dataset,
    'repair_insufficient_context': _stage_repair_insufficient_context,
    'evidence_recovery': _stage_evidence_recovery,
    'delete_finetune_runs': _stage_delete_finetune_runs,
    "ingest": _stage_ingest,
    "osv_download": _stage_osv_download,
    "osv_install": _stage_osv_install,
    "verify_case": _stage_verify_case,
    "hf_download": _stage_hf_download,
    "install_model": _stage_install_model,
    "install_ollama_model": _stage_install_ollama_model,
    "build_reviewed_dataset": _stage_build_reviewed_dataset,
    "campaign_run": _stage_campaign_run,
    "collection_report": _stage_collection_report,
    "export_training_bundle": _stage_export_training_bundle,
    "finetune_validate": _stage_finetune_validate,
    "import_finetune_outputs": _stage_import_finetune_outputs,
    "import_trial_outputs": _stage_import_trial_outputs,
    'export_full_model': _stage_export_full_model,
    "import_finetune_eval": _stage_import_finetune_eval,
    "extract_fixes": _stage_extract_fixes,
    "mine_code": _stage_mine_code,
    "dep_versions": _stage_dep_versions,
    "verify_fixes": _stage_verify_fixes,
    "resplit_pairs": _stage_resplit_pairs,
    "build_code_dataset": _stage_build_code_dataset,
    "enrich_pairs": _stage_enrich_pairs,
    'context_review_local': _stage_context_review_local,
    'context_review_export': _stage_context_review_export,
    'review_exchange_export': _stage_review_exchange_export,
    'review_exchange_validate': _stage_review_exchange_validate,
    'review_exchange_import': _stage_review_exchange_import,
    "dataset": _stage_dataset,
    "rag_index": _stage_rag_index,
    "rag_recall": _stage_rag_recall,
    "rag_index_project": _stage_rag_index_project,
    "eval_detection": _stage_eval_detection,
    "compare": _stage_compare,
    "db_backup": _stage_db_backup,
    "gpu_smoke": _stage_gpu_smoke,
    "gpu_eval": _stage_gpu_eval,
    "train_local": _stage_train_local,
    "scan_project_deps": _stage_scan_project_deps,
    "scan_project_code": _stage_scan_project_code,
    "scan_project_codebase": _stage_scan_project_codebase,
    "plan_project_update": _stage_plan_project_update,
    "convert_gguf": _stage_convert_gguf,
    "convert_adapter": _stage_convert_adapter,
    "import_external": _stage_import_external,
    "fetch_hub_dataset": _stage_fetch_hub_dataset,
    "sandbox_build_image": _stage_sandbox_build_image,
    "classify": _stage_classify,
    "detect": _stage_detect,
    "eval": _stage_eval,
    "patch_eval": _stage_patch_eval,
    "eval_patch": _stage_eval_patch,
    "recover_fixes": _stage_recover_fixes,
    "phase_audit": _stage_phase_audit,
    "scan_deps": _stage_scan_deps,
    "scan_codebase": _stage_scan_codebase,
    "patch": _stage_patch,
    "respond_project": _stage_respond_project,
    "propose_project_patch": _stage_propose_project_patch,
    "trial_update": _stage_trial_update,
}


def _default_dataset() -> str:
    from app.backend.pipeline import versions
    return str(versions.resolve("dataset", ".parquet", "dataset_preview.parquet",
                                pathlib.Path("data/processed")))


def _entry(raw: str) -> str:
    """One log entry from what the process printed up to a line end. The line end (``\\n`` or ``\\r\\n``) is not part of it; a lone carriage return is kept, because it is how a
    program redraws a line (a progress bar): it tells the page that the next entry is drawn over this one. Text the stream ended with, without any line end, is an entry too."""
    if raw.endswith("\r\n"):
        return raw[:-2]
    return raw[:-1] if raw.endswith("\n") else raw


def _take_progress(job: Job, report: str) -> None:
    """A progress report a stage printed (``progress()``): the JSON after the marker. One that cannot be read is dropped."""
    try:
        state = json.loads(report)
        if isinstance(state, dict) and state.get("device_used") in ("cpu", "gpu"):
            job.compute = {**job.compute, "used": state["device_used"]}
        job.progress = state
    except ValueError:
        pass                          # ignored: a report that is not JSON is dropped, as it always was; the job goes on
    if time.time() - job.saved_at >= RECORD_EVERY:
        save_log(job)                 # a running job's record follows its progress, so a crash leaves a true last state


def _run(job: Job):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
           "TQDM_MININTERVAL": "10", "ZTSA_OLLAMA_MANAGED_CHILD": "1",
           "PYTHONPATH": os.pathsep.join(filter(None, [str(_ROOT), os.environ.get("PYTHONPATH")]))}
    env.pop("ZTSA_CAMPAIGN_RAM_CAP_BYTES",None)
    try:
        if job.stage=="campaign_run":
            from app.backend.pipeline.acceleration.memory_profile import limit_threads
            limit_threads(env)
            from app.backend.pipeline import derived
            from app.backend.pipeline.campaigns import repo
            with derived.connect() as conn:
                campaign=repo.get(conn,(job.params or {}).get("campaign_id")) or {}
            if ((campaign.get("plan") or {}).get("mining") or {}).get("version")==4:
                from app.backend.pipeline.acceleration.memory_profile import campaign_ram_cap
                env["ZTSA_CAMPAIGN_RAM_CAP_BYTES"]=str(campaign_ram_cap())        # 3 GB unless ZTSA_CAMPAIGN_MEMORY_MB asks for the large tier
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "app.backend.jobworker", job.stage],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=os.getcwd(), env=env,
            creationflags=(_NO_WINDOW | _BELOW_NORMAL) if os.name == "nt" else 0,
            start_new_session=os.name != "nt")
        job.proc = proc
        proc.stdin.write(json.dumps(job.params).encode("utf-8"))
        proc.stdin.close()
        # newline="" turns no line end into another: "\n", "\r" and "\r\n" each end an entry (a redraw is delivered at once) and a lone "\r" stays in the entry, as _entry says
        for raw in io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline=""):
            entry = _entry(raw)
            mark = entry.find(PROGRESS_MARK)             # not only at the start: a report can follow a partial line (a bar that has not ended its line)
            if mark >= 0:
                if entry[:mark].strip():
                    job.logs.append(entry[:mark])
                _take_progress(job, entry[mark + len(PROGRESS_MARK):])
                continue
            job.logs.append(entry)
        rc = proc.wait()
        if job.cancelled:
            job.status = "cancelled"
            job.logs.append(job.error or "cancelled by user")
        elif rc == 0:
            job.status = "done"
        else:
            job.status = "error"
            recent = termtext.visible(job.logs.tail(RECORD_TAIL))          # what a terminal would show: no colour codes, a redrawn bar once
            tb = next((i for i in range(len(recent) - 1, -1, -1) if recent[i].startswith("Traceback")), None)
            job.error = "\n".join(recent[tb:] if tb is not None else recent[-20:])
    except Exception as e:                       # could not start the worker at all
        job.status = "error"
        job.error = f"could not start the job process: {e}"
        job.logs.append(job.error)
    finally:
        job.finished = datetime.datetime.utcnow().isoformat() + "Z"
        job.logs.close()
        _record_compute(job)
        try:
            from app.backend import slowcache
            slowcache.job_finished(job.stage)          # the cached views a job may have changed are recomputed on the next ask
        except Exception as e:
            print(f"could not invalidate the cached views after {job.stage}: {type(e).__name__}: {e}")
        from app.backend.pipeline import finetune_deletion, finetune_prep as fp, finetune_catalog
        if job.stage in finetune_deletion.RUN_STAGES:
            name = (job.params or {}).get('run') or 'pilot'
            finetune_catalog.invalidate(fp.RUNS, name)
            finetune_deletion.release(fp.RUNS, job.id)
        elif job.stage == 'delete_finetune_runs':
            finetune_deletion.unreserve(fp.RUNS, job.params['preview_token'])
        save_log(job)
        _letter_about(job)


def _record_compute(job: Job) -> None:
    """For a job that has a choice of device: what was asked for, what ran it and how long it took, measured from the job's own start and end. Never a prediction."""
    from app.backend.pipeline import compute_choice
    if not compute_choice.is_heavy(job.stage):
        return
    try:
        began = datetime.datetime.fromisoformat(job.started.rstrip("Z"))
        ended = datetime.datetime.fromisoformat(job.finished.rstrip("Z"))
        seconds = round((ended - began).total_seconds(), 1)
    except ValueError:
        seconds = None
    job.compute = {"requested": (job.params or {}).get("device") or None, "used": job.compute.get("used"), "seconds": seconds}


def _letter_about(job: Job) -> None:
    """Send the letter about a finished job, if Settings ask for one, from a thread of its own: a mail server that is slow must not hold the job's record or the next job."""
    from app.backend.pipeline import notify
    record = job.public()
    if not notify.should_send(record):
        return
    tail = termtext.visible(job.logs.tail(8))
    threading.Thread(target=notify.email_job, args=(record, tail), daemon=True, name="job-letter").start()


def save_log(job: Job) -> None:
    """The job's record (at its start, while it runs and when it ends), written atomically so a power cut leaves the previous file or none."""
    from app.backend.pipeline import fsio
    job.saved_at = time.time()
    try:
        keep = RECORD_TAIL if job.status != "running" else 200          # a running record is rewritten often, so it carries a short tail; <id>.log has all
        fsio.write_json(LOG_DIR / f"{job.id}.json", {**job.public(), "logs": job.logs.tail(keep), "log_start": max(0, len(job.logs) - keep)}, indent=2)
        if job.stage == "build_reviewed_dataset" and job.params.get("build_id"):
            from app.backend.pipeline import build_catalog
            build_catalog.sync_job(job.public())
    except OSError as e:
        print(f"could not save the log of job {job.id}: {e}")


def _kill(proc) -> None:
    """Terminate a job process and its children (git, etc.)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except Exception:  # ignored: if the process-tree kill fails, terminate the process itself (next line)
        proc.terminate()


CAMPAIGN_PAUSE_WAIT = 12.0       # seconds the campaigns get to pause themselves when the app closes


def stop_all(reason: str = "Cancelled because the app was closed.", wait: float = 8.0, campaign_wait: float | None = None) -> list[Job]:
    """Cancel every running job for good (the app is closing): kill its process tree, say why on the record, and write the record. A mining run is
    marked stopped, because a deliberate stop is not an interruption. The campaigns of the new system are first asked to pause and get a few seconds to do it (what they
    were doing is saved); the ones still running then are killed and marked paused. Returns the jobs that were running."""
    victims = running()
    campaigns = [j for j in victims if j.stage == "campaign_run"]
    if campaigns:
        from app.backend.pipeline.campaigns import control
        for job in campaigns:
            try:
                control.pause_for_job(job.params)
            except Exception as e:                      # ignored: a campaign that cannot be asked is killed below
                print(f"could not ask campaign job {job.id} to pause: {type(e).__name__}: {e}")
        end = time.time() + (CAMPAIGN_PAUSE_WAIT if campaign_wait is None else campaign_wait)
        while time.time() < end and any(j.status == "running" for j in campaigns):
            time.sleep(0.1)
    for job in victims:
        if job.status != "running":                    # a campaign that paused itself in time
            continue
        job.cancelled = True
        job.error = reason
        _kill(job.proc)
        if job.stage == "mine_code":
            from app.backend.pipeline import mining_run
            mining_run.finish("stopped")
        if job.stage == "campaign_run":
            from app.backend.pipeline.campaigns import control
            control.after_kill(job.params, "the app was closed")
    end = time.time() + wait
    while time.time() < end and any(j.status == "running" for j in victims):
        time.sleep(0.05)
    for job in victims:
        if job.status == "running":                    # its thread did not finish in time (or there was none): close the record here
            job.status = "cancelled"
            job.finished = datetime.datetime.utcnow().isoformat() + "Z"
            job.logs.append(reason)
            job.logs.close()
            save_log(job)
    return victims


def cancel(job_id: str, *, hard: bool = False) -> bool:
    """Stop a job. A campaign of the new system is not killed: it is asked to pause, saves where it was and ends by itself (the watchdog kills it if it takes too long); ``hard`` kills it."""
    job = _JOBS.get(job_id)
    if not job or job.status != "running":
        return False
    if job.stage == "campaign_run" and not hard:
        from app.backend.pipeline.campaigns import control
        return control.pause_for_job(job.params)
    if job.stage == 'repair_insufficient_context' and job.params.get('attempt_id'):
        from app.backend.pipeline import insufficient_context_repair
        insufficient_context_repair.cancel_attempt(job.params['tag'], job.params['attempt_id'], root=job.params.get('build_root'))
    if job.stage == 'qualify_npm_dataset' and job.params.get('workflow_id'):
        from app.backend.pipeline import npm_qualification_workflow
        record = npm_qualification_workflow.get(job.params['workflow_id'], root=job.params.get('build_root'))
        if record and not record.get('cancel_requested'):
            npm_qualification_workflow.cancel(record['id'], root=job.params.get('build_root'))
    if job.stage == 'evidence_recovery' and job.params.get('attempt_id'):
        from app.backend.pipeline import evidence_recovery_history
        evidence_recovery_history.invalidate(job.params['attempt_id'], root=job.params.get('build_root'),
                                             state='cancelled', reason='The recovery job was cancelled.')
    job.cancelled = True
    _kill(job.proc)
    if job.stage == "mine_code":                  # a deliberate stop is not an interruption
        from app.backend.pipeline import mining_run
        mining_run.finish("stopped")
    if job.stage == "campaign_run":
        from app.backend.pipeline.campaigns import control
        control.after_kill(job.params)
    return True


@atexit.register
def _shutdown() -> None:
    for job in list(_JOBS.values()):
        if job.status == "running":
            job.cancelled = True
            _kill(job.proc)
        job.logs.close()


def start(stage: str, params: dict | None = None) -> Job:
    if stage not in STAGES:
        raise ValueError(f"unknown stage: {stage}")
    if stage in ('train_local', 'gpu_smoke'):
        raise ValueError('Desktop training is retired. Prepare a portable trial or full bundle, run its notebook in a remote runtime, then import its outputs.')
    if stage in ('rag_index', 'rag_recall') or (stage == 'classify' and (params or {}).get('rag')):
        raise ValueError('RAG training and evaluation workflows are retired. Retrieval is available only in agentic Projects.')
    if stage == 'export_training_bundle':
        from app.backend.pipeline import finetune_prep
        finetune_prep.export_data_preflight(params or {})
    from app.backend.pipeline import compute_choice
    compute_choice.validate(stage, (params or {}).get("device"))                  # a device the job has no path for is refused in one line, before anything starts
    from app.backend.pipeline.campaigns import guard
    why = guard.check(stage, params)                                              # a campaign working on an ecosystem keeps manual jobs off it
    if why:
        raise Blocked(why)
    limit = max_running()
    if len(running()) >= limit:
        raise TooManyJobs(f"{limit} jobs are already running; wait for one to finish or stop one first")
    job_id = uuid.uuid4().hex[:12]
    job = Job(id=job_id, stage=stage, params=params or {}, logs=JobLog(spill=LOG_DIR / f"{job_id}.log"),
              started=datetime.datetime.utcnow().isoformat() + "Z")
    from app.backend.pipeline import finetune_deletion, finetune_prep as fp
    try:
        if stage in finetune_deletion.RUN_STAGES:
            name = (params or {}).get('run') or 'pilot'
            finetune_deletion.admit(fp.RUNS, name, job.id)
        elif stage == 'delete_finetune_runs':
            finetune_deletion.reserve(fp.RUNS, (params or {}).get('preview_token'), job.id)
    except finetune_deletion.Refused as problem:
        raise Blocked(str(problem)) from problem
    register(job)
    save_log(job)                                   # the record exists from the first moment, so an app closed now leaves a trace
    job.thread = threading.Thread(target=_run, args=(job,), daemon=True)
    job.thread.start()
    return job


def forget(job_id: str) -> bool:
    """Remove a finished job's record for good: out of the registry and off the disk (its ``<id>.json`` and ``<id>.log``). For something that owns its job's whole lifetime (a campaign
    deleted along with it, for example), so the job does not keep appearing on its own, unlabelled, long after the thing it belonged to is gone. A running job is left alone (nothing is
    running it is a precondition this never checks for itself, so a caller that is not sure should check ``get(job_id).status`` first); an id nobody has is a no-op, not an error."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None and job.status == "running":
            return False
        _JOBS.pop(job_id, None)
    for suffix in (".json", ".log"):
        try:
            (LOG_DIR / f"{job_id}{suffix}").unlink()
        except OSError:
            pass                      # ignored: already gone, or held by something else for a moment; nothing more a delete can do about it
    return True


def register(job: Job) -> Job:
    """Put a job in the registry (``start`` does this; tools that fake a job for a measurement use it too)."""
    with _LOCK:
        _JOBS[job.id] = job
        finished = sorted((j for j in _JOBS.values() if j.status != "running"), key=lambda j: j.started)
        for old in finished[:max(0, len(finished) - MAX_FINISHED)]:      # a long session keeps the newest finished jobs only
            _JOBS.pop(old.id, None)
    return job


def load_persisted(log_dir: pathlib.Path | None = None) -> list[Job]:
    """Bring the records of earlier sessions back into the registry (the newest ``MAX_FINISHED``). A record that still says ``running`` belongs
    to a job whose app was closed or crashed: it becomes ``interrupted`` (written back), never ``running``, because nothing is running it."""
    from app.backend.pipeline import fsio
    folder = pathlib.Path(log_dir or LOG_DIR)
    loaded: list[Job] = []
    if not folder.is_dir():
        return loaded
    for path in sorted(folder.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True)[:MAX_FINISHED * 3]:
        if not _RECORD_NAME.match(path.name):
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            job = Job(id=rec["id"], stage=rec["stage"], params=rec.get("params") or {}, status=rec.get("status") or "error", logs=list(rec.get("logs") or []),
                      started=rec.get("started") or "", finished=rec.get("finished") or "", error=rec.get("error") or "", progress=rec.get("progress") or {},
                      compute=rec.get("compute") if isinstance(rec.get("compute"), dict) else {})
        except (OSError, ValueError, KeyError, TypeError):
            continue                                  # an unreadable or foreign file is not a job record
        if job.id in _JOBS:
            continue
        if job.status == "running":
            job.status = "interrupted"
            job.finished = datetime.datetime.fromtimestamp(path.stat().st_mtime, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            job.error = job.error or "The app was closed while this job was running."
            job.logs.append("interrupted: the app was closed while this job was running")
            try:
                fsio.write_json(path, {**job.public(), "logs": job.logs.tail(RECORD_TAIL), "log_start": rec.get("log_start", 0)}, indent=2)
            except OSError as e:
                print(f"could not mark job {job.id} interrupted: {e}")
        job.logs.close()
        if job.stage == "build_reviewed_dataset" and job.params.get("build_id"):
            from app.backend.pipeline import build_catalog
            build_catalog.sync_job(job.public())
        loaded.append(job)
    for job in sorted(loaded, key=lambda j: j.started):
        register(job)
    from app.backend.pipeline import build_catalog
    build_catalog.recover_requests()
    for root in {j.params.get('build_root') for j in loaded if j.stage == 'build_reviewed_dataset' and j.params.get('build_root')}:
        build_catalog.recover_requests(root=root)
    return loaded


def get(job_id: str) -> Job | None:
    return _JOBS.get(job_id)


def running(stage: str | None = None) -> list[Job]:
    """The jobs that are running now, of one stage when ``stage`` is given."""
    return [j for j in list(_JOBS.values()) if j.status == "running" and (stage is None or j.stage == stage)]


def running_stages() -> list[str]:
    """The stage of every running job (one entry per job)."""
    return [j.stage for j in running()]


def is_running(stage: str) -> bool:
    return bool(running(stage))


def listing() -> list[dict]:
    return [j.public() for j in sorted(_JOBS.values(), key=lambda x: x.started, reverse=True)]
