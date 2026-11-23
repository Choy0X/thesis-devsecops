"""Install pinned app-managed RTK, ripgrep, and Windows MinGit into ``data/tools/bin``.

App setup runs this for supported platforms. From the repository root::

    .venv\Scripts\python -m tools.install_toolchain               # supported tools
    .venv\Scripts\python -m tools.install_toolchain --tool rtk     # one
    .venv\Scripts\python -m tools.install_toolchain --status       # what is pinned and what is installed; nothing is downloaded
"""
from __future__ import annotations

import argparse
import sys

from app.backend.pipeline.agentic.exec import toolchain


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--tool", choices=sorted(toolchain.load_lock()["tools"]), help="one tool (default: all of them)")
    ap.add_argument("--status", action="store_true", help="show what is pinned and installed; download nothing")
    ap.add_argument("--retest", action="store_true", help="run the self-test of what is installed again")
    ap.add_argument("--defaults", action="store_true", help="prepare supported tools that need installation")
    args = ap.parse_args(argv)
    tools = [args.tool] if args.tool else [row["tool"] for row in toolchain.status()["tools"] if row["supported"] or args.status or args.retest]
    if args.defaults:
        toolchain.prepare_defaults()
        missing = [row for row in toolchain.status()["tools"] if row["supported"] and not row["tested"]]
        for row in missing:
            print(f"{row['tool']}: {row['preparation_error'] or 'unavailable'}", file=sys.stderr)
        return int(bool(missing))
    if args.status or args.retest:
        for t in tools:
            if args.retest and toolchain.installed(t):
                toolchain.retest(t)
        for row in toolchain.status()["tools"]:
            if row["tool"] in tools:
                state = "installed and tested" if row["tested"] else "installed, not passing its test" if row["installed"] else "not installed"
                print(f"{row['tool']} {row['wanted']} ({row['licence']}): {state}" + ("" if row["supported"] else " (no build for this computer)"))
                for n in row["notes"]:
                    print("   " + n)
        return 0
    last = {"name": None}

    def show(name, done, total):
        if last["name"] != name:
            last["name"] = name
            print(f"fetching {name} ({total / 1048576:.1f} MB)", flush=True)
    for t in tools:
        try:
            record = toolchain.install(t, progress=show)
        except toolchain.ToolchainError as e:
            print(f"{t}: not installed: {e}", file=sys.stderr)
            return 1
        print(f"{t} {record['version']}: installed. " + "; ".join(record["notes"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
