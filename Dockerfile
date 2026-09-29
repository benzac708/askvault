# syntax=docker/dockerfile:1.7
#
# Two stages, one job. The builder resolves and installs dependencies with uv
# into /opt/venv. The runtime is a stock slim Python that receives that venv and
# nothing else: no compiler, no uv, no build backend, no package-index
# credentials. That makes the shipped image both the smallest this service can
# be and the smallest blast radius if it is ever compromised.
#
# ARCHITECTURE. The artifact that ships is linux/arm64, matching the single k3s
# node it runs on, and the CI workflow pins that platform on every build. No
# line here is architecture-specific -- the service is pure Python and nothing
# is compiled -- so an x86 builder would produce a working image, just a
# different and unwanted one. That asymmetry is the whole reason the platform
# lives in CI rather than in this file: a Dockerfile cannot stop anyone building
# the wrong thing, but a build step that always says --platform makes it hard to
# do by accident. See D17a.
#
# WHAT THE RUNTIME DOES NOT GET. The corpus is baked into the image rather than
# mounted, so there is exactly one copy of it in exactly one repository and no
# ConfigMap holding documentation that can drift from the code that indexes it.
# The FTS5 index is *not* baked in: it is derived state, rebuilt from the corpus
# on every start, and written to a mounted emptyDir so a fresh pod can always
# reconstruct it. Nothing survives a restart on purpose. See D29.

# ----------------------------------------------------------------- builder --

# uv is copied in as a binary rather than installed by a package manager, so the
# resolver version is pinned by tag and cannot drift with the base image.
FROM ghcr.io/astral-sh/uv:0.12.9 AS uv

FROM python:3.12-slim-bookworm AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

# The venv lives outside the build context because /opt/venv is the unit that
# crosses into the runtime stage, and both stages have to agree on its absolute
# path. UV_PYTHON_DOWNLOADS=never is what makes the build hermetic: the base
# image already carries the pinned interpreter, so uv is forbidden from quietly
# fetching another one.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src

# Dependencies first, from the lockfile alone, before a line of application code
# exists in the image. That is the reason uv.lock is committed rather than
# generated: a change under app/ cannot change what gets installed, so a rebuild
# of a given commit resolves to the same versions, and a dependency bump shows up
# in review as its own commit instead of hiding inside a feature diff.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the code and the corpus, as a second layer. Editing a prompt or a
# template must not invalidate the dependency install above, which is the slow
# half of any build.
#
# samples/ is copied here and not mounted at runtime so there is exactly one copy
# of the corpus, in one repository, versioned with the retrieval code that parses
# it. A ConfigMap of documentation would drift from the parser, and a corpus
# mounted from outside would let the running index and the repository disagree
# about what the service actually answers from. See the header note on D29.
COPY app ./app
COPY samples ./samples

# --no-editable is load-bearing, not a preference. `uv sync` installs the project
# itself as editable by default, which writes a .pth file pointing back at
# /src -- a path that does not exist in the runtime stage. The build then
# succeeds, the venv ships, and the first import dies with ModuleNotFoundError on
# a module that is plainly present in the builder. A non-editable install bakes
# the code into site-packages, which is what makes the venv relocatable and the
# two-stage split work at all. Nothing in the image needs to be importable from a
# source tree, because the source tree is not in the image.
#
# --reinstall-package is load-bearing for a different reason: uv sync is
# idempotent against the lockfile, so an edit to web/template.html or a prompt
# that does not bump the project version looks "already installed" to it. A
# cached builder venv then carries the previous commit's app forward and the
# image silently ships stale UI (this bit the dead-centre ship). Reinstalling
# the project on every build is cheap -- the dependency layer above is what is
# slow -- and makes the baked code exactly the commit being tagged.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --reinstall-package askvault

# ----------------------------------------------------------------- runtime --

FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="askvault" \
      org.opencontainers.image.description="Internal-docs RAG service: lexical retrieval, cited answers, one replica." \
      org.opencontainers.image.source="https://github.com/benzac708/askvault" \
      org.opencontainers.image.licenses="MIT"

# PYTHONDONTWRITEBYTECODE is not a size optimisation here, it is a correctness
# one: the pod runs with a read-only root filesystem, so a __pycache__ write
# turns the first import after every restart into an EACCES traceback.
# PYTHONUNBUFFERED matters for the same reason in reverse -- logging is JSON lines
# on stdout, and a block-buffered stream would hand a crash-looping container's
# last few lines to nobody.
#
# DB_PATH is absolute, and that is a hard requirement rather than a style choice.
# The shipped default is relative, which resolves against the working directory
# and therefore lands on the read-only root filesystem. The Deployment sets the
# same absolute value explicitly (lock L6) so the manifest reads the same way
# whether or not you have the image to hand.
#
# /data is created here so a bare `docker run` works, but a pod mounts an
# emptyDir over it and kubelet creates that as root:root 0755 -- which UID 10001
# cannot write. The pod securityContext therefore sets fsGroup to the same 10001,
# which is the only reason the write succeeds under a read-only root filesystem.
# The two files that must agree on that number are this one and base/deployment.yaml.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    SAMPLES_DIR=/app/samples \
    DB_PATH=/data/askvault.db

# UID 10001 sits above the "normal user" floor of 1000, below the high range
# some registry scanners treat as dynamic allocation, and is not a number
# anything else in this estate already uses. /app stays root-owned and
# unwritable: the service reads its own code and never writes to it.
#
# --home-dir is set rather than left to default even though the directory is
# never created. `useradd --no-create-home` still writes HOME=/home/askvault
# into /etc/passwd, and a container runtime will faithfully export that to the
# process -- so the service runs with $HOME pointing at a path that does not
# exist. Harmless until something tries to use it, at which point it fails far
# from the cause. Pointing it at /nonexistent makes the intent checkable.
RUN groupadd --gid 10001 askvault \
    && useradd --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin askvault \
    && mkdir -p /data \
    && chown 10001:10001 /data

# The base image ships pip, and nothing here uses it. An image with a working
# package manager and a writable site-packages is one `pip install` away from
# running code that never went through review, and removing it costs nothing
# because the venv is already fully resolved by the builder.
# Runtime-stage only, so the builder keeps the tooling it needs to resolve.
# setuptools and pkg_resources are listed defensively: 3.12-slim-bookworm ships
# neither today, and `rm -rf` on an absent path is a no-op, so the list stays
# correct if a future tag does.
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip \
        /usr/local/lib/python3.12/site-packages/pip-* \
        /usr/local/lib/python3.12/site-packages/setuptools \
        /usr/local/lib/python3.12/site-packages/setuptools-* \
        /usr/local/lib/python3.12/site-packages/pkg_resources \
        /usr/local/lib/python3.12/site-packages/_distutils_hack \
        /usr/local/lib/python3.12/ensurepip \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /src/samples /app/samples

# Two checks on the finished filesystem, because both are claims about a build
# backend's defaults rather than about anything in this repository.
#
# 1. app/web/template.html *is* the user interface. hatchling includes it
#    because `packages = ["app"]` covers the whole directory, but that is a
#    claim about a default, and defaults change. This turns it into a checked
#    fact and fails the build rather than the deployment.
# 2. The runtime's dependencies import with pip gone from the image, which is
#    what proves the removal above removed only what it was meant to.
RUN python -c "\
import fastapi, uvicorn, prometheus_client; \
from app.web import TEMPLATE_PATH; \
assert TEMPLATE_PATH.is_file(), f'template not packaged: {TEMPLATE_PATH}'; \
print(f'template packaged at {TEMPLATE_PATH}')"

USER 10001:10001

EXPOSE 8000

# No --no-access-log here: app.core.logging already disables uvicorn's access
# logger in code, because the app emits its own structured request line and two
# access records per request is duplication that eventually disagrees with itself.
# Spelling it in both places would be a second source of truth.
#
# No HEALTHCHECK either. Kubernetes ignores it -- liveness and readiness are
# declared as probes in the manifest, where the timings live with the rest of the
# rollout policy, and a HEALTHCHECK would only mislead someone running the image
# by hand into thinking it gates something.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
