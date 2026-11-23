# The image of Scan Codebase, tier 1. It is built only on the owner's say-so, and only from a lock file.
#
# Every version, digest and hash arrives as a build argument from toolkit.lock.json (made by the one-time resolve step, which needs the internet and is its own ask). The arguments
# have no defaults on purpose: a build without the lock fails instead of using whatever is newest. Bases are taken by digest, Python packages with --require-hashes, npm packages
# with npm ci (the lock file carries their integrity hashes), and nothing is fetched by a pipe into a shell.
#
# The build needs the network once. The container made from the image never does: its environment says so, and the runner starts it with no network except loopback.
#
# Build context (all produced by the resolve step, none of it in the repository until then):
#   requirements.venv.lock, requirements.test.lock    Python packages with hashes: semgrep and bandit (in a virtual environment); pytest and coverage (in the Python the tests run with)
#   js/package.json and js/package-lock.json    eslint and eslint-plugin-security
#   rules/               the Semgrep rules, vendored, with the licence text of each set
#   licences.json        the licence read from each package's metadata, checked against the approved list

ARG PYTHON_IMAGE
ARG NODE_IMAGE
ARG GITLEAKS_IMAGE

FROM ${NODE_IMAGE} AS node
FROM ${GITLEAKS_IMAGE} AS gitleaks

FROM ${PYTHON_IMAGE}

LABEL aivdf.image="1" \
      aivdf.toolkit="scan-codebase-tier1"

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --from=gitleaks /usr/bin/gitleaks /usr/local/bin/gitleaks

# The scan tools (semgrep, bandit) live in a virtual environment of their own, and only pytest and coverage are in the Python that the project's tests run with, so the tools' many packages (requests,
# rich, jsonschema ...) are not on the path of a project's tests: a project that forgot to list one of them fails, as it should.
COPY requirements.venv.lock requirements.test.lock /opt/toolkit/
RUN python -m venv /opt/toolkit/venv \
 && /opt/toolkit/venv/bin/pip install --no-cache-dir --require-hashes -r /opt/toolkit/requirements.venv.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/toolkit/requirements.test.lock

COPY js/package.json js/package-lock.json js/eslint.config.mjs /opt/toolkit/js/
RUN cd /opt/toolkit/js && npm ci --ignore-scripts --no-audit --no-fund

COPY rules /opt/toolkit/rules
COPY licences.json /opt/toolkit/licences.json

# nothing in the container may try the network, update itself or report back
ENV PATH="${PATH}:/opt/toolkit/venv/bin" \
    SEMGREP_SEND_METRICS=off \
    SEMGREP_ENABLE_VERSION_CHECK=0 \
    PIP_NO_INDEX=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NPM_CONFIG_OFFLINE=true \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin scanner \
 && mkdir -p /work \
 && chown scanner /work
USER 10001
WORKDIR /work
CMD ["sleep", "infinity"]
