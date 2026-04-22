#!/usr/bin/env bash
# Build a community release — strips ecosystem-only code for open-source distribution.
#
# Usage: ./scripts/build-community-release.sh [output_dir]
#
# Output: a clean directory with only community-safe code:
#   - packages/core/
#   - packages/community/  (with frontend_dist/ embedded)
#   - frontend/            (without _ecosystem/ adapters)
#   - scripts/
#   - README.md

set -euo pipefail

OUTPUT_DIR="${1:-./community-release}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Okto Pulse Community Release Builder ==="
echo "  Source: $REPO_ROOT"
echo "  Output: $OUTPUT_DIR"

# Clean output
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

# 1. Copy packages/core (full)
echo "  Copying packages/core..."
cp -r "$REPO_ROOT/packages/core" "$OUTPUT_DIR/packages/core"

# 2. Copy packages/community (full)
echo "  Copying packages/community..."
cp -r "$REPO_ROOT/packages/community" "$OUTPUT_DIR/packages/community"

# 3. Copy frontend WITHOUT _ecosystem/ dirs
echo "  Copying frontend (excluding ecosystem adapters)..."
cp -r "$REPO_ROOT/frontend" "$OUTPUT_DIR/frontend"
rm -rf "$OUTPUT_DIR/frontend/src/adapters/auth/_ecosystem"
rm -rf "$OUTPUT_DIR/frontend/src/adapters/portal/_ecosystem"
rm -rf "$OUTPUT_DIR/frontend/node_modules"
rm -rf "$OUTPUT_DIR/frontend/dist"

# 4. Build frontend for community
echo "  Building frontend (VITE_AUTH_MODE=local)..."
cd "$OUTPUT_DIR/frontend"
npm install 2>/dev/null || echo "  Warning: npm install failed (may need manual install)"
VITE_AUTH_MODE=local VITE_API_URL= npx vite build --outDir dist 2>/dev/null || echo "  Warning: build failed"

# 5. Copy dist to community package
if [ -d "$OUTPUT_DIR/frontend/dist" ]; then
    echo "  Embedding frontend dist in community package..."
    cp -r "$OUTPUT_DIR/frontend/dist" "$OUTPUT_DIR/packages/community/src/okto_pulse/community/frontend_dist"
fi

# 6. Copy scripts and docs
cp -r "$REPO_ROOT/scripts" "$OUTPUT_DIR/scripts" 2>/dev/null || true
cp "$REPO_ROOT/packages/community/README.md" "$OUTPUT_DIR/README.md" 2>/dev/null || true

# 7. Do NOT copy packages/ecosystem or backend/ (ecosystem-only)
echo ""
echo "=== Release ready at: $OUTPUT_DIR ==="
echo "  Excluded: packages/ecosystem/, backend/, _ecosystem/ adapters"
echo ""
echo "  Contents:"
echo "    packages/core/      — shared models, services, API, MCP"
echo "    packages/community/ — local auth, SQLite, CLI, frontend_dist"
echo "    frontend/           — React SPA (community adapters only)"
ls -la "$OUTPUT_DIR"
