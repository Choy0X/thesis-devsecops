"""Workspace-bound code and assessment tools, shared by the IDE and MCP transport."""
from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path
import re
import uuid

from training.finetune import contracts
from . import workspace
from .store import stamp


def specification(name, description, properties, required=()):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}


TEXT = {"type": "string"}
ASSESSMENT = {"type": "object", "properties": {
    "status": {"type": "string", "enum": list(contracts.STATUSES)},
    "findings": {"type": "array", "items": {"type": "object", "properties": {k: TEXT for k in contracts.FINDING_KEYS}, "required": ["summary"], "additionalProperties": False}},
    "evidence": {"type": "array", "items": {"type": "object", "properties": {"quote": TEXT, "reason": TEXT}, "required": ["quote", "reason"], "additionalProperties": False}},
    "assumptions": {"type": "array", "items": TEXT}, "missing_context": {"type": "array", "items": TEXT},
    "validation_state": {"type": "string", "enum": ["not_validated"]},
    "patch": {"type": ["string", "null"]}, "behavioral_intent": {"type": ["string", "null"]}},
    "required": list(contracts.BASE_KEYS), "additionalProperties": False}
DEFINITIONS = [
    specification("repository_overview", "Inspect saved project structure, manifests, language counts and declared test commands. No execution.", {}),
    specification("symbol_context", "Read a saved file and locate a symbol with a source hash. Python definitions use AST; other languages use bounded text matches.", {"path": TEXT, "symbol": TEXT}, ("path", "symbol")),
    specification("record_assessment", "Validate and record a supported assessment with exact source hashes. Model validation_state must be not_validated. Quotes must occur in the selected saved files.",
        {"task": {"type": "string", "enum": [k for k in contracts.names() if k != "general_code_replay"]},
         "response": {"type": "object", "description": "Training response contract: status, findings, evidence, assumptions, missing_context, validation_state=not_validated; repair adds patch and behavioral_intent. Tool validates every field."},
         "paths": {"type": "array", "items": TEXT, "maxItems": 20}}, ("task", "response", "paths")),
    specification("dependency_inventory", "Read exact installed/locked/declared dependency metadata without importing packages.", {}),
    specification("security_knowledge", "Retrieve local primary advisory, CWE association and fix-reference evidence. Missing/stale sources remain explicit.", {"query": TEXT}, ("query",)),
    specification("source_candidates", "Run bounded static rules on application-owned saved source. Returns candidates and coverage, not proof of safety.", {}),
    specification("selected_dependency_source", "Read a user-selected dependency source file with its exact-version snapshot hash. Analysis only.", {"package_id": TEXT, "path": TEXT}, ("package_id", "path")),
]


class ToolService:
    def __init__(self, root: Path, receipts: Path, *, source: Path | None = None, cache: Path | None = None, knowledge: Path | None = None):
        self.root, self.receipts = Path(root).resolve(), Path(receipts).resolve()
        self.source, self.cache, self.knowledge = Path(source or root).resolve(), cache, knowledge
        if not self.root.is_dir() or self.receipts.is_relative_to(self.root):
            raise ValueError("Tools require an existing workspace and a separate receipt directory.")

    def call(self, name: str, arguments: dict) -> dict:
        if name not in {item["name"] for item in DEFINITIONS}:
            raise ValueError("The requested workspace tool is unavailable.")
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(next(item["inputSchema"] for item in DEFINITIONS if item["name"] == name))
        problems = list(validator.iter_errors(arguments))
        if problems:
            raise ValueError("Invalid tool arguments: " + problems[0].message)
        result = getattr(self, "_" + name)(**arguments)
        return {"workspace": str(self.root), "timestamp": stamp(), **result}

    def _repository_overview(self):
        inventory = workspace.files(self.root)
        manifests, commands = [], []
        names = {"pyproject.toml", "package.json", "Cargo.toml", "go.mod", "requirements.txt", "pom.xml", "composer.json", "Gemfile"}
        for name, path in inventory.items():
            if path.name in names:
                manifests.append({"path": name, "sha256": workspace.digest(path)})
                if path.name == "package.json":
                    try:
                        scripts = json.loads(path.read_text(encoding="utf-8")).get("scripts", {})
                        commands.extend({"manifest": name, "name": key, "command": value, "executed": False} for key, value in scripts.items() if "test" in key)
                    except (ValueError, UnicodeError):
                        pass
        return {"file_count": len(inventory), "languages": dict(Counter(p.suffix for p in inventory.values())),
                "manifests": manifests, "declared_test_commands": commands,
                "capabilities": {"container_security_verification": {"available": False,
                    "reason": "The Projects MCP bridge does not yet expose the existing Sandbox verification service. Do not run intrusive probes with native shell tools."}},
                "coverage": {"excluded_directories": sorted(workspace.EXCLUDED_DIRS), "environment_discovery": "separate dependency scan required"}}

    def _dependency_inventory(self):
        from .scans_inventory import inventory
        return inventory(self.root, self.source)

    def _security_knowledge(self, query):
        from .knowledge import LocalKnowledge
        if not query.strip() or len(query) > 300:
            raise ValueError("Use a knowledge query of 1 to 300 characters.")
        return LocalKnowledge(self.root, cache_root=self.cache, interim_root=self.knowledge).retrieve(query)

    def _source_candidates(self):
        from .analysis import analyze
        return analyze(self.root, None)

    def _selected_dependency_source(self, package_id, path):
        manifest = self.receipts / "selected-sources.json"
        if not manifest.is_file():
            raise ValueError("Select packages and run dependency source review first.")
        document = json.loads(manifest.read_text(encoding="utf-8"))
        for package in document.get("packages", []):
            if package.get("package_id") != package_id and package.get("id") != package_id:
                continue
            row = next((f for f in package.get("files", []) if f["path"] == path), None)
            if row:
                candidate = Path(row["source_path"])
                if self.cache is None or not candidate.resolve().is_relative_to(Path(self.cache).resolve()) or workspace.linked(candidate):
                    raise ValueError("Selected source is outside the app-owned source vault.")
                from .scans_inventory import safe_path
                safe_path(candidate, Path(self.cache))
                if candidate.stat().st_size > 128000:
                    raise ValueError("Selected source exceeds the 128000-byte context limit.")
                raw = candidate.read_bytes()
                if len(raw) > 128000 or workspace.digest(candidate) != row["sha256"]:
                    raise ValueError("Selected source changed or exceeds the 128000-byte context limit.")
                return {"package_id": package_id, "path": path, "version": package["version"], "sha256": row["sha256"],
                        "origin": package["origin"], "text": raw.decode("utf-8"), "read_only": True,
                        "assessment_path": "dependency:" + package_id + "#" + path}
        raise ValueError("The file is not part of the user-selected dependency source inventory.")

    def _symbol_context(self, path: str, symbol: str):
        if not symbol or len(symbol) > 200:
            raise ValueError("A bounded symbol name is required.")
        saved = workspace.read({"root": self.root}, path)
        lines = saved["text"].splitlines()
        definitions = []
        if Path(path).suffix == ".py":
            try:
                for node in ast.walk(ast.parse(saved["text"])):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol:
                        definitions.append({"line": node.lineno, "end_line": node.end_lineno, "text": "\n".join(lines[node.lineno - 1:node.end_lineno])})
            except SyntaxError as exc:
                raise ValueError("Python syntax prevents symbol parsing: " + str(exc)) from exc
        matches = [{"line": i, "text": line} for i, line in enumerate(lines, 1) if re.search(r"\b" + re.escape(symbol) + r"\b", line)]
        return {"path": path, "sha256": saved["version"], "definitions": definitions,
                "matches": matches[:100], "coverage": "Python AST and text matches" if path.endswith(".py") else "Text matches only; semantic definitions unavailable"}

    def _record_assessment(self, task: str, response: dict, paths: list[str]):
        problems = contracts.validate_response(task, response)
        if problems:
            raise ValueError("; ".join(problems))
        saved = {}
        packages = {}
        for path in paths:
            if path.startswith("dependency:") and "#" in path:
                package, relative = path[len("dependency:"):].split("#", 1)
                item = self._selected_dependency_source(package, relative)
                saved[path] = {"text": item["text"], "version": item["sha256"]}
                packages[package] = {"version": item["version"], "origin": item["origin"]}
            else:
                saved[path] = workspace.read({"root": self.root}, path)
        for evidence in response["evidence"]:
            if not any(evidence["quote"] in item["text"] for item in saved.values()):
                raise ValueError("An evidence quote does not occur in the selected saved source files.")
        revision = None
        manifest = self.receipts.parent / "workspace.json"
        if manifest.is_file():
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            if Path(metadata.get("root", "")).resolve() == self.root:
                revision = metadata.get("revision")
        value = {"id": uuid.uuid4().hex, "task": task, "assessment": contracts.normalize(task, response),
                 "workspace": str(self.root), "timestamp": stamp(), "files": {path: item["version"] for path, item in saved.items()},
                 "packages": packages,
                 "source_revision": revision, "coverage": {"files": list(saved), "execution_verified": False}}
        self.receipts.mkdir(parents=True, exist_ok=True)
        workspace._write(self.receipts / (value["id"] + ".json"), json.dumps(value, ensure_ascii=True, indent=2).encode())
        return value
