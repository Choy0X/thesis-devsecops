"""FastAPI backend for the desktop app: the wiring only.

The routes live in domain routers (one module per area: settings, jobs, results, the dataset records, the code pairs, the sandbox, and the
review queue, the campaign, the builds and the fine-tune runs). This module creates the app, gives every error the same body, puts the
local-only access check and the slow-route cache in front, includes the routers, and serves the built React frontend when it is present
so the single Uvicorn process is the whole app.
"""
from __future__ import annotations

import contextlib
import os
import pathlib

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.backend.pipeline.db.operations import ConnectionFailure

from app.backend import errors, jobs, lifecycle, security, slowcache
from app.backend import qualification_api
from app.backend.pipeline import derived
from app.backend.pipeline.db import mongo

@contextlib.asynccontextmanager
async def _lifespan(_app):
    """At start: the job records of earlier sessions come back, and a job that was running when the app closed shows as interrupted."""
    try:
        jobs.load_persisted()
    except Exception as e:                       # a damaged records folder must not stop the app from starting
        print(f"could not load the job records: {type(e).__name__}: {e}")
    try:
        lifecycle.mark_started()
        from app.backend import sandbox_runtime
        sandbox_runtime.bootstrap()
    except OSError as e:
        print(f"could not write the run marker: {e}")
    try:
        from app.backend import settings_migrations
        for done in settings_migrations.run():             # a change a newer version makes to the saved settings, once; the Settings page tells the person
            print(f"settings migration {done['id']}: {len(done['changes'])} change(s)")
    except Exception as e:                       # the settings are left as they were
        print(f"could not migrate the settings: {type(e).__name__}: {e}")
    try:
        if not os.environ.get("PYTEST_CURRENT_TEST"):          # a test never acts on the real list of leftovers
            from app.backend.pipeline import hard_reset
            left = hard_reset.retry_pending()                  # what the last hard reset could not delete (a file in use) is tried again now
            if any(left.values()):
                print(f"hard reset leftovers: {left['removed']} removed, {left['left']} still in use, {left['refused']} not covered")
    except Exception as e:                       # the leftovers stay on their list for the next start
        print(f"could not retry the hard reset leftovers: {type(e).__name__}: {e}")
    try:
        from app.backend.pipeline.campaigns import control
        with derived.connect() as conn:                      # a campaign whose process did not survive the last session is shown interrupted, ready to resume
            for cid in control.reconcile(conn):
                print(f"campaign {cid} was running when the app closed; it is marked interrupted")
        stop_watchdog = control.start_watchdog()
    except Exception as e:                                   # the campaigns are looked at again when the page is opened
        print(f"could not check the campaigns: {type(e).__name__}: {e}")
        stop_watchdog = lambda: None
    try:
        from app.backend.pipeline.scan import agent_sandbox
        stop_agent_watchdog = agent_sandbox.start_idle_watchdog()
    except Exception as e:                                   # an idle sandbox is only stopped a little later, at the next start
        print(f"could not start the agent sandbox watchdog: {type(e).__name__}: {e}")
        stop_agent_watchdog = lambda: None
    from app.backend.pipeline.ollama import server
    server.start_in_background()
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        from app.backend.pipeline.agentic.exec import toolchain
        try:
            toolchain.start_defaults()
        except (OSError, ValueError, toolchain.ToolchainError) as exc:
            print(f"could not prepare command tools: {exc}")
    try:
        from app.backend.pipeline import context_automation
        context_automation.start()              # resumes only an explicitly enabled preference; disabled by default
    except Exception as e:
        print(f"could not start explanation automation: {type(e).__name__}: {e}")
    stop_qualification = lambda: None
    try:
        if not os.environ.get('PYTEST_CURRENT_TEST'):
            from app.backend.pipeline import qualification_scheduler
            stop_qualification = qualification_scheduler.start()
    except Exception as e:
        print(f"could not resume evidence workflows: {type(e).__name__}: {e}")
    try:
        yield
    finally:
        stop_qualification()
        stop_watchdog()
        stop_agent_watchdog()
        from app.backend import project_ide_api
        project_ide_api.close()
        if not lifecycle.status()['done']:
            import asyncio
            await asyncio.to_thread(lifecycle.shutdown)


app = FastAPI(title="AI Vulnerability Framework", version="0.1.0", lifespan=_lifespan)


@app.exception_handler(RequestValidationError)
async def _invalid_request(_request, exc):
    """A request body or query that does not fit the route answers with the same error body as every other error."""
    first = (exc.errors() or [{}])[0]
    where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("body", "query", "path"))
    reason = first.get("msg", "invalid request")
    return errors.error("The request is not valid", 422, detail=f"{where}: {reason}" if where else reason,
                        hint="Check the value of that field and try again.")


@app.exception_handler(jobs.TooManyJobs)
async def _too_many_jobs(_request, exc):
    return errors.error(str(exc), 409, hint="Open the Activity page to stop a job, or wait for one to finish.")


@app.exception_handler(jobs.Blocked)
async def _blocked_by_a_campaign(_request, exc):
    return errors.error(str(exc), 409, hint="Open the Campaigns page to pause or delete the campaign that holds the ecosystem.", code="campaign_holds_ecosystem")


def _database_down(_request, exc):
    """The database stopped answering mid-request (or never did): say so in the usual error body, so the UI shows its Start banner."""
    return errors.error("The local database is unavailable", 503, detail=str(exc)[:300] or type(exc).__name__,
                        hint="Check the local database file, folder permissions and available disk space on the Database page, then retry.", code="database_unavailable")


app.add_exception_handler(mongo.DatabaseUnavailable, _database_down)
app.add_exception_handler(__import__("sqlite3").OperationalError, _database_down)
app.add_exception_handler(ConnectionFailure, _database_down)                 # server selection timeouts, lost connections, network timeouts


@app.exception_handler(StarletteHTTPException)
async def _http_error(_request, exc):
    return errors.error(str(exc.detail), exc.status_code)

# No CORS: the UI is served by this same process. Only the app's own window may
# call the API (local Host, session cookie, same-origin WebSockets).
# The slow read-only routes are computed once at a time and served from memory (slowcache.py); it sits inside the access check.
if slowcache.enabled():
    app.add_middleware(slowcache.SlowRoutes)
app.add_middleware(security.LocalOnly)

from app.backend import (agent_api, build_api, campaigns_api, code_api, collections_api, context_provider_api, db_api, device_api, external_api, finetune_api, jobs_api, models_api, rag_api,  # noqa: E402
                         records_api, projects_api, project_ide_api, project_chat_context_api, reset_api, results_api, review_api, sandbox_api, serving_api, settings_api, shell_api, skills_api, train_api, verify_api)

# Every path keeps the literal it always had (no prefix); the order is the order the routes were declared in before the split.
for _module in (qualification_api, review_api, campaigns_api, build_api, finetune_api, settings_api, jobs_api, results_api, records_api, code_api, sandbox_api, db_api, device_api, shell_api,
                collections_api, context_provider_api, models_api, verify_api, train_api, rag_api, serving_api, project_ide_api, project_chat_context_api, projects_api, external_api, reset_api, agent_api, skills_api):
    app.include_router(_module.router)

# ``ZTSA_FRONTEND_DIST`` names another build to serve (the real-window probes serve ``dist-e2e``, so a build never replaces the files of ``dist`` under a window that is open on them)
FRONTEND_DIST = pathlib.Path(os.environ.get("ZTSA_FRONTEND_DIST") or pathlib.Path(__file__).resolve().parents[1] / "frontend" / "dist")


# ── frontend (served when built) ─────────────────────────────────────────────
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    # The SPA entry must never be cached, so a fresh build (which references new
    # hashed asset filenames) is always picked up. Hashed /assets/* stay cacheable.
    _NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    def _index():
        # serving the UI hands the window its session cookie (security.py)
        return FileResponse(FRONTEND_DIST / "index.html",
                            headers={**_NO_CACHE, "Set-Cookie": security.cookie_header()})

    _DIST = FRONTEND_DIST.resolve()

    @app.get("/")
    def index():
        return _index()

    @app.get("/{path:path}")
    def spa(path: str):
        f = (FRONTEND_DIST / path).resolve()
        if not f.is_relative_to(_DIST):          # e.g. /..%2F..%2F.env
            return errors.error("not found", 404)
        return FileResponse(f) if f.is_file() else _index()
else:
    @app.get("/")
    def placeholder():
        return JSONResponse({"message": "Backend running. Build the frontend (app/frontend) or run via app.desktop."},
                            headers={"Set-Cookie": security.cookie_header()})
