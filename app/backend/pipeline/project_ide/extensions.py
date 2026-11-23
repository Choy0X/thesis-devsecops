"""App-owned extension snapshots and explicit, per-project execution consent.

Registration only reads files and writes this store. It never imports code, starts
MCP processes, contacts a registry, or installs packages. ``effective`` is private
runtime configuration: unlike inspection metadata, it can contain credentials.
The runtime must append these fragments to its app-owned policy, never replace it.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.parse import urlsplit
import uuid

from filelock import FileLock
import yaml


class ExtensionError(ValueError):
    """An extension is invalid, changed, unsupported, or not approved."""


class ExtensionStore:
    """Durable local extension registry. Public methods return secret-free copies.

    Local plugin packaging is deliberately limited to one self-contained UTF-8
    JavaScript/TypeScript file. Assets, relative imports and package dependencies
    require a separately packaged, exact-version npm plugin. Skill imports copy
    SKILL.md only; they do not import sibling scripts/assets or grant permissions.
    A pinned npm version identifies a registry release, not verified downloaded
    bytes. No registry access or plugin execution occurs in this class.
    """

    def __init__(self, root: Path, *, bundled_skill_names=()):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._path = self.root / "extensions.json"
        self._lock = FileLock(str(self.root / ".extensions.lock"), timeout=30)
        bundled = Path(__file__).parent / "skills"
        self._reserved_skills = {item.name.casefold() for item in bundled.iterdir() if item.is_dir()} if bundled.is_dir() else set()
        self._reserved_skills.update(str(name).casefold() for name in bundled_skill_names)

    @staticmethod
    def _json(value):
        try:
            return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ExtensionError("Extension configuration must be plain JSON data.") from exc

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", name) or len(name) > 64:
            raise ExtensionError("Extension names must use lowercase letters, numbers and single hyphens (at most 64 characters).")
        if name in {"con", "prn", "aux", "nul", "boundary", "projects"} or re.fullmatch(r"(?:com|lpt)[1-9]", name):
            raise ExtensionError("That extension name is reserved by the application or Windows.")
        return name

    @staticmethod
    def _plain_file(path: Path, limit: int) -> bytes:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ExtensionError("Extension sources must be regular files, not links or reparse points.")
            if info.st_size > limit:
                raise ExtensionError("Extension source exceeds its size limit.")
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
            if len(data) > limit:
                raise ExtensionError("Extension source exceeds its size limit.")
            return data
        except OSError as exc:
            raise ExtensionError("Extension source is missing or unreadable.") from exc

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _owned(self, path: Path) -> Path:
        path = Path(path).absolute()
        try:
            resolved = path.resolve()
            resolved.relative_to(self.root)
            cursor = path
            while cursor != self.root:
                if cursor.is_symlink() or (cursor.exists() and getattr(cursor.lstat(), "st_file_attributes", 0) & 0x400):
                    raise ExtensionError("Owned extension paths cannot contain links or reparse points.")
                cursor = cursor.parent
        except (ValueError, OSError) as exc:
            raise ExtensionError("Extension snapshot escaped the owned store.") from exc
        return resolved

    def _atomic(self, path: Path, data: bytes):
        path = self._owned(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".extension-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    @contextmanager
    def _transaction(self):
        with self._lock:
            if self._path.exists():
                try:
                    state = json.loads(self._plain_file(self._path, 8 * 1024 * 1024))
                except (ValueError, UnicodeError) as exc:
                    raise ExtensionError("Extension registry is malformed; repair it before continuing.") from exc
                if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("records"), dict):
                    raise ExtensionError("Unsupported extension registry schema.")
            else:
                state = {"schema_version": 1, "records": {}}
            before = self._json(state)
            yield state["records"]
            after = self._json(state)
            if len(after.encode("utf-8")) > 8 * 1024 * 1024:
                raise ExtensionError("Extension registry exceeds its local size budget.")
            if after != before or not self._path.exists():
                self._atomic(self._path, after.encode("utf-8"))

    def _hash(self, record):
        payload = {key: record.get(key) for key in ("kind", "name", "source", "source_kind", "config", "file_hash", "files", "package_version")}
        return self._digest(self._json(payload).encode("utf-8"))

    def _register(self, kind, name, source, source_kind, config, *, content=None, metadata=None, package_version=None, files=None, filename=None):
        name = self._name(name)
        if kind == "skill" and name.casefold() in self._reserved_skills:
            raise ExtensionError("That skill name collides with an app-owned bundled skill.")
        with self._transaction() as records:
            existing = next((item for item in records.values() if item["kind"] == kind and item["name"] == name), None)
            identity = existing["id"] if existing else uuid.uuid4().hex
            version = existing["version"] + 1 if existing else 1
            record = {"id": identity, "kind": kind, "name": name, "version": version,
                      "source": str(source), "source_kind": source_kind, "config": deepcopy(config),
                      "file_hash": self._digest(content) if content is not None else None,
                      "files": files or [], "package_version": package_version,
                      "metadata": metadata or {}, "trusted": False, "trusted_version": None,
                      "trusted_hash": None, "enabled_projects": [], "errors": [], "snapshot": None,
                      "capabilities": {"skill": ["instructions"], "mcp": ["external_tools"], "plugin": ["runtime_code"]}[kind]}
            if content is not None:
                snapshot = self.root / "snapshots" / identity / str(version)
                if kind == "skill":
                    snapshot = snapshot / name
                snapshot = snapshot / filename
                self._atomic(snapshot, content)
                record["snapshot"] = str(snapshot)
            record["hash"] = self._hash(record)
            records[identity] = record
            return self._public(record)

    def import_skill(self, path: Path) -> dict:
        source = Path(path).absolute()
        if source.name != "SKILL.md":
            raise ExtensionError("Select a local SKILL.md file.")
        content = self._plain_file(source, 128 * 1024)
        try:
            text = content.decode("utf-8-sig")
            match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
            if not match or len(match[1]) > 16 * 1024:
                raise ExtensionError("SKILL.md requires bounded YAML frontmatter.")
            if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)) for token in yaml.scan(match[1])):
                raise ExtensionError("Skill metadata cannot contain YAML tags, anchors or aliases.")
            parsed = yaml.safe_load(match[1])
            if not isinstance(parsed, dict):
                raise ExtensionError("Skill metadata must be a mapping.")
            name, description = parsed.get("name"), parsed.get("description")
            self._name(name)
            if not isinstance(description, str) or not description.strip() or len(description) > 1024:
                raise ExtensionError("Skill description must be nonempty text of at most 1024 characters.")
            metadata = {"name": name, "description": description, "packaging": "SKILL.md only; sibling assets and scripts are not imported"}
            for key in ("version", "license", "compatibility"):
                if key in parsed:
                    if not isinstance(parsed[key], str) or len(parsed[key]) > 1024:
                        raise ExtensionError("Optional skill metadata must be bounded text.")
                    metadata[key] = parsed[key]
        except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
            raise ExtensionError("Skill frontmatter must be safe UTF-8 YAML.") from exc
        return self._register("skill", name, source, "local_file", {}, content=content, metadata=metadata, filename="SKILL.md")

    @staticmethod
    def _strings(mapping):
        return isinstance(mapping, dict) and all(isinstance(k, str) and isinstance(v, str) and "\0" not in k + v for k, v in mapping.items())

    def _mcp(self, config):
        if not isinstance(config, dict) or len(self._json(config)) > 64 * 1024:
            raise ExtensionError("MCP configuration must be a bounded native config object.")
        config = deepcopy(config)
        kind = config.get("type")
        allowed = {"type", "enabled", "timeout"} | ({"command", "environment"} if kind == "local" else {"url", "headers", "oauth"})
        if kind not in ("local", "remote") or set(config) - allowed:
            raise ExtensionError("Only native local/remote MCP fields are allowed; application policy cannot be overridden.")
        if "enabled" in config and not isinstance(config["enabled"], bool):
            raise ExtensionError("MCP enabled must be a boolean; use project enablement after registration.")
        config.pop("enabled", None)
        timeout = config.get("timeout", 5000)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300000:
            raise ExtensionError("MCP timeout must be an integer from 1 to 300000 milliseconds.")
        files = []
        if kind == "local":
            command = config.get("command")
            if not isinstance(command, list) or not command or len(command) > 128 or any(not isinstance(x, str) or not x or "\0" in x or "{env:" in x or "{file:" in x for x in command):
                raise ExtensionError("Local MCP command must be a nonempty string array with literal arguments.")
            if not self._strings(config.get("environment", {})):
                raise ExtensionError("MCP environment must map string keys to string values.")
            executable = shutil.which(command[0])
            if not executable:
                raise ExtensionError("Local MCP executable was not found. Install it separately and register its explicit command again.")
            command[0] = str(Path(executable).absolute())
            literal_next = False
            for argument in command:
                if literal_next:
                    literal_next = False
                    continue
                if argument in ("-c", "-e", "--eval"):
                    literal_next = True
                    continue
                path = Path(argument)
                if path.is_absolute() and path.is_file():
                    content = self._plain_file(path, 512 * 1024 * 1024)
                    files.append({"path": str(path.absolute()), "hash": self._digest(content)})
                elif ("/" in argument or "\\" in argument or path.suffix.lower() in {".py", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".cmd", ".bat", ".exe", ".sh"}) and not path.is_absolute() and not argument.startswith("-") and "://" not in argument:
                    raise ExtensionError("Local MCP file arguments must be absolute paths; relative packaging is unsupported.")
        else:
            url = config.get("url")
            try:
                parsed = urlsplit(url) if isinstance(url, str) else None
                if parsed is None or parsed.scheme not in ("http", "https") or not parsed.hostname or any(c.isspace() for c in url) or "{env:" in url or "{file:" in url:
                    raise ExtensionError("Remote MCP requires a literal HTTP or HTTPS address.")
                parsed.port
            except ValueError as exc:
                raise ExtensionError("Remote MCP address is malformed.") from exc
            if not self._strings(config.get("headers", {})):
                raise ExtensionError("MCP headers must map string keys to string values.")
            oauth = config.get("oauth", False)
            if oauth is not False and (not isinstance(oauth, dict) or set(oauth) - {"clientId", "clientSecret", "scope"} or not self._strings(oauth)):
                raise ExtensionError("MCP oauth must be false or a native OAuth configuration.")
        return config, files

    def register_mcp(self, name: str, config: dict) -> dict:
        self._name(name)
        native, files = self._mcp(config)
        source = native.get("url") or native["command"][0]
        return self._register("mcp", name, source, native["type"], native, files=files)

    def register_plugin(self, name: str, source: str | Path) -> dict:
        self._name(name)
        value = str(source)
        # Only exact registry versions; no implicit latest, URLs, git or ranges.
        match = re.fullmatch(r"((?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*)@((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)", value)
        if match:
            return self._register("plugin", name, value, "npm", {}, package_version=match[2])
        path = Path(value).absolute()
        if path.suffix.lower() not in (".mjs", ".js", ".ts") or not path.is_file():
            raise ExtensionError("Plugin source must be a local JavaScript/TypeScript file or an exact pinned npm package version.")
        content = self._plain_file(path, 512 * 1024)
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError as exc:
            raise ExtensionError("Local plugins must be UTF-8 source files.") from exc
        if re.search(r"\b(?:import|require)\b", text):
            raise ExtensionError("Only self-contained local plugins are supported; imports, dependencies and asset packaging require a pinned npm package.")
        return self._register("plugin", name, path, "local_file", {}, content=content, filename="plugin" + path.suffix.lower(), metadata={"packaging": "self-contained single file; external assets are unsupported"})

    def _record(self, records, identity):
        try:
            return records[identity]
        except (KeyError, TypeError) as exc:
            raise ExtensionError("The requested extension does not exist.") from exc

    def _refresh(self, record):
        errors = []
        try:
            if record["kind"] == "skill" and record["name"].casefold() in self._reserved_skills:
                errors.append("Extension skill name collides with a bundled skill; import it under a different name.")
            if self._hash(record) != record["hash"]:
                errors.append("Extension configuration changed; reimport and trust the inspected version.")
            if record["source_kind"] == "local_file":
                if self._digest(self._plain_file(Path(record["source"]), 512 * 1024)) != record["file_hash"]:
                    errors.append("Extension source changed; reimport and trust the inspected version.")
                snapshot = self._owned(Path(record["snapshot"]))
                if self._digest(self._plain_file(snapshot, 512 * 1024)) != record["file_hash"]:
                    errors.append("Extension snapshot changed; reimport before enabling it.")
            for item in record["files"]:
                if self._digest(self._plain_file(Path(item["path"]), 512 * 1024 * 1024)) != item["hash"]:
                    errors.append("Local MCP source changed; register and trust it again.")
                    break
        except (ExtensionError, OSError):
            errors.append("Extension source or snapshot is missing, unreadable or unsafe; reimport it.")
        if errors:
            record["trusted"] = False
            record["trusted_hash"] = None
            record["trusted_version"] = None
            record["errors"] = errors
        # A detected change revokes trust permanently, even if bytes are restored.
        return record

    def _public(self, record):
        result = deepcopy(record)
        result.pop("files", None)
        native = result["config"]
        if record["kind"] == "mcp":
            if record["source_kind"] == "remote":
                parsed = urlsplit(record["source"])
                address = f"{parsed.scheme}://{parsed.hostname}/[redacted]"
                result["source"] = address
                native["url"] = address
                if "headers" in native:
                    native["headers"] = {key: "[redacted]" for key in native["headers"]}
                if isinstance(native.get("oauth"), dict):
                    native["oauth"] = {key: "[redacted]" for key in native["oauth"]}
            else:
                native["command"] = native["command"][:1] + ["[redacted]" for _ in native["command"][1:]]
                if "environment" in native:
                    native["environment"] = {key: "[redacted]" for key in native["environment"]}
        return result

    def inspect(self, identity: str) -> dict:
        with self._transaction() as records:
            return self._public(self._refresh(self._record(records, identity)))

    get = inspect

    def list(self) -> list[dict]:
        with self._transaction() as records:
            return [self._public(self._refresh(record)) for record in sorted(records.values(), key=lambda item: (item["kind"], item["name"]))]

    def trust(self, identity: str, expected_version: int, expected_hash: str) -> dict:
        # Refresh in its own transaction so revocation persists even on rejection.
        self.inspect(identity)
        with self._transaction() as records:
            record = self._refresh(self._record(records, identity))
            if record["version"] != expected_version or record["hash"] != expected_hash or record["errors"]:
                raise ExtensionError("Extension version/hash changed or has load errors; inspect and reimport before trusting it.")
            record.update(trusted=True, trusted_version=record["version"], trusted_hash=record["hash"])
            return self._public(record)

    def enable(self, project_id: str, identity: str, enabled: bool) -> dict:
        if not isinstance(project_id, str) or not project_id.strip() or len(project_id) > 128 or not isinstance(enabled, bool):
            raise ExtensionError("A project identity and boolean enabled value are required.")
        self.inspect(identity)
        with self._transaction() as records:
            record = self._refresh(self._record(records, identity))
            if enabled and not self._trusted(record):
                raise ExtensionError("Explicit trust in the current extension version/hash is required before enabling it.")
            projects = set(record["enabled_projects"])
            if enabled:
                projects.add(project_id)
            else:
                projects.discard(project_id)
            record["enabled_projects"] = sorted(projects)
            return self._public(record)

    @staticmethod
    def _trusted(record):
        return record["trusted"] and not record["errors"] and record["trusted_version"] == record["version"] and record["trusted_hash"] == record["hash"]

    def effective(self, project_id: str) -> dict:
        """Return private native fragments plus public provenance and load errors.

        Call only between turns and freeze the returned provenance for that turn.
        The caller must preserve app-owned boundary plugin, skills, MCP and policy.
        """
        result = {"config": {}, "provenance": [], "errors": []}
        with self._transaction() as records:
            for record in sorted(records.values(), key=lambda item: (item["kind"], item["name"])):
                self._refresh(record)
                if project_id not in record["enabled_projects"]:
                    continue
                if not self._trusted(record):
                    result["errors"].append({"id": record["id"], "name": record["name"], "errors": record["errors"] or ["The current extension version requires explicit trust."]})
                    continue
                config = result["config"]
                if record["kind"] == "mcp":
                    config.setdefault("mcp", {})[record["name"]] = {**deepcopy(record["config"]), "enabled": True}
                elif record["kind"] == "plugin":
                    location = Path(record["snapshot"]).as_uri() if record["snapshot"] else record["source"]
                    config.setdefault("plugin", []).append(location)
                else:
                    directory = str(Path(record["snapshot"]).parent.parent)
                    config.setdefault("skills", {"paths": []})["paths"].append(directory)
                public = self._public(record)
                result["provenance"].append({key: public[key] for key in ("id", "kind", "name", "source", "source_kind", "version", "hash", "package_version", "capabilities")})
        return result
