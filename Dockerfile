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

FROM python:3.12-slim AS base
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
RUN pip install build
COPY okto-pulse-core/ /src/okto-pulse-core/
COPY okto-pulse/      /src/okto-pulse/
RUN python -m build --wheel --outdir /wheels /src/okto-pulse-core \
 && python -m build --wheel --outdir /wheels /src/okto-pulse

FROM base AS local-install
COPY --from=wheel-builder /wheels /wheels
RUN pip install /wheels/okto_pulse_core-*.whl /wheels/okto_pulse-*.whl

# =============================================================================
# PYPI PATH (prod target)
# =============================================================================

FROM base AS pypi-install
ARG OKTO_PULSE_VERSION=0.1.10
RUN pip install "okto-pulse==${OKTO_PULSE_VERSION}"

# =============================================================================
# RUNTIME FINALIZER — local source
# =============================================================================

FROM local-install AS local-runtime
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
 && python -c "import os,sys; sys.exit(0 if any('all-MiniLM' in p for p,_,_ in os.walk(os.environ['HF_HOME'])) else 1)"
# Patch: replace the hardcoded 127.0.0.1 in MCP server with an env-var lookup.
# Uses find rather than importlib to avoid triggering the import chain at build time.
# grep -q asserts the patch applied — fails the build if the line was renamed upstream.
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
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
 && python -c "import os,sys; sys.exit(0 if any('all-MiniLM' in p for p,_,_ in os.walk(os.environ['HF_HOME'])) else 1)"
RUN SERVER_PY=$(find /usr/local/lib -name "server.py" -path "*/okto_pulse/core/mcp/server.py" | head -1) \
 && sed -i 's|host="127.0.0.1", port=port|host=os.environ.get("MCP_HOST", "0.0.0.0"), port=port|' "$SERVER_PY" \
 && grep -q 'MCP_HOST' "$SERVER_PY"
EXPOSE 8100 8101
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/api/v1/kg/settings || exit 1
VOLUME ["/data"]
CMD ["okto-pulse", "serve"]
