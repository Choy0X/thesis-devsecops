"""Building the image of the agent sandbox (agentic mode, Part B): one layer on top of the already-built tool image of Scan Codebase (``aivdf/scan-tools:1``), adding ``git`` so a
session's checkpoints can be real commits rather than a second, independently-reasoned-about container, and the two command tools of the agent's terminal, rtk and ripgrep (the pinned release of
each for the architecture of the Docker daemon, fetched and checked here on this computer and put in the build's context, so the image build itself needs the network only for apt).

    python -m app.backend.pipeline.scan.agent_image build

The build needs the network once (for ``apt-get``), and only when the owner runs it by hand; it is never built automatically. The container made from the image never reaches the
network: ``agent_sandbox.py`` starts it exactly like the scan sandbox (``network_mode="none"``, every capability dropped, an ordinary user). Unlike the scan toolkit's Python and npm
packages, ``git`` has no hash-pinned archive to require ahead of the build; what is recorded instead, once the build finishes, is the version apt actually installed
(``data/sandbox/agent_image.json``, read by ``agent_sandbox.ensure()`` before the first session of a project ever creates a container) -- enough to notice a different git turning up
on the next build, without pretending to a reproducibility guarantee apt cannot give.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

from app.backend import sandbox
from app.backend.pipeline import fsio, util
from app.backend.pipeline.scan import sandbox_run

AGENT_DIR = pathlib.Path(__file__).resolve().parent / "agent_sandbox_image"
CONTEXT = pathlib.Path("data/sandbox/agent_context")            # the build context: the Dockerfile, the two programs, their licences and rtk's configuration (assembled, not committed)
IMAGE_RECORD = pathlib.Path("data/sandbox/agent_image.json")
TAG = "aivdf/agent-sandbox:1"
_GIT_VERSION = re.compile(r"^git version \S+")
_TOOL_VERSIONS = {"rtk": re.compile(r"^rtk (\S+)"), "ripgrep": re.compile(r"^ripgrep (\S+)")}
_ARCH = {"x86_64": "linux-x86_64", "amd64": "linux-x86_64", "aarch64": "linux-aarch64", "arm64": "linux-aarch64"}


class AgentImageError(ValueError):
    """The agent sandbox image could not be built, in a sentence (command-line use only)."""


class AgentImageMissing(ValueError):
    """The agent sandbox image has not been built yet, or its record is unreadable: an HTTP-shaped error so the agent API (Part C) can surface it directly, the way ``ChatStoreError``
    already does for Project Chat."""

    def __init__(self, message: str = "The agent sandbox image has not been built yet. It is built from the command line "
                                        "(python -m app.backend.pipeline.scan.agent_image build) once the tool image of Scan Codebase exists.", status: int = 409, code: str = "agent_image_missing"):
        super().__init__(message)
        self.status, self.code = status, code


def image_record() -> dict:
    """``{"tag", "id", "built_at", "base_id", "git_version"}`` of the agent sandbox image, from the record the build step writes."""
    try:
        doc = json.loads(IMAGE_RECORD.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("tag"):
            return doc
    except (OSError, ValueError):
        pass
    raise AgentImageMissing()


def stage_context(client, context: pathlib.Path, stage=None) -> dict:
    """Assemble the build context: the Dockerfile, the programs of the command tools for the architecture of the Docker daemon (fetched and checked by ``toolchain.stage_for_image``), their
    licences, and the configuration rtk reads inside the container (no tracking, a recall store that is kept). Returns what was staged, tool by tool."""
    from app.backend.pipeline.agentic.exec import rtk, toolchain
    import shutil
    try:
        arch = str((client.info() or {}).get("Architecture") or "x86_64").lower()
    except Exception:  # ignored: a daemon that does not say is taken to be the usual one
        arch = "x86_64"
    platform_name = _ARCH.get(arch)
    if platform_name is None:
        raise AgentImageError(f"The Docker daemon reports the architecture {arch!r}, which the command tools are not built for.")
    shutil.rmtree(context, ignore_errors=True)
    context.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(AGENT_DIR / "Dockerfile", context / "Dockerfile")
    try:
        staged = (stage or toolchain.stage_for_image)(context, platform_name)
    except toolchain.ToolchainError as e:
        raise AgentImageError(f"The command tools could not be prepared for the image: {e}") from None
    (context / "xdg" / "rtk").mkdir(parents=True, exist_ok=True)
    (context / "xdg" / "rtk" / "config.toml").write_text(rtk.CONFIG, encoding="utf-8")
    return {"platform": platform_name, "tools": staged}


def build(client=None, *, on_line=lambda line: None, context: pathlib.Path | None = None, stage=None) -> dict:
    """Build ``aivdf/agent-sandbox:1`` from the committed Dockerfile here, on top of whichever tool image of Scan Codebase is currently built, and record it."""
    client = client or sandbox._client()
    try:
        base = sandbox_run.toolkit_image()
    except sandbox_run.ToolkitMissing as e:
        raise AgentImageError(f"The agent sandbox builds on the tool image of Scan Codebase, which does not exist yet: {e}") from None
    context = pathlib.Path(context or CONTEXT)
    staged = stage_context(client, context, stage)
    log: list[str] = []
    try:
        stream = client.api.build(path=str(context), tag=TAG, buildargs={"BASE_IMAGE": base["tag"]}, labels={"aivdf.image": "1", "aivdf.agent_sandbox": "1"}, rm=True, decode=True, pull=False, nocache=False)
        for chunk in stream:
            if chunk.get("error"):
                raise AgentImageError("The build failed: " + str(chunk["error"]).strip()[-600:])
            text = (chunk.get("stream") or "").strip()
            if text:
                log.append(text)
                on_line(text)
    except AgentImageError:
        raise
    except Exception as e:
        raise AgentImageError(f"The build could not run ({type(e).__name__}: {str(e)[:200]})") from None
    image = client.images.get(TAG)
    git_version = next((m.group(0) for entry in log for line in entry.splitlines() for m in [_GIT_VERSION.match(line.strip())] if m), None)
    lines = [line.strip() for entry in log for line in entry.splitlines()]
    ran = {name: next((m.group(1) for line in lines for m in [rx.match(line)] if m), None) for name, rx in _TOOL_VERSIONS.items()}
    record = {"tag": TAG, "id": image.id, "built_at": util.now_zulu(), "size_mb": round(image.attrs.get("Size", 0) / 1e6), "base_tag": base["tag"], "base_id": base["id"],
              "git_version": git_version, "tools": {name: {**info, "ran": ran.get(name)} for name, info in staged["tools"].items()}, "platform": staged["platform"], "log_tail": log[-15:]}
    fsio.write_json(IMAGE_RECORD, record, indent=2)
    return record


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    try:
        if cmd == "build":
            rec = build(on_line=lambda line: print(line[-200:].encode("ascii", "replace").decode("ascii")))
            print(json.dumps(rec, indent=2, default=str, ensure_ascii=True)[:2000])
        else:
            print(__doc__)
            return 2
    except AgentImageError as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
