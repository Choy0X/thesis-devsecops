"""Read-only dependency and project source reviews."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote, urlsplit
import urllib.request
import uuid

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from app.backend.pipeline.ecosystems import versions as ordering
from . import scans_registry
from .scans_inventory import inventory, norm, public_observation, safe_path

MAX_RESPONSE = 32 * 1024 * 1024
MAX_ARTIFACT = 64 * 1024 * 1024
HOSTS = scans_registry.HOSTS | {"files.pythonhosted.org", "api.osv.dev", "services.nvd.nist.gov"}


class ScanCancelled(RuntimeError):
    pass


def check_cancel(cancelled):
    if cancelled and (cancelled() if callable(cancelled) else cancelled.is_set()):
        raise ScanCancelled("Dependency scan cancelled.")


def validate_url(url):
    part = urlsplit(url)
    if part.scheme != "https" or part.hostname not in HOSTS or part.username or part.password or part.port not in (None, 443):
        raise ValueError("Only the public HTTPS registry/advisory hosts are permitted.")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url, payload=None, limit=MAX_RESPONSE):
    validate_url(url)
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={"User-Agent": "ProjectsAgentIDE/1 dependency-analysis", "Accept": "application/json", "Content-Type": "application/json"})
    # Ignore ambient proxy credentials/settings. Redirects are checked before following.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirect())
    with opener.open(request, timeout=15) as response:
        if int(response.headers.get("Content-Length", 0)) > limit: raise ValueError("Response exceeds scan limit.")
        data = response.read(limit + 1)
        if len(data) > limit: raise ValueError("Response exceeds scan limit.")
        return data


def _fetch_json(url, payload=None):
    return json.loads(_download(url, payload))


def _fresh(lookup):
    return lookup["status"] == "fresh"


def _satisfies(eco, version, spec):
    """Conservative declared-range compatibility. Unsupported expressions stay unknown."""
    if not spec or spec == "*": return True
    try:
        if eco == "PyPI":
            if re.fullmatch(r"[~^]\s*\d+(?:\.\d+){0,2}", spec): return _satisfies("npm", version, spec)
            if re.fullmatch(r"\d+(?:\.\d+)*(?:\.\*)?", spec): spec = "==" + spec
            return Version(version) in SpecifierSet(spec)
        if eco == "Go":
            if not ordering.key(eco, spec).valid:
                return None
            wanted = ordering.normalize(eco, spec).split(".")
            proposed = ordering.normalize(eco, version).split(".")
            width = 2 if wanted[0] == "0" else 1
            return proposed[:width] == wanted[:width] and ordering.compare(eco, version, spec) >= 0
        if eco in {"Maven", "NuGet"}:
            exact = re.fullmatch(r"\[([A-Za-z0-9_.+-]+)\]", spec)
            if exact and ordering.key(eco, exact[1]).valid:
                return ordering.compare(eco, version, exact[1]) == 0
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", spec) and ordering.key(eco, spec).valid:
                comparison = ordering.compare(eco, version, spec)
                return comparison >= 0 if eco == "NuGet" else comparison == 0
            match = re.fullmatch(r"([\[(])\s*([A-Za-z0-9_.+-]*)\s*,\s*([A-Za-z0-9_.+-]*)\s*([\])])", spec)
            if not match:
                return None
            start, lower, upper, end = match.groups()
            if any(bound and not ordering.key(eco, bound).valid for bound in (lower, upper)):
                return None
            cmp_lower = ordering.compare(eco, version, lower) if lower else 1
            cmp_upper = ordering.compare(eco, version, upper) if upper else -1
            return (cmp_lower > 0 or (cmp_lower == 0 and start == "[")) and (cmp_upper < 0 or (cmp_upper == 0 and end == "]"))
        if eco == "crates.io" and re.fullmatch(r"\d+(?:\.\d+){0,2}", spec):
            spec = "^" + spec
        if eco == "crates.io" and spec.startswith("=") and ordering.key(eco, spec[1:].strip()).valid:
            return ordering.compare(eco, version, spec[1:].strip()) == 0
        if "||" in spec:
            values = [_satisfies(eco, version, s.strip()) for s in spec.split("||")]
            return True if True in values else (None if None in values else False)
        if re.fullmatch(r"\d+(?:\.\d+){0,2}", spec):
            parts = spec.split(".")
            if len(parts) < 3: return version.split(".")[:len(parts)] == parts
            return ordering.compare(eco, version, spec) == 0
        match = re.fullmatch(r"([~^])\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", spec)
        if match:
            sign, major, minor, patch = match.groups(); major, minor, patch = int(major), int(minor or 0), int(patch or 0)
            low = f"{major}.{minor}.{patch}"
            if sign == "~": high = f"{major + 1}.0.0" if match.group(3) is None else f"{major}.{minor + 1}.0"
            elif major or match.group(3) is None: high = f"{major + 1}.0.0"
            elif minor: high = f"0.{minor + 1}.0"
            elif match.group(4) is None: high = "0.1.0"
            else: high = f"0.0.{patch + 1}"
            return ordering.compare(eco, version, low) >= 0 and ordering.compare(eco, version, high) < 0
        if re.fullmatch(r"\d+(?:\.\d+)?\.[xX*]", spec): return version.split(".")[:len(spec.split(".")) - 1] == spec.split(".")[:-1]
        if re.fullmatch(r"(?:[<>=]+\s*\d+(?:\.\d+){0,2}\s*)+", spec):
            return Version(version) in SpecifierSet(",".join(re.findall(r"[<>=]+\s*\d+(?:\.\d+){0,2}", spec)).replace(" ", ""))
        return None
    except (ValueError, TypeError): return None


def _match_entry(eco, name, version, entry):
    """True/False/None: exact package identity plus explicit versions or OSV events."""
    package = entry.get("package")
    if not isinstance(package, dict) or not isinstance(package.get("name"), str):
        return None
    if package.get("ecosystem") != eco or norm(eco, package.get("name", "")) != name: return False
    parsed = ordering.normalize(eco, version)
    versions, ranges = entry.get("versions", []), entry.get("ranges", [])
    if not isinstance(versions, list) or not isinstance(ranges, list):
        return None
    if any(isinstance(candidate, str) and ordering.normalize(eco, candidate) == parsed for candidate in versions):
        return True
    uncertain = any(not isinstance(candidate, str) or not ordering.key(eco, candidate).valid for candidate in versions)
    for interval in ranges:
        if not isinstance(interval, dict) or interval.get("type") not in {"ECOSYSTEM", "SEMVER"}:
            uncertain = True
            continue
        events = interval.get("events")
        if not isinstance(events, list) or not events:
            uncertain = True
            continue
        if any(not isinstance(event, dict) or len(event) != 1 or
               any(kind not in {"introduced", "fixed", "last_affected", "limit"} or not isinstance(bound, str) or
                   (bound != "0" and not ordering.key(eco, bound).valid) for kind, bound in event.items())
               for event in events):
            uncertain = True
            continue
        active = False
        for event in events:
            if "introduced" in event: active = event["introduced"] == "0" or ordering.compare(eco, version, event["introduced"]) >= 0
            elif "fixed" in event:
                if active and ordering.compare(eco, version, event["fixed"]) < 0: return True
                active = False
            elif "last_affected" in event:
                if active and ordering.compare(eco, version, event["last_affected"]) <= 0: return True
                active = False
            elif "limit" in event:
                if active and ordering.compare(eco, version, event["limit"]) < 0: return True
                active = False
        if active: return True
    return None if uncertain or not (ranges or versions) else False



class _AnswerCache:
    """Model answers by (model digest, protocol, prompt), one JSON line each; a rescan of unchanged code reuses them.
    Without a model digest nothing is cached."""

    def __init__(self, path: Path, identity: dict):
        self.path, self.digest = Path(path), identity.get("digest") or ""
        self.prefix = f"{self.digest}|{identity.get('protocol')}|"
        self.rows = {}
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                self.rows[row["key"]] = row["answer"]
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _key(self, messages):
        return hashlib.sha256((self.prefix + json.dumps(messages, sort_keys=True)).encode()).hexdigest()

    def get(self, messages):
        return self.rows.get(self._key(messages)) if self.digest else None

    def put(self, messages, answer):
        if not self.digest:
            return
        key = self._key(messages)
        self.rows[key] = answer
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"key": key, "answer": answer}) + "\n")

class ScanService:
    def __init__(self, cache: Path, fetch_json=None, fetch_bytes=None):
        self.cache = Path(cache).absolute()
        self.fetch_json = fetch_json or _fetch_json
        self.fetch_bytes = fetch_bytes or (lambda url: _download(url, limit=MAX_ARTIFACT))

    def model_review(self, workspace_root: Path, cancelled, *, progress=None, chat=None) -> dict:
        """Review the project's source with the local v40 reviewer in two passes (app/backend/pipeline/project_scan.py).

        Decoding is unconstrained: a JSON schema made 44 of 65 answers invalid on a real project, while free decoding
        matched the evaluated model on 19 of 20 views. Answers are cached by model digest and prompt, so a rescan of
        unchanged code reuses them."""
        from app.backend.pipeline import llm_client, project_scan, reviewer
        from app.backend.pipeline.gateway import client as gateway
        from app.backend.pipeline.gateway.errors import GatewayError

        check_cancel(cancelled)
        if chat is not None:
            return self._model_review_files(workspace_root, cancelled, chat, progress)
        try:
            identity = reviewer.local_reviewer()
        except reviewer.ReviewStopped as error:
            raise ValueError(str(error)) from error
        tag = identity["tag"]
        answers = _AnswerCache(self.cache / "model_review" / "answers.jsonl", identity)
        with llm_client.local_client(timeout=600) as model:
            def ask(messages):
                cached = answers.get(messages)
                if cached is not None:
                    return cached
                try:
                    check_cancel(cancelled)
                    with gateway.inference_session("a model review scan"):
                        check_cancel(cancelled)
                        response = model.chat.completions.create(model=tag, messages=messages, temperature=0,
                                                                 max_tokens=reviewer.MAX_OUTPUT_TOKENS, timeout=600)
                except ScanCancelled as error:
                    raise project_scan.ReviewStopped("Model review cancelled.") from error
                except GatewayError as error:
                    raise project_scan.ReviewStopped(str(error)) from error
                except llm_client.request_error_type() as error:
                    raise project_scan.ReviewStopped("Ollama could not finish a request. Review coverage is partial.") from error
                text = response.choices[0].message.content or ""
                answers.put(messages, text)
                return text

            return {**self._model_review_files(workspace_root, cancelled, ask, progress), "model": identity}

    def _model_review_files(self, workspace_root: Path, cancelled, chat, progress) -> dict:
        from app.backend.pipeline import funcsplit, project_scan
        from app.backend.pipeline.ingest.codemine import language_of
        from . import workspace
        from .analysis import _regular

        root = Path(workspace_root).absolute()
        if not root.is_dir() or workspace.linked(root):
            raise ValueError("An existing regular workspace directory is required.")
        supported = funcsplit._TS.keys() | {"vue", "svelte"}
        files, skipped = [], []
        for path in sorted(workspace.files(root)):
            check_cancel(cancelled)
            language = language_of(path)
            if language not in supported:
                if language:
                    skipped.append({"path": path, "reason": "unsupported_language"})
                continue
            try:
                source = workspace.bound(root, path)
                before = _regular(source)
                if before.st_size > funcsplit.MAX_SOURCE_BYTES:
                    skipped.append({"path": path, "reason": "source_too_large"})
                    continue
                with source.open("rb") as stream:
                    if not os.path.samestat(before, os.fstat(stream.fileno())):
                        raise ValueError("Source changed during scan")
                    raw = stream.read(funcsplit.MAX_SOURCE_BYTES + 1)
                if len(raw) > funcsplit.MAX_SOURCE_BYTES or b"\0" in raw:
                    raise ValueError("Unsupported source file")
                text = raw.decode("utf-8-sig")
            except (OSError, ValueError):
                skipped.append({"path": path, "reason": "unreadable_source"})
                continue
            files.append({"path": path, "text": text, "language": language, "sha256": hashlib.sha256(raw).hexdigest()})

        def guarded(messages):
            try:
                check_cancel(cancelled)
            except ScanCancelled as error:
                raise project_scan.ReviewStopped("Model review cancelled.") from error
            return chat(messages)

        result = project_scan.scan(files, guarded, progress=progress)
        result["skipped"] = skipped + result["skipped"]
        if skipped:
            result["status"] = "partial"
        return result

    def _prepare(self, work, source, environment=None):
        for root in [work, source] + ([environment] if environment else []):
            root = Path(root).resolve()
            if self.cache.resolve().is_relative_to(root) or root.is_relative_to(self.cache.resolve()):
                raise ValueError("Scan vault must be outside workspace, source, and selected environment.")
        safe_path(self.cache, self.cache.anchor)
        self.cache.mkdir(parents=True, exist_ok=True)
        for child in ("snapshots", "sources"):
            safe_path(self.cache / child, self.cache)

    def _lookup(self, url, payload=None, cancelled=None, validator=None, load=None):
        check_cancel(cancelled); validate_url(url)
        key = hashlib.sha256((url + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
        path = self.cache / "snapshots" / (key + ".json")
        safe_path(path, self.cache)
        try:
            result = (load or self.fetch_json)(url, payload)
            check_cancel(cancelled)
            if not isinstance(result, dict): raise ValueError("Registry response is not a JSON object.")
            if validator: validator(result)
            entry = {"fetched_at": time.time(), "source": url, "payload": payload, "data": result}
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
            temp.write_text(json.dumps(entry), encoding="utf-8"); temp.replace(path)
            return result, {"status": "fresh", "source": url, "fetched_at": entry["fetched_at"], "age_seconds": 0}
        except ScanCancelled: raise
        except Exception as exc:
            try:
                safe_path(path, self.cache)
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached["source"] != url or cached["payload"] != payload: raise ValueError("Snapshot identity mismatch.")
                if validator: validator(cached["data"])
                return cached["data"], {"status": "offline_snapshot", "source": url, "fetched_at": cached["fetched_at"], "age_seconds": max(0, time.time() - cached["fetched_at"]), "error": str(exc)[:300]}
            except (OSError, ValueError, KeyError, TypeError):
                return None, {"status": "failed", "source": url, "error": str(exc)[:300], "age_seconds": None}

    def advisory(self, row, version, cancelled):
        if not ordering.key(row["ecosystem"], version).valid:
            return {"status": "inconclusive", "lookup": {"status": "not_queried", "reason": "Exact version is not valid for its ecosystem."}, "advisories": [], "withdrawn": []}
        if any(o["version"] == version and not public_observation(row["ecosystem"], o) for o in row.get("observations", [])):
            return {"status": "inconclusive", "lookup": {"status": "not_queried", "reason": "The recorded dependency origin is outside the public registry."}, "advisories": [], "withdrawn": []}
        version_query = "v" + ordering.normalize("Go", version) if row["ecosystem"] == "Go" else version
        payload = {"package": {"ecosystem": row["ecosystem"], "name": row["name"]}, "version": version_query}
        records, pages = [], []
        for _ in range(20):
            data, lookup = self._lookup("https://api.osv.dev/v1/query", payload, cancelled, lambda d: _validate_osv(d))
            pages.append(lookup)
            if data is None: return {"status": "inconclusive", "lookup": lookup, "advisories": [], "withdrawn": []}
            records.extend(data.get("vulns", []))
            if not data.get("next_page_token"): break
            payload = {**payload, "page_token": data["next_page_token"]}
        else: return {"status": "inconclusive", "lookup": pages[-1], "reason": "Advisory pagination limit reached.", "advisories": [], "withdrawn": []}
        lookup = next((p for p in pages if p["status"] != "fresh"), pages[0])
        active, withdrawn, unknown = [], [], False
        for record in records:
            check_cancel(cancelled)
            if not record.get("affected") and not record.get("withdrawn"):
                requested_id = record["id"]
                def validate_detail(data):
                    if data.get("id") != requested_id or not (data.get("withdrawn") or (isinstance(data.get("affected"), list) and data["affected"] and all(isinstance(e, dict) for e in data["affected"]))):
                        raise ValueError("Advisory detail is incomplete or its identity does not match.")
                record, detail = self._lookup("https://api.osv.dev/v1/vulns/" + quote(requested_id, safe=""), cancelled=cancelled, validator=validate_detail)
                if not record or detail["status"] == "failed": unknown = True; continue
                if not _fresh(detail): lookup = detail
            if record.get("withdrawn"):
                withdrawn.append({"id": record["id"], "withdrawn": record["withdrawn"], "aliases": record.get("aliases", [])}); continue
            matches = [_match_entry(row["ecosystem"], row["name"], version, e) for e in record.get("affected", [])]
            if True not in matches:
                # An exact-version query returned a record we could not corroborate.
                unknown = True
                continue
            ids = {record["id"], *record.get("aliases", [])}
            entry = {"id": record["id"], "ids": sorted(ids), "aliases": record.get("aliases", []), "summary": record.get("summary"), "details": record.get("details"), "severity": record.get("severity", []), "affected": record["affected"], "references": record.get("references", []), "modified": record.get("modified"), "cve_details": []}
            active.append(entry)
        # Merge connected alias sets, including aliases linked through a third record.
        merged = []
        for entry in active:
            overlap = [e for e in merged if set(e["ids"]) & set(entry["ids"])]
            for other in overlap:
                entry["ids"] = sorted(set(entry["ids"]) | set(other["ids"])); merged.remove(other)
            merged.append(entry)
        for entry in merged:
            for cve in [i for i in entry["ids"] if re.fullmatch(r"CVE-\d{4}-\d{4,}", i)][:5]:
                detail, provenance = self._lookup("https://services.nvd.nist.gov/rest/json/cves/2.0?cveId=" + cve, cancelled=cancelled)
                entry["cve_details"].append({"id": cve, "lookup": provenance, "data": detail})
        return {"status": "affected" if merged else ("inconclusive" if unknown or not _fresh(lookup) else "no_known_vulnerabilities"), "lookup": lookup, "advisories": merged, "withdrawn": withdrawn,
                "statement": "Exact package/version advisory matching; no proof of safety or reachability."}

    def _registry(self, row, cancelled):
        eco, name = row["ecosystem"], row.get("registry_name") or row["name"]
        url = scans_registry.registry_url(eco, name)
        if not url:
            return [], set(), {"status": "not_queried", "reason": "No public version registry is supported for this ecosystem."}

        def load(address, payload):
            raw = self.fetch_bytes(address) if eco in {"Go", "Maven"} else self.fetch_json(address, payload)
            return listing(raw)

        def listing(raw):
            published = scans_registry.published_versions(eco, name, raw)
            deprecated = [v for v, item in raw["versions"].items() if isinstance(item, dict) and item.get("deprecated")] if eco == "npm" else []
            if eco == "Go":
                check_cancel(cancelled)
                deprecated = scans_registry.go_retracted(name, published, self.fetch_bytes)
            return {"published": published, "deprecated": deprecated}

        def validate(data):
            release_data = listing(data) if "published" not in data and eco in {"PyPI", "npm"} else data
            for key in ("published", "deprecated"):
                value = release_data.get(key, [] if key == "deprecated" else None)
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValueError("Registry release listing is malformed.")

        data, lookup = self._lookup(url, cancelled=cancelled, validator=validate, load=load)
        if not data:
            return [], set(), lookup
        if "published" not in data:
            data = listing(data)
        releases = set()
        for raw_version in data["published"]:
            version = ordering.normalize(eco, raw_version)
            key = ordering.key(eco, version)
            if not key.valid or ordering.is_prerelease(eco, version): continue
            releases.add(version)
        deprecated = {ordering.normalize(eco, version) for version in data.get("deprecated", [])}
        return sorted(releases, key=lambda v: ordering.key(eco, v), reverse=True), deprecated, lookup

    def versions(self, workspace_root, source_root, cancelled, environment=None, progress=None):
        check_cancel(cancelled)
        if progress:
            progress({"phase": "inventory"})
        self._prepare(workspace_root, source_root, environment)
        result = inventory(workspace_root, source_root, environment, _cancelled=cancelled)
        summary = {"ecosystems": sorted({r["ecosystem"] for r in result["packages"]}), "affected_packages": 0,
                   "no_known_vulnerabilities": 0, "inconclusive_packages": 0, "recommendations": 0}
        result.update({"schema": "project-ide-versions/1", "generated_at": datetime.now(timezone.utc).isoformat(),
                       "analysis_only": True, "source": "OSV", "summary": summary})
        total = len(result["packages"])

        def update(phase, completed, **current):
            if progress:
                progress({"phase": phase, "completed": completed, "total": total,
                          "affected": summary["affected_packages"], "inconclusive": summary["inconclusive_packages"], **current})

        for index, row in enumerate(result["packages"]):
            check_cancel(cancelled)
            update("versions", index, package_id=row["id"], package_name=row["name"])
            exact = sorted({p["version"] for p in row["versions"]})
            row["checks"] = {v: self.advisory(row, v, cancelled) for v in exact}
            has_public = any(check["lookup"]["status"] != "not_queried" for check in row["checks"].values())
            releases, deprecated, lookup = self._registry(row, cancelled) if has_public else (
                [], set(), {"status": "not_queried", "reason": "No exact public dependency version was identified."})
            row["registry"] = lookup; row["latest_stable"] = releases[0] if releases else None
            latest = row["latest_stable"]
            row["latest_check"] = row["checks"].get(latest) or (self.advisory(row, latest, cancelled) if latest and exact else {"status": "inconclusive"})
            row["outdated"] = any(ordering.compare(row["ecosystem"], v, releases[0]) < 0 for v in row["resolved"]) if releases and row["resolved"] else None
            row["recommendation"] = None
            constraints = row["declared"] or [o["declared"] for o in row["observations"] if o["kind"] == "transitive_declared" and o["declared"]]
            current = [row["checks"][version] for version in row["resolved"]]
            if row["resolution"] == "minimum_declared":
                summary["inconclusive_packages"] += 1
            elif any(check["status"] == "affected" for check in current):
                summary["affected_packages"] += 1
            elif current and all(check["status"] == "no_known_vulnerabilities" for check in current):
                summary["no_known_vulnerabilities"] += 1
            else:
                summary["inconclusive_packages"] += 1
            if row["resolution"] != "minimum_declared" and _fresh(lookup) and any(
                check["status"] == "affected" and _fresh(check["lookup"]) for check in current
            ):
                row["recommendation"] = self._recommend(row, [v for v in releases if v not in deprecated], constraints, cancelled)
                summary["recommendations"] += bool(row["recommendation"])
            row["recommendation_limits"] = "At most 30 newer releases. Candidates require a fresh registry listing and advisory check."
        check_cancel(cancelled)
        update("finalizing", total)
        return result

    def _recommend(self, row, releases, constraints, cancelled):
        eco = row["ecosystem"]
        newer = [release for release in releases if all(ordering.compare(eco, release, v) > 0 for v in row["resolved"])]
        options = []
        for version in newer:
            matches = [_satisfies(eco, version, constraint) for constraint in constraints]
            compatibility = ("requires_constraint_change" if False in matches else
                             "declared_ranges_satisfied" if matches and all(match is True for match in matches) else
                             "needs_compatibility_review")
            options.append((version, compatibility))
        ranks = {"declared_ranges_satisfied": 0, "needs_compatibility_review": 1, "requires_constraint_change": 2}
        options.sort(key=lambda item: ranks[item[1]])
        for candidate, compatibility in options[:30]:
            checked = row["checks"].get(candidate) or (row["latest_check"] if candidate == row["latest_stable"] else
                                                       self.advisory(row, candidate, cancelled))
            row["checks"][candidate] = checked
            if checked["status"] == "no_known_vulnerabilities" and _fresh(checked["lookup"]):
                return {"version": candidate, "check": checked, "compatibility": compatibility,
                        "limitations": "No known advisory matched this published release. Validate runtime, API and platform compatibility before updating."}
        return None

    def source(self, workspace_root, source_root, selected_package_ids, cancelled, environment=None, progress=None):
        from .scans_source import review_sources
        check_cancel(cancelled)
        current = inventory(workspace_root, source_root, environment, _cancelled=cancelled)
        available = {p["id"]: p for p in current["packages"]}
        if not selected_package_ids or len(set(selected_package_ids)) != len(selected_package_ids) or any(i not in available for i in selected_package_ids):
            raise ValueError("Select distinct package IDs from the current inventory before source review.")
        self._prepare(workspace_root, source_root, environment)
        return review_sources(self, [available[i] for i in selected_package_ids], current, cancelled, progress)


def _validate_osv(data):
    if "vulns" in data and (not isinstance(data["vulns"], list) or any(not isinstance(v, dict) or not v.get("id") for v in data["vulns"])):
        raise ValueError("Malformed OSV response.")
    if data.get("error") or data.get("code"): raise ValueError("OSV returned an error response.")
    if data and not ({"vulns", "next_page_token"} & data.keys()):
        raise ValueError("OSV returned an unrecognized response.")
    if "next_page_token" in data and not isinstance(data["next_page_token"], str):
        raise ValueError("OSV pagination token is malformed.")


def extract_archive(data, destination, kind, cancelled):
    from .scans_source import extract_archive as extract
    return extract(data, destination, kind, cancelled)
