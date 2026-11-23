"""Codebase scanner (Chapter 4.4).

Audits a project's own source for vulnerability patterns that no CVE database
would surface: command / SQL injection, insecure deserialisation, unsafe data
handling, authentication-bypass smells, and hard-coded secrets. This is the
Python replacement for the archived Rust scanners; the rule table is explicit and
every finding records the rule that matched so it is explainable (Chapter 9).

Each finding is emitted in the shape the orchestrator consumes:
{rule, kind: "codebase", file, line, severity_vector|osv_severity, summary}.
"""
import re
import json
import pathlib
import argparse

# (rule id, compiled regex, qualitative severity, human summary, CWE)
RULES = [
    ("py-exec-eval", re.compile(r"\b(eval|exec)\s*\("), "HIGH",
     "Use of eval/exec on dynamic input (code injection)", "CWE-95"),
    ("py-os-system", re.compile(r"\bos\.system\s*\(|\bsubprocess\.(call|run|Popen)\([^)]*shell\s*=\s*True"),
     "HIGH", "Shell command execution (command injection)", "CWE-78"),
    ("py-pickle", re.compile(r"\b(pickle|cPickle)\.loads?\s*\(|\byaml\.load\s*\((?![^)]*Loader)"),
     "HIGH", "Insecure deserialisation", "CWE-502"),
    ("py-sql-fstring", re.compile(r"(execute|executemany)\s*\(\s*f[\"'].*\{.*\}.*[\"']", re.I),
     "CRITICAL", "SQL built from an f-string (SQL injection)", "CWE-89"),
    ("py-sql-concat", re.compile(r"(SELECT|INSERT|UPDATE|DELETE)\b.*[\"']\s*\+\s*\w+", re.I),
     "HIGH", "SQL built by string concatenation (SQL injection)", "CWE-89"),
    ("py-weak-hash", re.compile(r"hashlib\.(md5|sha1)\s*\("), "MODERATE",
     "Weak hash for security use", "CWE-327"),
    ("py-verify-false", re.compile(r"verify\s*=\s*False|ssl\._create_unverified"), "HIGH",
     "TLS verification disabled", "CWE-295"),
    ("py-debug-true", re.compile(r"\bdebug\s*=\s*True", re.I), "MODERATE",
     "Debug mode enabled (information exposure / auth weakening)", "CWE-489"),
    ("secret-key", re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*[\"'][A-Za-z0-9/+_\-]{16,}[\"']"),
     "HIGH", "Hard-coded secret", "CWE-798"),
    ("py-tmp-insecure", re.compile(r"\/tmp\/[A-Za-z0-9_.\-]+|tempfile\.mktemp\s*\("), "MODERATE",
     "Insecure temporary file handling", "CWE-377"),
]

SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "build", "__pycache__", "archive", ".chrome-profile"}
EXTS = {".py", ".pyw"}


def scan(project: str = ".", output=None):
    root = pathlib.Path(project)
    findings = []
    files = 0
    for path in root.rglob("*"):
        if path.is_dir() or path.suffix not in EXTS:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        files += 1
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except Exception:  # ignored: an unreadable file is skipped
            continue
        for i, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            for rid, rx, sev, summary, cwe in RULES:
                if rx.search(line):
                    findings.append({
                        "rule": rid, "kind": "codebase",
                        "file": str(path.relative_to(root)), "line": i,
                        "osv_severity": sev, "cwe": cwe,
                        "summary": summary, "snippet": line.strip()[:200],
                    })

    result = {"project": str(root), "n_files": files,
              "n_findings": len(findings), "findings": findings}
    text = json.dumps(result, indent=2)
    if output:
        pathlib.Path(output).write_text(text)
    print(f"codebase scan: {files} Python files, {len(findings)} findings")
    for f in findings[:20]:
        print(f"  {f['file']}:{f['line']}  {f['rule']}  ({f['osv_severity']})")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=".")
    ap.add_argument("--output", default=None)
    a = ap.parse_args()
    scan(a.project, a.output)
