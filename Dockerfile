# Okto Pulse — multi-target Dockerfile
#
# Targets:
#   local-runtime  Build wheels from sibling source repos (dev workflow).
#                  Requires build context = parent of okto-pulse/ so Docker
#                  can COPY both okto-pulse/ and okto-pulse-core/.
#                  Used by docker-compose.yml.
#
#   pypi-runtime   Install okto-pulse from PyPI (reproducible release artifact).
#                  Build context = this repo only.
#                  Used by docker-compose.prod.yml.
#
# Both targets:
#   - Pre-download all-MiniLM-L6-v2 so the container starts offline-capable.
#   - Patch the MCP server to bind 0.0.0.0 (the core hardcodes 127.0.0.1 which
#     is unreachable from the host even with -p 8101:8101). Controlled at
#     runtime via MCP_HOST env var (default 0.0.0.0).
#   - Correct data/KG dir env vars: DATA_DIR and KG_BASE_DIR (the legacy
#     OKTO_PULSE_DATA_DIR env var is not read by the Python code).
#
# Python 3.12 required: core uses nested f-string syntax (PEP 701) added in 3.12.
#
# Reproducibility:
#   - Base image pinned by digest (manifest-list digest, multi-arch safe).
#     Refresh via: docker pull python:3.12-slim &&
#                  docker buildx imagetools inspect python:3.12-slim
#   - Transitive Python deps pinned via okto-pulse/uv.lock (local-runtime).
#   - uv version pinned (UV_VERSION ARG).
#   - HF model checksum verified at build time when HF_MODEL_SHA256 is set.

ARG UV_VERSION=0.7.12

# python:3.12-slim @ 2026-04-24 (manifest list digest, supports amd64/arm64).
FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf-cache/sentence_transformers
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# =============================================================================
# LOCAL-SOURCE PATH (dev target)
# =============================================================================

FROM base AS wheel-builder
ARG UV_VERSION
RUN pip install --no-cache-dir "uv==${UV_VERSION}" build
COPY okto-pulse-core/ /src/okto-pulse-core/
COPY okto-pulse/      /src/okto-pulse/
RUN python -m build --wheel --outdir /wheels /src/okto-pulse-core \
 && python -m build --wheel --outdir /wheels /src/okto-pulse

FROM base AS local-install
ARG UV_VERSION
COPY --from=wheel-builder /wheels /wheels
# Lockfile-driven install for reproducibility. uv.lock pins all transitive
# deps; we exclude both the project itself and okto-pulse-core (those come
# from the locally-built wheels in /wheels). Sibling pyproject.toml is copied
# only because [tool.uv.sources] in okto-pulse references it for resolution.
COPY okto-pulse/pyproject.toml okto-pulse/uv.lock /lock/okto-pulse/
COPY okto-pulse-core/pyproject.toml             /lock/okto-pulse-core/
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
 && cd /lock/okto-pulse \
 && uv export --frozen --no-emit-project --no-emit-package okto-pulse-core \
        --no-hashes --format requirements.txt > /tmp/requirements.lock \
 && uv pip install --system --no-deps -r /tmp/requirements.lock \
 && uv pip install --system --no-deps /wheels/okto_pulse_core-*.whl /wheels/okto_pulse-*.whl \
 && rm -rf /lock /tmp/requirements.lock

# =============================================================================
# PYPI PATH (prod target)
# =============================================================================

FROM base AS pypi-install
ARG UV_VERSION
ARG OKTO_PULSE_VERSION=0.1.11
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
 && uv pip install --system "okto-pulse==${OKTO_PULSE_VERSION}"

# =============================================================================
# RUNTIME FINALIZER — local source
# =============================================================================

FROM local-install AS local-runtime
# HF model integrity check.
# Download the model, then optionally verify the safetensors SHA against a
# known-good value (HF_MODEL_SHA256). When the ARG is empty we still print
# the actual SHA so a future build can pin it. To enable verification:
#   docker build --build-arg HF_MODEL_SHA256=<sha> ...
ARG HF_MODEL_SHA256=53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
 && python -c "import os,sys; sys.exit(0 if any('all-MiniLM' in p for p,_,_ in os.walk(os.environ['HF_HOME'])) else 1)" \
 && SAFETENSORS=$(find "$HF_HOME" -name 'model.safetensors' | head -1) \
 && ACTUAL_SHA=$(sha256sum "$SAFETENSORS" | awk '{print $1}') \
 && echo "all-MiniLM-L6-v2 model.safetensors sha256: ${ACTUAL_SHA}" \
 && if [ -n "${HF_MODEL_SHA256}" ]; then \
        if [ "${HF_MODEL_SHA256}" != "${ACTUAL_SHA}" ]; then \
            echo "::error::HF model SHA mismatch: expected ${HF_MODEL_SHA256} got ${ACTUAL_SHA}" >&2 ; \
            exit 1 ; \
        fi ; \
        echo "HF model integrity verified" ; \
    else \
        echo "WARNING: HF_MODEL_SHA256 not set — skipping integrity check. Pin the SHA above to enable." ; \
    fi
# Patch: replace the hardcoded 127.0.0.1 in MCP server with an env-var lookup.
# Uses find rather than importlib to avoid triggering the import chain at build time.
# grep -q asserts the patch applied — fails the build if the line was renamed upstream.
# (Phase 4a will move this into core source so the patch can be removed.)
RUN SERVER_PY=$(find /usr/local/lib -name "server.py" -path "*/okto_pulse/core/mcp/server.py" | head -1) \
 && sed -i 's|host="127.0.0.1", port=port|host=os.environ.get("MCP_HOST", "0.0.0.0"), port=port|' "$SERVER_PY" \
 && grep -q 'MCP_HOST' "$SERVER_PY"
EXPOSE 8100 8101
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/api/v1/kg/settings || exit 1
VOLUME ["/data"]
CMD ["okto-pulse", "serve"]

# =============================================================================
# RUNTIME FINALIZER — PyPI
# =============================================================================

FROM pypi-install AS pypi-runtime
ARG HF_MODEL_SHA256=53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
 && python -c "import os,sys; sys.exit(0 if any('all-MiniLM' in p for p,_,_ in os.walk(os.environ['HF_HOME'])) else 1)" \
 && SAFETENSORS=$(find "$HF_HOME" -name 'model.safetensors' | head -1) \
 && ACTUAL_SHA=$(sha256sum "$SAFETENSORS" | awk '{print $1}') \
 && echo "all-MiniLM-L6-v2 model.safetensors sha256: ${ACTUAL_SHA}" \
 && if [ -n "${HF_MODEL_SHA256}" ]; then \
        if [ "${HF_MODEL_SHA256}" != "${ACTUAL_SHA}" ]; then \
            echo "::error::HF model SHA mismatch: expected ${HF_MODEL_SHA256} got ${ACTUAL_SHA}" >&2 ; \
            exit 1 ; \
        fi ; \
        echo "HF model integrity verified" ; \
    else \
        echo "WARNING: HF_MODEL_SHA256 not set — skipping integrity check. Pin the SHA above to enable." ; \
    fi
RUN SERVER_PY=$(find /usr/local/lib -name "server.py" -path "*/okto_pulse/core/mcp/server.py" | head -1) \
 && sed -i 's|host="127.0.0.1", port=port|host=os.environ.get("MCP_HOST", "0.0.0.0"), port=port|' "$SERVER_PY" \
 && grep -q 'MCP_HOST' "$SERVER_PY"
EXPOSE 8100 8101
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/api/v1/kg/settings || exit 1
VOLUME ["/data"]
CMD ["okto-pulse", "serve"]
