#!/bin/bash
# Build script for okto-pulse CLI
# This script builds both okto-pulse-core and okto-pulse packages

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Root directory
ROOT_DIR="D:/Projetos/Techridy"
CORE_DIR="${ROOT_DIR}/okto_labs_pulse_core"
COMMUNITY_DIR="${ROOT_DIR}/okto_labs_pulse_community"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Okto Pulse CLI Build Script${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""

# Step 1: Build the frontend (if needed)
echo -e "${YELLOW}[1/4] Checking frontend...${NC}"
if [ ! -d "${COMMUNITY_DIR}/frontend_dist" ] || [ ! -f "${COMMUNITY_DIR}/frontend_dist/index.html" ]; then
    echo -e "${YELLOW}Building frontend...${NC}"
    cd "${COMMUNITY_DIR}/frontend"
    npm install
    npm run build
    echo -e "${GREEN}✓ Frontend built${NC}"
else
    echo -e "${GREEN}✓ Frontend already built${NC}"
fi

# Step 2: Build okto-pulse-core
echo ""
echo -e "${YELLOW}[2/4] Building okto-pulse-core...${NC}"
cd "${CORE_DIR}"
python -m pip install --upgrade build
python -m build
echo -e "${GREEN}✓ okto-pulse-core built${NC}"

# Step 3: Build okto-pulse (community)
echo ""
echo -e "${YELLOW}[3/4] Building okto-pulse (community)...${NC}"
cd "${COMMUNITY_DIR}"
python -m build
echo -e "${GREEN}✓ okto-pulse (community) built${NC}"

# Step 4: Summary
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Build Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Built packages:"
echo "  • ${CORE_DIR}/dist/"
echo "  • ${COMMUNITY_DIR}/dist/"
echo ""
echo -e "${YELLOW}To install:${NC}"
echo "  pip install ${CORE_DIR}/dist/okto_pulse_core-*.whl"
echo "  pip install ${COMMUNITY_DIR}/dist/okto_pulse-*.whl"
echo ""
echo -e "${YELLOW}Or install in editable mode:${NC}"
echo "  pip install -e ${CORE_DIR}"
echo "  pip install -e ${COMMUNITY_DIR}"
echo ""
