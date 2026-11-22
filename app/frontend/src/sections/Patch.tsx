import { Panel } from "../components/ui";
import { RunConsole } from "../components/RunConsole";

export function Patch() {
  return (
    <div className="space-y-5">
      <Panel title="Self-patching (feasibility)" desc="Generate a candidate mitigation, apply it in an isolated Docker sandbox, run the tests, roll back. A feasibility demonstration — patches are candidates for human review, never auto-merged.">
        <RunConsole stage="patch" params={{ cases: "data/patch_cases.json", tag: "patch_feasibility" }}
          label="Run feasibility set"
          hint="Needs data/patch_cases.json and a reachable Docker daemon + LLM endpoint." />
      </Panel>
      <Panel title="Case format" desc="data/patch_cases.json">
        <pre className="overflow-x-auto rounded-lg border border-line bg-terminal p-3 font-mono text-[0.76rem] leading-relaxed text-muted">{`[
  {
    "cve": "CVE-2024-XXXX",
    "advisory": "SQL injection via unsanitised 'name' parameter",
    "filename": "app/db.py",
    "code": "def lookup(name):\\n    return db.execute(f\\"SELECT * FROM u WHERE n='{name}'\\")",
    "test_cmd": "python -m pytest -q"
  }
]`}</pre>
      </Panel>
    </div>
  );
}
