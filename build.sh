#!/bin/bash
# Build script for okto-pulse CLI
# This script builds both okto-pulse-core and okto-pulse packages

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Resolve the sibling repositories from this script, independent of the
# developer workstation path.  CI may override the Core checkout explicitly.
COMMUNITY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "${COMMUNITY_DIR}/.." && pwd)"
CORE_DIR="${OKTO_PULSE_CORE_DIR:-${WORKSPACE_DIR}/okto-pulse-core}"
FRONTEND_DIR="${COMMUNITY_DIR}/frontend"
PACKAGED_FRONTEND_DIR="${COMMUNITY_DIR}/src/okto_pulse/community/frontend_dist"

if [ ! -f "${CORE_DIR}/pyproject.toml" ]; then
    echo -e "${RED}Core checkout not found: ${CORE_DIR}${NC}" >&2
    exit 1
fi

ensure_python_build_tool() {
    if python -c 'import build' >/dev/null 2>&1; then
        return
    fi

    echo -e "${YELLOW}Python wheel builder not found; installing it...${NC}"
    python -m pip install --disable-pip-version-check "build>=1.2,<2"
}

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Okto Pulse CLI Build Script${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Step 1: Always rebuild and verify the frontend embedded in the wheel.
echo -e "${YELLOW}[1/4] Building packaged frontend...${NC}"
cd "${FRONTEND_DIR}"
npm ci
npm run build
npm run verify:frontend-dist
if [ ! -f "${PACKAGED_FRONTEND_DIR}/index.html" ]; then
    echo -e "${RED}Packaged frontend is missing: ${PACKAGED_FRONTEND_DIR}/index.html${NC}" >&2
    exit 1
fi
echo -e "${GREEN}✓ Packaged frontend built and hash-verified${NC}"

# Step 2: Build okto-pulse-core
echo ""
echo -e "${YELLOW}[2/4] Building okto-pulse-core...${NC}"
cd "${CORE_DIR}"
ensure_python_build_tool
python -m build --wheel
CORE_WHEEL="$(find "${CORE_DIR}/dist" -maxdepth 1 -type f -name 'okto_pulse_core-0.3.0-*.whl' -print -quit)"
if [ -z "${CORE_WHEEL}" ]; then
    echo -e "${RED}Core 0.3.0 wheel was not produced${NC}" >&2
    exit 1
fi
echo -e "${GREEN}✓ okto-pulse-core built${NC}"

# Step 3: Build okto-pulse (community)
echo ""
echo -e "${YELLOW}[3/4] Building okto-pulse (community)...${NC}"
cd "${COMMUNITY_DIR}"
python -m build --wheel
COMMUNITY_WHEEL="$(find "${COMMUNITY_DIR}/dist" -maxdepth 1 -type f -name 'okto_pulse-0.3.0-*.whl' -print -quit)"
if [ -z "${COMMUNITY_WHEEL}" ]; then
    echo -e "${RED}Community 0.3.0 wheel was not produced${NC}" >&2
    exit 1
fi
echo -e "${GREEN}✓ okto-pulse (community) built${NC}"

# Step 4: Summary
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Build Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Built packages:"
echo "  • ${CORE_WHEEL}"
echo "  • ${COMMUNITY_WHEEL}"
echo ""
echo -e "${YELLOW}To install:${NC}"
echo "  pip install ${CORE_DIR}/dist/okto_pulse_core-*.whl"
echo "  pip install ${COMMUNITY_DIR}/dist/okto_pulse-*.whl"
echo ""
echo -e "${YELLOW}Or install in editable mode:${NC}"
echo "  pip install -e ${CORE_DIR}"
echo "  pip install -e ${COMMUNITY_DIR}"
echo ""
