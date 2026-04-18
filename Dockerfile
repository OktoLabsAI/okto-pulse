# Okto Pulse community edition — self-contained image with pre-downloaded
# embedding model so `docker run --network=none ...` starts healthy (AC-7).
#
# Build:  docker build -t okto-pulse:community .
# Run:    docker run --rm -p 8100:8100 -p 8101:8101 okto-pulse:community

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/opt/hf-cache \
    SENTENCE_TRANSFORMERS_HOME=/opt/hf-cache/sentence_transformers \
    OKTO_PULSE_DATA_DIR=/data

# Minimal OS deps: build-essential is only needed to compile a few wheels
# (e.g. tokenizers) on platforms without prebuilt wheels. curl is handy for
# healthchecks. Purge the apt cache to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the community package (brings okto-pulse-core + sentence-transformers
# as hard dependencies). Pin to a wheel from PyPI — override with
# `--build-arg OKTO_PULSE_VERSION=0.1.2` if a specific release is needed.
ARG OKTO_PULSE_VERSION=
RUN if [ -n "$OKTO_PULSE_VERSION" ]; then \
        pip install "okto-pulse==${OKTO_PULSE_VERSION}"; \
    else \
        pip install okto-pulse; \
    fi

# Pre-download the MiniLM embedding model into the HF cache so the first
# container start does not hit the network (TR-6, AC-7). Committing this
# layer to the image means `docker run --network=none` is enough to serve.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Smoke-test that the cache actually sits where we expect — fails the build
# early if HF_HOME was ignored, instead of shipping a broken offline image.
RUN python -c "import os, sys; root=os.environ['HF_HOME']; sys.exit(0 if any('all-MiniLM' in p for p,_,_ in os.walk(root)) else 1)"

EXPOSE 8100 8101

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/api/v1/kg/settings || exit 1

VOLUME ["/data"]

CMD ["okto-pulse", "serve"]
