# The agent sandbox image (agentic mode, Part B, and the terminal of Manual and Auto): the already-built, already-vetted tool image of Scan Codebase (aivdf/scan-tools:1), with `git` added so a
# session's checkpoints can be real commits, and the two command tools of the agent's terminal: rtk (condenses what a command prints; the Apache-2.0 edition) and ripgrep. The two programs are
# not downloaded here: agent_image.build() fetches the pinned release of each for this machine's architecture, checks its size and SHA-256, and puts the program in this build's context
# (bin/), so the build itself needs the network only for apt. Built only on the owner's say-so (python -m app.backend.pipeline.scan.agent_image build), after the tool image itself exists.
# The container made from this image never reaches the network: the agent sandbox starts it exactly like the scan sandbox (network_mode="none").
#
# Unlike the scan toolkit's Python and npm packages, git has no hash-pinned archive for this base image to require: apt installs whatever git is in the base image's own package
# snapshot at build time. The version actually installed is read back from this step's own output, not pinned ahead of it (agent_image.py's build() records it afterwards).

ARG BASE_IMAGE
FROM ${BASE_IMAGE}

LABEL aivdf.image="1" \
      aivdf.agent_sandbox="1"

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && git --version
COPY bin/rtk /usr/local/bin/rtk
COPY bin/rg /usr/local/bin/rg
COPY xdg/ /etc/xdg/
COPY licenses/ /usr/share/doc/aivdf-tools/
RUN chmod 0755 /usr/local/bin/rtk /usr/local/bin/rg \
 && rtk --version \
 && rg --version | head -n 1 \
 && command -v bash setsid timeout awk grep sed kill sleep wc > /dev/null
USER 10001
