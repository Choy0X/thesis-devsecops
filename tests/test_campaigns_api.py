"""The routes of the campaigns: what the start dialog is offered, a plan judged, a campaign started, paused, resumed and deleted, and the manual jobs it keeps off its ecosystems."""
import json
import time

import pytest
from legacy_campaigns import add_legacy

from app.backend import jobs
from app.backend.pipeline import codestore, derived
from app.backend.pipeline.campaigns import admission, control, plan as planmod, repo
from app.backend.pipeline.db import mongo
from app.backend.pipeline.ingest import osv

NPM = {"ecosystems": ["npm"], "preset": "collect"}

pytestmark = [pytest.mark.mongo, pytest.mark.usefixtures("on_mongo")]


@pytest.fixture
def dumps(workspace, monkeypatch):
    raw, interim = workspace / "data" / "raw", workspace / "data" / "interim"
    monkeypatch.setattr(osv, "RAW", raw)
    monkeypatch.setattr(osv, "INTERIM", interim)
    for slug in ("npm", "pypi", "ubuntu"):
        (raw / f"osv_{slug}.zip").write_bytes(b"PK" + b"0" * 100)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_value_1234")
    return raw


class FakeJob:
    def __init__(self, n):
        self.id = f"job{n:09d}"

    def public(self):
        return {"id": self.id, "stage": "campaign_run", "status": "running"}


@pytest.fixture
def started(monkeypatch):
    """Starting a job records the request and spawns nothing."""
    seen = []

    def start(stage, params=None):
        seen.append((stage, dict(params or {})))
        return FakeJob(len(seen))
    monkeypatch.setattr(jobs, "start", start)
    return seen


@pytest.fixture
def no_process(monkeypatch):
    """The real ``jobs.start``, but no process is spawned and the jobs it registers are forgotten with the test."""
    from app.backend.pipeline.db import legacy
    legacy.handle()                                      # the MongoDB driver starts its monitor threads on first use, which must happen before threading.Thread is replaced below
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())


def hold(ecosystems, state="running"):
    """A campaign stored directly, holding its ecosystems."""
    with derived.connect() as conn:
        cid = repo.new_id(conn)
        plan = planmod.normalize({"ecosystems": ecosystems, "preset": "collect"})
        repo.claim(conn, cid, ecosystems)
        repo.create(conn, cid, name=None, plan=plan, ecosystems=ecosystems, state=state)
    return cid


@pytest.fixture
def roomy(monkeypatch):
    """A machine with room, whatever this one is."""
    real = admission.gather_facts

    def facts(conn, ecosystems=()):
        f = real(conn, ecosystems)
        f.physical_cores, f.ram_total_gb, f.ram_free_gb, f.disk_free_gb, f.jobs_running, f.jobs_max = 8, 32.0, 24.0, 500.0, 0, 8
        return f
    monkeypatch.setattr(admission, "gather_facts", facts)


def stored(cid):
    with derived.connect() as conn:
        return repo.get(conn, cid)


def test_compute_policy_validates_mode_and_requires_safe_boundary(client, dumps, roomy, workspace):
    assert client.post('/api/campaigns/validate', json={**NPM, 'compute': {'mode': 'bogus'}}).status_code == 422
    cid = hold(['npm'])
    assert client.post(f'/api/campaigns/{cid}/compute-policy', json={'mode': 'cuda'}).status_code == 409
    with derived.connect() as conn:
        original = stored(cid)['plan']
        repo.update(conn, cid, state='paused', plan={**original, 'workflow_version': 1})
    response = client.post(f'/api/campaigns/{cid}/compute-policy', json={'mode': 'cuda'})
    assert response.status_code == 200
    updated = response.json()
    assert updated['resume_required'] is True
    assert updated['plan']['workflow_version'] == 2
    assert updated['plan']['context']['manual'] is True
    assert updated['plan']['compute']['mode'] == 'cuda'
    assert stored(cid)['state'] == 'paused'


def test_mining_policy_is_explicit_paused_only_and_preserves_collection_settings(client, workspace):
    cid = hold(['npm'])
    route = f'/api/campaigns/{cid}/mining-policy'
    assert client.post(route, json={'version':2,'mine_workers':4,'parser_workers':1}).status_code == 409
    with derived.connect() as conn:
        original = stored(cid)['plan']
        repo.update(conn, cid, state='paused', plan={**original, 'mining':{'version':1}})
    assert client.post(route, json={'version':2,'mine_workers':5}).status_code == 422
    response = client.post(route, json={'version':2,'mine_workers':4,'parser_workers':1})
    assert response.status_code == 200
    updated = response.json()['plan']
    assert updated['mining'] == {'version':2,'parser_workers':1}
    assert updated['knobs']['mine_workers'] == 4
    for name in ('compute','context','ecosystems','stages','build_profile'):
        assert updated[name] == original[name]
    assert stored(cid)['state'] == 'paused'


def test_acquisition_profile_migration_requires_pause_and_is_recorded(client, workspace):
    cid = hold(['npm'])
    route = f'/api/campaigns/{cid}/mining-policy'
    selection = {'version': 4, 'parser_workers': 1, 'acquisition_version': 2}
    assert client.post(route, json=selection).status_code == 409
    with derived.connect() as conn:
        original = stored(cid)['plan']
        repo.update(conn, cid, state='paused')
    response = client.post(route, json=selection)
    assert response.status_code == 200
    assert response.json()['resume_required'] is True
    assert response.json()['plan']['mining'] == selection
    assert stored(cid)['state'] == 'paused'
    for field in ('compute', 'context', 'ecosystems', 'stages', 'build_profile'):
        assert response.json()['plan'][field] == original[field]


def test_compute_migration_does_not_silently_upgrade_historical_mining(client, workspace):
    cid = hold(['npm'], state='paused')
    with derived.connect() as conn:
        old = stored(cid)['plan']
        old.pop('mining', None)
        repo.update(conn, cid, plan=old)
    response = client.post(f'/api/campaigns/{cid}/compute-policy', json={'mode':'cpu'})
    assert response.status_code == 200
    assert response.json()['plan']['mining'] == {'version':1}
    with derived.connect() as conn:
        assert repo.events(conn, cid)[-1]['kind'] == 'compute_policy_changed'


def test_auto_is_historical_only_and_requires_paused_migration(client, dumps, roomy, workspace):
    assert client.post('/api/campaigns/validate', json={**NPM, 'compute': {'mode': 'auto'}}).status_code == 422
    cid = hold(['npm'], state='paused')
    with derived.connect() as conn:
        value = repo.get(conn, cid)['plan']
        value['compute'] = {'mode': 'auto'}
        repo.update(conn, cid, plan=value)
    assert client.get(f'/api/campaigns/{cid}').json()['campaign']['plan']['compute'] == {'mode': 'auto'}
    assert client.post(f'/api/campaigns/{cid}/resume').status_code == 409
    assert stored(cid)['plan']['compute'] == {'mode': 'auto'}
    assert client.post(f'/api/campaigns/{cid}/compute-policy', json={'mode': 'cpu'}).json()['plan']['compute'] == {'mode': 'cpu', 'version': 2}


def test_cuda_configuration_is_checked_before_creating_job(client, dumps, roomy, started, monkeypatch):
    from app.backend import compute
    monkeypatch.setattr(compute, 'gpu_info', lambda **kw: {'available': False, 'reason': 'CUDA runtime unavailable'})
    body = {**NPM, 'compute': {'mode': 'cuda'}}
    value = client.post('/api/campaigns/validate', json=body).json()
    assert any(e['code'] == 'cuda_configuration' for e in value['errors'])
    assert client.post('/api/campaigns', json=body).status_code == 422
    assert started == []


def test_manual_context_selection_cannot_borrow_another_campaigns_pairs(client, dumps, workspace):
    from app.backend.pipeline.acceleration.context_gate import require_ready
    from app.backend.pipeline.db import legacy
    cid = hold(['npm'], state='done')
    db = legacy.handle()
    db['code_pairs'].insert_many([{'_id': 1, 'pair_key': 'pair-ours', 'created_at': '2026-10-20', 'commit_key': 'ours'},
                                  {'_id': 2, 'pair_key': 'pair-other', 'created_at': '2026-10-20', 'commit_key': 'other'}])
    db['fix_commits'].insert_many([{'commit_key': 'ours', 'created_at': '2026-10-20', 'queue': {'campaign_id': cid, 'status': 'mined'}},
                                   {'commit_key': 'other', 'created_at': '2026-10-20', 'queue': {'campaign_id': 'different', 'status': 'mined'}}])
    require_ready(cid, [1])
    with pytest.raises(ValueError, match='not collected'):
        require_ready(cid, [2])
    with pytest.raises(ValueError, match='not collected'):
        require_ready(cid, [999])


def test_stop_compute_owner_rejects_changed_ownership(client, monkeypatch):
    from app.backend.pipeline.gateway import client as gateway
    monkeypatch.setattr(gateway, 'status', lambda: {'exclusive': {'owner': 'new-owner', 'pid': 123}})
    assert client.post('/api/campaigns/compute/stop-owner', json={'owner': 'old-owner'}).status_code == 409


def start(client, body=None, expect=201):
    r = client.post("/api/campaigns", json=body or NPM)
    assert r.status_code == expect, r.text
    return r.json()


# -- what the dialog is offered ------------------------------------------------------------------------------------------------

def test_the_options_list_the_dumps_the_presets_the_stages_and_the_profiles(client, dumps):
    o = client.get("/api/campaigns/options").json()
    assert [t["name"] for t in o["targets"]] == ["npm", "pypi", "ubuntu"]
    assert next(t for t in o["targets"] if t["name"] == "ubuntu")["mineable"] is False
    assert [p["id"] for p in o["presets"]] == ["ingest_only", "collect", "collect_verify", "full", "full_context"] and o["default_preset"] == "collect"
    assert [s["id"] for s in o["stages"]] == [s for s in planmod.STAGE_ORDER if s != 'enrich'] and next(s for s in o["stages"] if s["id"] == "build")["scope"] == "campaign"
    assert {"training", "final", "max_data", "high_quality"} <= {p["id"] for p in o["build_profiles"]}
    assert [p["id"] for p in o["context_profiles"]] == ["weak", "weak_partial", "all", "redo"] and o["knobs"]["mine_workers"] == 4 and o["knob_limits"]["mine_workers"] == [1, 16]


def test_the_resources_say_what_there_is_and_what_the_running_campaigns_use(client, dumps, started, roomy):
    first = start(client)["campaign"]["campaign_id"]
    r = client.get("/api/campaigns/resources").json()
    assert r["cores"] == 8 and r["cpu_cap"] == 7 and r["ram_total_gb"] == 32.0 and r["github_token"] is True and r["used"]["cpu"] == 2
    assert [c["campaign_id"] for c in r["campaigns"]] == [first] and r["used"]["github_per_hour"] > 0


# -- judging a plan ----------------------------------------------------------------------------------------------------------

def test_a_plan_is_judged_without_starting_anything(client, dumps, started, roomy):
    v = client.post("/api/campaigns/validate", json={"ecosystems": ["npm", "ubuntu"], "preset": "collect_verify"}).json()
    assert v["ok"] is True and v["errors"] == [] and v["per_ecosystem"]["ubuntu"]["stages"] == ["ingest"] and v["per_ecosystem"]["npm"]["stages"][-1] == "resplit"
    assert [w["code"] for w in v["warnings"]] == ["ingest_only"] and v["needs"]["cpu"] == 2 and v["plan"]["preset"] == "collect_verify"
    assert started == [] and client.get("/api/campaigns").json()["campaigns"] == []
    bad = client.post("/api/campaigns/validate", json={"ecosystems": ["Go"], "stages": {"queue": True}}).json()
    assert bad["ok"] is False and bad["errors"][0] == {**bad["errors"][0], "code": "no_dump", "source": "plan"}


def test_a_plan_the_machine_cannot_take_is_refused_with_the_numbers(client, dumps, started, monkeypatch):
    real = admission.gather_facts
    monkeypatch.setattr(admission, "gather_facts", lambda conn, ecosystems=(): (lambda f: (setattr(f, "ram_total_gb", 4.0), setattr(f, "ram_free_gb", 1.2), f)[2])(real(conn, ecosystems)))
    v = client.post("/api/campaigns/validate", json=NPM).json()
    ram = next(e for e in v["errors"] if e["code"] == "ram_free")
    assert ram["source"] == "machine" and ram["numbers"]["free"] == 1.2 and "1.2 GB is free" in ram["text"]


# -- starting ----------------------------------------------------------------------------------------------------------------

def test_a_campaign_starts_with_its_records_its_claim_its_job_and_a_run_config(client, dumps, started, roomy, workspace):
    body = start(client, {"name": "npm first", **NPM})
    c = body["campaign"]
    assert body["ok"] is True and c["state"] == "running" and c["name"] == "npm first" and c["ecosystems"] == ["npm"] and c["declared"]["cpu"] == 2
    assert started == [("campaign_run", {"campaign_id": c["campaign_id"], "campaign_name": "npm first"})] and body["job"]["id"] == "job000000001"
    s = stored(c["campaign_id"])
    assert s["job_id"] == "job000000001" and s["plan"]["stages"]["mine"] is True
    with derived.connect() as conn:
        assert repo.owners(conn) == {"npm": c["campaign_id"]} and [e["kind"] for e in repo.events(conn, c["campaign_id"])] == ["created"]
    text = (workspace / "experiments" / "campaigns" / c["campaign_id"] / "run_config.json").read_text(encoding="utf-8")
    cfg = json.loads(text)
    assert cfg["task"] == "campaign" and cfg["plan"]["ecosystems"] == ["npm"] and cfg["github_token_present"] is True and cfg["database"] == mongo.db_name()
    assert "ghp_secret_value_1234" not in text, "the token is never written down"


def test_a_plan_that_cannot_work_on_this_data_is_refused_and_nothing_is_stored(client, dumps, started, roomy):
    r = client.post("/api/campaigns", json={"ecosystems": ["Go"], "stages": {"mine": True}})
    assert r.status_code == 422 and r.json()["code"] == "plan_invalid" and r.json()["reasons"][0]["code"] == "no_dump"
    assert started == [] and client.get("/api/campaigns").json() == {"campaigns": [], "interrupted": [], "owners": {}, "total": 0, "limit": 100, "offset": 0}


def test_an_ecosystem_that_a_campaign_holds_cannot_be_taken_by_another(client, dumps, started, roomy):
    first = start(client)["campaign"]["campaign_id"]
    r = client.post("/api/campaigns", json={"ecosystems": ["npm", "pypi"], "preset": "ingest_only"})
    assert r.status_code == 409 and r.json()["code"] == "no_room" and first in r.json()["reasons"][0]["text"] and r.json()["reasons"][0]["code"] == "ecosystem_busy"
    assert len(client.get("/api/campaigns").json()["campaigns"]) == 1


def test_two_campaigns_on_different_ecosystems_run_together_until_the_room_runs_out(client, dumps, started, monkeypatch):
    real = admission.gather_facts

    def facts(conn, ecosystems=()):
        f = real(conn, ecosystems)
        f.physical_cores, f.ram_total_gb, f.ram_free_gb, f.disk_free_gb, f.jobs_running, f.jobs_max = 6, 32.0, 24.0, 500.0, 0, 8        # 5 cores can be spared: 2 + 2, not three
        return f
    monkeypatch.setattr(admission, "gather_facts", facts)
    start(client, {"ecosystems": ["npm"], "stages": {"ingest": True, "queue": True, "mine": True}, "knobs": {"mine_workers": 4}})
    start(client, {"ecosystems": ["pypi"], "stages": {"ingest": True, "queue": True, "mine": True}, "knobs": {"mine_workers": 4}})
    r = client.post("/api/campaigns", json={"ecosystems": ["ubuntu"], "stages": {"ingest": True, "mine": True}})
    assert r.status_code == 409 and "cpu" in [x["code"] for x in r.json()["reasons"]]
    cpu = next(x for x in r.json()["reasons"] if x["code"] == "cpu")
    assert cpu["numbers"] == {"needs": 2, "used": 4, "cap": 5}
    assert len(client.get("/api/campaigns").json()["campaigns"]) == 2


def test_a_job_that_cannot_start_leaves_no_campaign_behind(client, dumps, roomy, monkeypatch, workspace):
    monkeypatch.setattr(jobs, "start", lambda stage, params=None: (_ for _ in ()).throw(jobs.TooManyJobs("4 jobs are already running")))
    r = client.post("/api/campaigns", json=NPM)
    assert r.status_code == 409 and "4 jobs are already running" in r.json()["error"]
    assert client.get("/api/campaigns").json() == {"campaigns": [], "interrupted": [], "owners": {}, "total": 0, "limit": 100, "offset": 0}
    assert not list((workspace / "experiments" / "campaigns").glob("*")) if (workspace / "experiments" / "campaigns").exists() else True


# -- reading one -------------------------------------------------------------------------------------------------------------

def test_a_campaign_shows_every_step_it_will_take_and_where_it_is(client, dumps, started, roomy):
    cid = start(client, {"ecosystems": ["npm", "ubuntu"], "stages": {"ingest": True, "queue": True, "mine": True}})["campaign"]["campaign_id"]
    with derived.connect() as conn:
        repo.put_step(conn, cid, "npm", "ingest", "done", detail={"rows": 5})
        repo.put_step(conn, cid, "npm", "queue", "running")
    got = client.get(f"/api/campaigns/{cid}").json()
    assert [(s["target"], s["stage"], s["status"]) for s in got["steps"]] == [("npm", "ingest", "done"), ("npm", "queue", "running"), ("npm", "mine", "pending"), ("ubuntu", "ingest", "pending")]
    assert got["campaign"]["campaign_id"] == cid and got["campaign"]["plan"]["ecosystems"] == ["npm", "ubuntu"] and got["job"] is None
    assert client.get("/api/campaigns/cmp-nothing").status_code == 404


def test_the_story_of_a_campaign_is_read_from_a_point(client, dumps, started, roomy):
    cid = start(client)["campaign"]["campaign_id"]
    with derived.connect() as conn:
        for k in ("started", "stage_started", "stage_done"):
            repo.add_event(conn, cid, k)
    everything = client.get(f"/api/campaigns/{cid}/events").json()
    assert [e["kind"] for e in everything["events"]] == ["created", "started", "stage_started", "stage_done"] and everything["last"] == everything["events"][-1]["id"]
    later = client.get(f"/api/campaigns/{cid}/events", params={"after": everything["events"][1]["id"], "limit": 1}).json()
    assert [e["kind"] for e in later["events"]] == ["stage_started"]
    assert client.get("/api/campaigns/cmp-nothing/events").status_code == 404


def test_the_list_shows_the_older_kind_too_and_finds_a_campaign_whose_process_is_gone(client, dumps, started, roomy):
    with derived.connect() as conn:
        add_legacy(conn, "campaign-20260101-000000", state="done")
    cid = start(client, {"ecosystems": ["pypi"], "preset": "ingest_only"})["campaign"]["campaign_id"]
    with derived.connect() as conn:
        repo.update(conn, cid, pid=4_194_000, pid_started=1.0, heartbeat_ts=time.time())
    got = client.get("/api/campaigns").json()
    assert [c["campaign_id"] for c in got["campaigns"]] == [cid, "campaign-20260101-000000"] and [c["legacy"] for c in got["campaigns"]] == [False, True]
    assert got["interrupted"] == [cid] and got["campaigns"][0]["state"] == "interrupted" and got["owners"] == {"pypi": cid}


def test_the_routes_of_the_first_system_are_gone(client, workspace):
    for path in ("/api/campaigns/status", "/api/campaigns/plan"):
        assert client.get(path).status_code == 404, "these now read as a campaign of that name, which does not exist"
    assert client.post("/api/campaigns/start", json={}).status_code in (404, 405)
    assert client.post("/api/campaigns/pause", json={}).status_code in (404, 405)


# -- pause, resume, delete ---------------------------------------------------------------------------------------------------

def test_a_running_campaign_is_asked_to_pause_once(client, dumps, started, roomy):
    cid = start(client)["campaign"]["campaign_id"]
    r = client.post(f"/api/campaigns/{cid}/pause")
    assert r.status_code == 200 and r.json()["campaign"]["state"] == "pausing" and stored(cid)["control"] == "pause"
    again = client.post(f"/api/campaigns/{cid}/pause")
    assert again.status_code == 409 and "pausing" in again.json()["error"]
    assert client.post("/api/campaigns/cmp-nothing/pause").status_code == 404


def test_a_paused_campaign_is_resumed_with_its_room_judged_again(client, dumps, started, roomy):
    cid = start(client)["campaign"]["campaign_id"]
    client.post(f"/api/campaigns/{cid}/pause")
    with derived.connect() as conn:
        repo.update(conn, cid, state="paused")
    r = client.post(f"/api/campaigns/{cid}/resume")
    assert r.status_code == 200 and r.json()["campaign"]["state"] == "running" and r.json()["campaign"]["resumes"] == 1 and r.json()["job"]["id"] == "job000000002"
    assert started[-1] == ("campaign_run", {"campaign_id": cid, "campaign_name": None}) and stored(cid)["control"] == "run" and stored(cid)["job_id"] == "job000000002"
    assert client.post(f"/api/campaigns/{cid}/resume").status_code == 409, "it is running now"


def test_a_resume_is_refused_when_the_room_has_gone(client, dumps, started, roomy, monkeypatch):
    cid = start(client)["campaign"]["campaign_id"]
    with derived.connect() as conn:
        repo.update(conn, cid, state="paused")
    real = admission.gather_facts
    monkeypatch.setattr(admission, "gather_facts", lambda conn, ecosystems=(): (lambda f: (setattr(f, "disk_free_gb", 3.0), f)[1])(real(conn, ecosystems)))
    r = client.post(f"/api/campaigns/{cid}/resume")
    assert r.status_code == 409 and r.json()["reasons"][0]["code"] == "disk" and stored(cid)["state"] == "paused" and len(started) == 1


def test_a_partial_campaign_is_retried_by_resuming_it_and_takes_its_ecosystems_again(client, dumps, started, roomy):
    cid = start(client)["campaign"]["campaign_id"]
    with derived.connect() as conn:
        repo.update(conn, cid, state="partial")
        repo.release(conn, cid)
        assert repo.owners(conn) == {}
    r = client.post(f"/api/campaigns/{cid}/resume")
    assert r.status_code == 200 and r.json()["campaign"]["state"] == "running"
    with derived.connect() as conn:
        assert repo.owners(conn) == {"npm": cid}


def test_a_resume_that_cannot_start_its_job_puts_the_campaign_back(client, dumps, roomy, started, monkeypatch):
    cid = start(client)["campaign"]["campaign_id"]
    with derived.connect() as conn:
        repo.update(conn, cid, state="paused")
    monkeypatch.setattr(jobs, "start", lambda stage, params=None: (_ for _ in ()).throw(jobs.TooManyJobs("4 jobs are already running")))
    assert client.post(f"/api/campaigns/{cid}/resume").status_code == 409
    assert stored(cid)["state"] == "paused"


def test_a_running_campaign_cannot_be_deleted_and_a_paused_one_can_without_losing_what_it_produced(client, dumps, started, roomy, workspace):
    cid = start(client)["campaign"]["campaign_id"]
    r = client.delete(f"/api/campaigns/{cid}")
    assert r.status_code == 409 and "Pause it" in r.json()["hint"]
    codestore.queue_fix_commits([{"host": "github.com", "owner": "o", "repo": "r", "sha": "a" * 40, "repo_url": "https://github.com/o/r", "commit_key": "github.com/o/r@" + "a" * 7,
                                  "advisory_id": "GHSA-1", "cve_id": None, "source": "osv", "via": "osv_ref_web", "ecosystem": "npm"}])
    with derived.connect() as conn:
        repo.update(conn, cid, state="paused")
        repo.put_step(conn, cid, "npm", "ingest", "done")
        repo.add_sample(conn, cid, {"x": 1})
    folder = workspace / "experiments" / "campaigns" / cid
    assert folder.is_dir()
    r = client.delete(f"/api/campaigns/{cid}")
    assert r.status_code == 200 and r.json()["deleted"] == cid and "stay where they are" in r.json()["kept"]
    assert stored(cid) is None and not folder.exists()
    with derived.connect() as conn:
        assert repo.owners(conn) == {} and repo.steps(conn, cid) == {} and repo.samples(conn, cid) == [] and repo.events(conn, cid) == []
    assert codestore.count_fix_commits() == 1, "the commit it queued is still there"
    assert client.delete(f"/api/campaigns/{cid}").status_code == 404


def test_deleting_a_campaign_forgets_the_job_that_ran_it_so_it_cannot_be_mistaken_for_a_later_one(client, dumps, no_process, roomy, workspace):
    cid = start(client)["campaign"]["campaign_id"]
    job_id = stored(cid)["job_id"]
    assert jobs.get(job_id) is not None and jobs.get(job_id).status == "running"
    jobs.get(job_id).status = "done"                      # the job really finished (nothing here spawns a real process to do that itself)
    with derived.connect() as conn:
        repo.update(conn, cid, state="paused")
    r = client.delete(f"/api/campaigns/{cid}")
    assert r.status_code == 200
    assert jobs.get(job_id) is None, "the job that ran this campaign is gone with it, not left to show up unlabelled in the Jobs tray later"


def test_deleting_a_campaign_with_no_job_ever_started_does_nothing_extra(client, dumps, roomy, workspace):
    cid = hold(["npm"], state="paused")
    assert stored(cid)["job_id"] is None
    r = client.delete(f"/api/campaigns/{cid}")
    assert r.status_code == 200 and r.json()["deleted"] == cid


# -- manual jobs -------------------------------------------------------------------------------------------------------------

def test_a_manual_job_on_an_ecosystem_a_campaign_holds_is_refused_with_the_campaign_named(client, dumps, no_process):
    cid = hold(["npm"])
    for stage, params in (("osv_install", {"ecosystem": "npm"}), ("osv_download", {"ecosystem": "npm"}), ("ingest", {"ecosystems": ["npm", "PyPI"]}), ("recover_fixes", {"ecosystems": ["npm"]}),
                          ("mine_code", {}), ("extract_fixes", {})):
        r = client.post("/api/jobs", json={"stage": stage, "params": params})
        assert r.status_code == 409 and r.json()["code"] == "campaign_holds_ecosystem" and cid in r.json()["error"], (stage, r.text)
    r = client.post("/api/jobs", json={"stage": "osv_install", "params": {"ecosystem": "PyPI"}})
    assert r.status_code == 200, r.text
    assert client.post("/api/jobs", json={"stage": "recover_fixes", "params": {"ecosystems": ["Go"]}}).status_code == 200


def test_a_paused_campaign_still_keeps_manual_jobs_off_its_ecosystem(client, dumps, no_process):
    hold(["npm"], state="paused")
    assert client.post("/api/jobs", json={"stage": "mine_code", "params": {}}).status_code == 409


def test_once_the_campaign_has_ended_the_ecosystem_is_free_for_manual_jobs(client, dumps, no_process):
    cid = hold(["npm"])
    with derived.connect() as conn:
        repo.update(conn, cid, state="done")
        repo.release(conn, cid)
    assert client.post("/api/jobs", json={"stage": "mine_code", "params": {}}).status_code == 200


def test_the_app_marks_a_campaign_whose_process_did_not_survive_as_interrupted_when_it_starts(dumps, started, roomy, workspace):
    from fastapi.testclient import TestClient
    from app.backend import api, security
    with derived.connect() as conn:
        plan = planmod.normalize(NPM)
        repo.claim(conn, "cmp-20261010-aaaa", ["npm"])
        repo.create(conn, "cmp-20261010-aaaa", name=None, plan=plan, ecosystems=["npm"])
        repo.update(conn, "cmp-20261010-aaaa", pid=4_194_000, pid_started=1.0, heartbeat_ts=time.time())
    with TestClient(api.app, base_url="http://127.0.0.1"):
        pass
    assert stored("cmp-20261010-aaaa")["state"] == "interrupted"
    with derived.connect() as conn:
        assert repo.owners(conn) == {"npm": "cmp-20261010-aaaa"}, "it keeps its ecosystems and can be resumed"


def test_new_campaign_context_is_manual_and_does_not_require_a_loaded_model(client, dumps, started, roomy, monkeypatch):
    from app.backend.pipeline.campaigns import stages
    body = {"ecosystems": ["npm"], "preset": "full_context", "context": {"profile": "all", "workers": 3}}
    monkeypatch.setattr(stages, "model_check", lambda plan=None, **kw: (False, "The model server at 127.0.0.1:8080 did not answer."))
    v = client.post("/api/campaigns/validate", json=body).json()
    assert v['ok'] is True and v['plan']['context']['manual'] is True and v['plan']['workflow_version'] == 2
    monkeypatch.setattr(stages, "model_check", lambda plan=None, **kw: (True, ""))
    v = client.post("/api/campaigns/validate", json=body).json()
    assert v["ok"] is True and v["needs"]["context"] is False
    first = start(client, body)["campaign"]["campaign_id"]
    assert stored(first)["declared"]["context"] is False
    second = client.post("/api/campaigns", json={**body, "ecosystems": ["pypi"]})
    assert second.status_code == 409
    assert 'one_context' not in [r['code'] for r in second.json()['reasons']]
    assert 'github_budget' in [r['code'] for r in second.json()['reasons']]


def test_the_analytics_of_a_campaign_are_served_and_a_missing_one_is_not_found(client, dumps, started, roomy):
    from app.backend.pipeline.campaigns import analytics
    analytics.forget()
    cid = start(client)["campaign"]["campaign_id"]
    got = client.get(f"/api/campaigns/{cid}/analytics")
    assert got.status_code == 200
    a = got.json()
    assert a["campaign_id"] == cid and a["legacy"] is False and [s["stage"] for s in a["timeline"]["steps"]] == ["ingest", "queue", "recover", "mine"]
    assert set(a) >= {"timeline", "errors", "funnel", "throughput", "mix", "context", "outcomes"} and a["context"] is None
    assert client.get("/api/campaigns/cmp-nothing/analytics").status_code == 404
    analytics.forget()
