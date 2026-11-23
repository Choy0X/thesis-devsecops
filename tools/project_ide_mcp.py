"""App-owned MCP stdio transport for the workspace-bound Projects tool service.

No network listener, shell execution, dynamic imports, or extension discovery.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# OpenCode launches this absolute script from the isolated workspace.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.backend.pipeline.project_ide.tools import DEFINITIONS, ToolService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--receipts", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--knowledge", type=Path)
    args = parser.parse_args()
    service = ToolService(args.workspace, args.receipts, source=args.source, cache=args.cache, knowledge=args.knowledge)
    for line in sys.stdin:
        if len(line) > 2 * 1024 * 1024:
            break
        request = None
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            method = request.get("method")
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "projects-workspace-tools", "version": "1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": DEFINITIONS}
            elif method == "tools/call":
                params = request.get("params", {})
                try:
                    value = service.call(params.get("name"), params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=True)}], "isError": False}
                except (ValueError, OSError, KeyError) as exc:
                    result = {"content": [{"type": "text", "text": str(exc)[:2000]}], "isError": True}
            elif method in ("resources/list", "prompts/list"):
                result = {"resources" if method == "resources/list" else "prompts": []}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method unavailable"}}), flush=True)
                continue
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32603, "message": type(exc).__name__}}
        print(json.dumps(response, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
