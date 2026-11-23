"""Single-flight, stale-while-revalidate cache for the slow read-only routes of the local API.

Why it exists: on a large working store (several GB) a handful of read-only routes (the data summaries, the review summary, the enrichment
candidates) scan whole tables and take seconds to minutes. A page asks for several of them at once and the Data page asks again whenever the
store changes, so dozens of scans run together, starve the server's workers and the browser's few connections per host, and every page, even
the cheap ones, stays on its loading skeleton. This middleware keeps those routes cheap to ask:

* **Single flight.** One computation per route and query at a time, whatever the number of askers; at most ``concurrency`` computations run at
  once, so the disk is not thrashed by twenty scans.
* **Stale while revalidating.** After the first answer, a request gets the stored answer at once; one older than its time to live also starts
  one refresh in the background.
* **Writes and finished jobs invalidate, narrowly.** A successful write through the API marks the answers it can change as needing a
  recomputation (the next request waits for a fresh one rather than being shown a value the write has made wrong): a review decision
  affects the review views only; any other write affects all of them. A finished job that changes the store does the same. Job starts and
  settings change nothing these routes show and are left out.
* **Nothing to hold a connection for.** A client that sends ``x-slow: poll`` (the UI does) is answered ``202`` with ``x-slow-cache: warming``
  while the answer is being computed and asks again; any other client simply waits for the answer.

Only 200 answers are stored; an error is passed on to the asker and never stored. It is off when ``ZTSA_SLOW_CACHE=0`` (the tests turn it
off, so a test that writes to the store and asks again sees the write).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
import weakref

# path -> seconds an answer stays fresh. Read-only routes only.
# (/api/data/stamp is not here: it is a few stat calls, and the page polls it to learn that something changed, so it is never cached and
# never waits behind a scan.)
GET_ROUTES = {
    "/api/code/dataset/options": 60,
    "/api/dataset/summary": 60,
    "/api/dataset/by-year": 60,
    "/api/dataset/facets": 30,
    "/api/code/summary": 60,
    "/api/data/review/summary": 120,
    "/api/data/review/queue": 120,
    "/api/finetune/baselines": 30,
    "/api/finetune/runs": 15,
    "/api/compute": 30,
}
# What is worth computing before anyone asks, cheapest first. The review queue is asked for with the query the Review page uses. The
# migration checks are kept in the store and asked for on purpose, so they are not here.
PREWARM = ("/api/finetune/runs", "/api/finetune/baselines", "/api/dataset/summary", "/api/code/dataset/options",
           "/api/code/summary", "/api/data/review/summary", "/api/data/review/queue?limit=20&offset=0")
# The one listing the Data page asks for when it opens (the defaults of its enrichment panel).
PREWARM_POSTS = (("/api/code/enrich/candidates", {"filters": {
    "levels": ["weak"], "missing": [], "min_advisory": 200, "splits": ["train", "val"], "status": "missing", "val_share": 0.05, "train_all": False,
    "order": "weakest", "languages": [], "severities": [], "cwes": [], "ecosystems": [], "granularity": "", "prefer_function": True, "max_lines": 300,
    "per_cve_cap": 5, "date_from": None, "date_to": None, "include_tests": False, "include_tangled": False, "exclude_suspect": True},
    "limit": 50, "offset": 0}),)
# Read-only routes that take a JSON body to say what to list.
POST_ROUTES = {"/api/code/enrich/candidates": 60, "/api/code/dataset/preview": 120}
# Writes that do not change what the cached routes show.
NOT_INVALIDATING = ("/api/jobs", "/api/settings")
# A write under this prefix changes only the cached routes under the prefixes listed (review decisions are stored apart from the records).
WRITE_SCOPES = {"/api/data/review/": ("/api/data/review/",)}
# Views that are cheap to compute: they have a lane of their own, so a page that needs one never waits for the scans of the big views.
CHEAP_ROUTES = frozenset({"/api/finetune/runs", "/api/finetune/baselines"})
# What a store-writing job can change: the answers under these prefixes. A stage not named anywhere invalidates everything.
STORE_VIEWS = ("/api/dataset/", "/api/code/", "/api/data/", "/api/campaigns/")
STORE_STAGES = frozenset({"ingest", "extract_fixes", "mine_code", "dep_versions", "verify_fixes", "resplit_pairs", "enrich_pairs", "recover_fixes",
                          "campaign_run", "dataset", "build_code_dataset", "build_reviewed_dataset", "evidence_recovery"})
# Jobs that never touch the store: they read it at most and write only under experiments/ or notes/ or to a project folder, so finishing one
# leaves every cached answer as it was. Every name is a real entry of jobs.STAGES (a test checks it); a stage not listed here, or in
# STAGE_SCOPES, invalidates everything.
STORE_NEUTRAL_STAGES = frozenset({"gpu_smoke", "gpu_eval", "train_local", "scan_project_deps", "scan_project_code", "scan_project_codebase", "plan_project_update", "convert_gguf", "convert_adapter", "import_external", "fetch_hub_dataset", "sandbox_build_image", "classify", "detect", "eval", "eval_detection", "eval_patch", "patch_eval", "compare",
                                  "rag_index", "rag_recall", "rag_index_project", "scan_deps", "scan_codebase", "patch", "respond_project", "trial_update", "collection_report",
                                  "phase_audit", "db_backup", "osv_download", "osv_install", "hf_download", "install_model", "install_ollama_model"})
# Jobs that change only some cached answers: finishing one recomputes just the routes under these prefixes (the runs and baselines views).
STAGE_SCOPES = {**{stage: ("/api/finetune/",) for stage in ("export_training_bundle", "finetune_validate", "import_finetune_outputs",
                                                             "import_finetune_eval")},
                **{stage: STORE_VIEWS for stage in STORE_STAGES}}
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
WARMING = b'{"warming": true, "detail": "this view is being computed on a large store; ask again in a moment"}'
INSTANCES: "weakref.WeakSet[SlowRoutes]" = weakref.WeakSet()


def enabled() -> bool:
    return os.environ.get("ZTSA_SLOW_CACHE", "1") != "0"


def prewarm_enabled() -> bool:
    """Startup scans are optional; the cache still computes any view when opened.

    In particular, the runs and baseline summaries verify training bundles and
    can read gigabytes unrelated to the selected project or its model load.
    """
    return enabled() and os.environ.get("ZTSA_PREWARM", "0") == "1"


def job_finished(stage: str) -> None:
    """Called when a job ends: the answers it may have changed are recomputed on the next ask."""
    if stage not in STORE_NEUTRAL_STAGES:
        scope = STAGE_SCOPES.get(stage)
        for cache in list(INSTANCES):
            cache.invalidate(scope)


class _Entry:
    __slots__ = ("status", "headers", "body", "at", "wall", "dirty", "invalidations", "task", "error")

    def __init__(self):
        self.status = self.headers = self.body = self.at = self.wall = self.task = self.error = None
        self.dirty, self.invalidations = False, 0


class SlowRoutes:
    """Pure ASGI middleware; put it inside the access-control middleware so only an allowed request is ever served from the cache."""

    def __init__(self, app, get_routes=None, post_routes=None, concurrency: int = 2, clock=time.monotonic, wall=time.time):
        self.app = app
        self.get_routes = dict(GET_ROUTES if get_routes is None else get_routes)
        self.post_routes = dict(POST_ROUTES if post_routes is None else post_routes)
        self.concurrency, self.clock, self.wall = concurrency, clock, wall
        self.entries: dict[tuple, _Entry] = {}
        self._semaphore: asyncio.Semaphore | None = None
        INSTANCES.add(self)

    # -- entry point ------------------------------------------------------------------------------------------------------------------------

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method, path = scope["method"], scope.get("path", "")
        ttl = self.get_routes.get(path) if method == "GET" else self.post_routes.get(path) if method == "POST" else None
        if ttl is None:
            if method in WRITE_METHODS and path.startswith("/api/") and not path.startswith(NOT_INVALIDATING):
                return await self.app(scope, receive, self._watch(send, path))
            return await self.app(scope, receive, send)
        body = b""
        if method == "POST":
            body = await _read_body(receive)
        key = (method, path, scope.get("query_string", b""), _body_key(body))
        entry = self.entries.setdefault(key, _Entry())
        usable = entry.body is not None and not entry.dirty
        if usable:
            fresh = self.clock() - entry.at < ttl
            if not fresh:
                self._ensure_task(key, entry, scope, body)
            return await self._answer(entry, send, "hit" if fresh else "stale")
        task = self._ensure_task(key, entry, scope, body)
        if _wants_poll(scope):
            return await _respond(send, 202, [(b"content-type", b"application/json"), (b"retry-after", b"2"), (b"x-slow-cache", b"warming")], WARMING)
        await asyncio.shield(task)
        if entry.body is not None and not entry.dirty:
            return await self._answer(entry, send, "miss")
        if entry.error is not None:
            status, headers, error_body = entry.error
            return await _respond(send, status, headers, error_body)
        return await _respond(send, 503, [(b"content-type", b"application/json")], b'{"error": "the view changed while it was computed; try again"}')

    # -- invalidation ------------------------------------------------------------------------------------------------------------------------

    def invalidate(self, prefixes: tuple | None = None) -> None:
        """Mark the answers of the routes under ``prefixes`` (all when None) as needing a recomputation before they are shown again."""
        for key, entry in list(self.entries.items()):
            if prefixes is None or key[1].startswith(prefixes):
                entry.dirty = True
                entry.invalidations += 1

    def _watch(self, send, path: str):
        scope = next((s for prefix, s in WRITE_SCOPES.items() if path.startswith(prefix)), None)

        async def watching(message):
            if message["type"] == "http.response.start" and message["status"] < 400:
                self.invalidate(scope)
            await send(message)
        return watching

    # -- computing ---------------------------------------------------------------------------------------------------------------------------

    def _ensure_task(self, key, entry, scope, body):
        if entry.task is None or entry.task.done():
            entry.task = asyncio.get_running_loop().create_task(self._compute(key, entry, scope, body))
        return entry.task

    async def _compute(self, key, entry, scope, body):
        seen = entry.invalidations                  # a write during the computation makes the result stale at once
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.concurrency)
        if scope.get("path", "") in CHEAP_ROUTES:                  # its own lane: it never waits for a scan to finish
            status, headers, out = await self._run(scope, body)
        else:
            async with self._semaphore:
                status, headers, out = await self._run(scope, body)
        if status == 200:
            entry.status, entry.headers, entry.body, entry.error = status, headers, out, None
            entry.at, entry.wall = self.clock(), self.wall()
            entry.dirty = entry.invalidations != seen
        else:
            entry.error = (status, headers, out)

    async def _run(self, scope, body):
        inner = {**scope, "headers": [(k, v) for k, v in scope.get("headers") or [] if k != b"x-slow"]}
        sent = {"status": 500, "headers": [], "body": bytearray()}
        replay = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return replay.pop(0) if replay else {"type": "http.disconnect"}

        async def collect(message):
            if message["type"] == "http.response.start":
                sent["status"], sent["headers"] = message["status"], list(message.get("headers") or [])
            elif message["type"] == "http.response.body":
                sent["body"].extend(message.get("body") or b"")
        try:
            await self.app(inner, receive, collect)
        except Exception as e:                      # the route failed: the asker gets the error, nothing is stored
            return 500, [(b"content-type", b"application/json")], json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
        return sent["status"], sent["headers"], bytes(sent["body"])

    async def _answer(self, entry, send, how: str):
        age = max(0.0, self.clock() - entry.at)
        headers = [h for h in entry.headers if h[0].lower() not in (b"x-slow-cache", b"x-slow-age", b"x-slow-computed")]
        headers += [(b"x-slow-cache", how.encode()), (b"x-slow-age", f"{age:.0f}".encode()), (b"x-slow-computed", f"{entry.wall:.0f}".encode())]
        await _respond(send, entry.status, headers, entry.body)


# -- helpers -----------------------------------------------------------------------------------------------------------------------------------

async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body") or b"")
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _body_key(body: bytes) -> str:
    """The same JSON in a different layout (spaces, key order) is the same question."""
    try:
        return hashlib.sha1(json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()).hexdigest() if body else ""
    except ValueError:
        return hashlib.sha1(body).hexdigest()


def _wants_poll(scope) -> bool:
    return any(k == b"x-slow" and v == b"poll" for k, v in scope.get("headers") or [])


async def _respond(send, status, headers, body: bytes):
    headers = [h for h in headers if h[0].lower() != b"content-length"] + [(b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# -- warming at start --------------------------------------------------------------------------------------------------------------------------

def prewarm(base_url: str, paths=None, posts=None, *, log=print) -> threading.Thread:
    """Explicitly request optional startup scans once, without blocking boot or retrying errors.

    The client stops waiting after 30 seconds per request and starts no more
    scans after a minute. A timed-out server computation may still finish and
    populate the single-flight cache; it must not be retried by this worker.
    """
    from app.backend import security
    todo = [(path, None) for path in (PREWARM if paths is None else paths)] + [(path, body) for path, body in (PREWARM_POSTS if posts is None else posts)]

    def work():
        deadline = time.monotonic() + 60

        def detail(value):
            return " ".join(str(value).replace(security.TOKEN, "[redacted]").split())[:300]

        for path, body in todo:
            started = time.monotonic()
            remaining = deadline - started
            if remaining <= 0:
                log("[desktop] startup warm budget reached; remaining views will be computed when opened")
                break
            try:
                headers = {"Cookie": f"{security.COOKIE}={security.TOKEN}", **({"Content-Type": "application/json"} if body is not None else {})}
                req = urllib.request.Request(base_url + path, data=None if body is None else json.dumps(body).encode(), headers=headers,
                                             method="GET" if body is None else "POST")
                with urllib.request.urlopen(req, timeout=min(30, remaining)) as response:
                    if response.status == 200:
                        log(f"[desktop] warmed {path} in {time.monotonic() - started:.0f} s")
                    else:
                        log(f"[desktop] warm pending {path}: HTTP {response.status}")
            except urllib.error.HTTPError as e:
                with e:
                    raw = e.read(1024).decode("utf-8", "replace")
                try:
                    parsed = json.loads(raw)
                    message = (parsed.get("detail") or parsed.get("error") or raw) if isinstance(parsed, dict) else raw
                except ValueError:
                    message = raw
                log(f"[desktop] could not warm {path}: HTTP {e.code} {detail(e.reason)}: {detail(message)}")
            except Exception as e:
                log(f"[desktop] could not warm {path}: {type(e).__name__}: {detail(e)}")
    thread = threading.Thread(target=work, name="slow-cache-prewarm", daemon=True)
    thread.start()
    return thread
