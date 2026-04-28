#!/usr/bin/env bash
# rebuild.sh — Full okto-pulse rebuild + Portainer redeploy
#
# Run as root on the server — either directly or via SSH from your Mac:
#   ssh root@192.168.31.154 "cd /home/maheidem/docker/opencode/work/Oktolabs/deploy && ./rebuild.sh"
#
# Or from inside the deploy dir on the server:
#   ./rebuild.sh           # uses Docker layer cache (fast after first build)
#   NO_CACHE=1 ./rebuild.sh   # forces full rebuild (~5 min, re-downloads all deps)
#
# Workflow:
#   1. git pull both repos
#   2. docker compose build (local-runtime target)  →  image: okto-pulse:local
#   3. Portainer stack 62 update via API  →  container recreates with new image
#   4. Wait for healthy + smoke test

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"
PORTAINER_BASE="https://localhost:9443/api"
STACK_ID=62
ENDPOINT_ID=2
NO_CACHE="${NO_CACHE:-}"

GET_CRED="$SCRIPT_DIR/get_cred.py"

# ── Helpers ────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ── Read credentials ────────────────────────────────────────────────────────
API_KEY="$(python3 "$GET_CRED" api_key)"
ADMIN_PASS="$(python3 "$GET_CRED" password)"
[[ -n "$API_KEY" ]]    || die "Could not read Portainer API key"
[[ -n "$ADMIN_PASS" ]] || die "Could not read Portainer admin password"

# ── 1. Git pull ─────────────────────────────────────────────────────────────
log "Pulling okto-pulse..."
git -C "$REPO_ROOT/okto-pulse" pull

log "Pulling okto-pulse-core..."
git -C "$REPO_ROOT/okto-pulse-core" pull

# ── 2. Docker build ─────────────────────────────────────────────────────────
log "Building okto-pulse:local..."
BUILD_ARGS=()
[[ -n "$NO_CACHE" ]] && BUILD_ARGS+=(--no-cache)
docker compose -f "$SCRIPT_DIR/docker-compose.yml" build "${BUILD_ARGS[@]}"
log "Build complete."

# ── 3. Portainer redeploy ───────────────────────────────────────────────────
log "Getting JWT for Portainer write access..."
JWT="$(curl -sk -X POST -H 'Content-Type: application/json' \
  -d "{\"username\":\"maheidem\",\"password\":\"${ADMIN_PASS}\"}" \
  "${PORTAINER_BASE}/auth" | python3 -c "import sys,json; print(json.load(sys.stdin)['jwt'])")"
[[ -n "$JWT" ]] || die "Failed to get Portainer JWT"

log "Fetching current stack compose from Portainer..."
COMPOSE_CONTENT="$(curl -sk -H "X-API-Key: ${API_KEY}" \
  "${PORTAINER_BASE}/stacks/${STACK_ID}/file" | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['StackFileContent'])")"
[[ -n "$COMPOSE_CONTENT" ]] || die "Failed to read stack $STACK_ID compose from Portainer"

log "Redeploying Portainer stack $STACK_ID..."
UPDATE_RESULT="$(python3 - "${COMPOSE_CONTENT}" "${JWT}" "${STACK_ID}" "${ENDPOINT_ID}" << 'PYEOF'
import json, sys, urllib.request, ssl

compose, jwt, stack_id, endpoint_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

body = json.dumps({
    'env': [],
    'stackFileContent': compose,
    'prune': False,
    'pullImage': False,
}).encode()

req = urllib.request.Request(
    f'https://localhost:9443/api/stacks/{stack_id}?endpointId={endpoint_id}',
    data=body,
    method='PUT',
    headers={
        'Authorization': f'Bearer {jwt}',
        'Content-Type': 'application/json',
    }
)
with urllib.request.urlopen(req, context=ctx) as r:
    d = json.load(r)
    print(d.get('Id'), d.get('Name'), d.get('Status'))
PYEOF
)"
log "Portainer response: $UPDATE_RESULT"

# ── 4. Wait for healthy ──────────────────────────────────────────────────────
log "Waiting for container to become healthy..."
for i in $(seq 1 18); do
  STATUS="$(curl -sk -H "X-API-Key: ${API_KEY}" \
    "${PORTAINER_BASE}/endpoints/${ENDPOINT_ID}/docker/containers/json" | \
    python3 -c "
import sys, json
d = json.load(sys.stdin)
c = [x for x in d if 'pulse' in str(x['Names']).lower()]
print(c[0]['Status'] if c else 'not found')
" 2>/dev/null || echo "api-error")"
  if echo "$STATUS" | grep -q "(healthy)"; then
    log "Container healthy: $STATUS"
    break
  fi
  log "  ... $STATUS (attempt $i/18)"
  sleep 5
done

# ── 5. Smoke test ────────────────────────────────────────────────────────────
log "Verifying config.js..."
CONFIG_JS="$(curl -fsS http://127.0.0.1:9100/config.js 2>/dev/null || echo FAILED)"
if echo "$CONFIG_JS" | grep -q "192.168.31.154"; then
  log "config.js OK"
else
  log "WARNING: config.js unexpected:"
  echo "$CONFIG_JS"
fi

log "Verifying API..."
API_STATUS="$(curl -o /dev/null -sw "%{http_code}" http://127.0.0.1:9100/api/v1/kg/settings 2>/dev/null)"
if [[ "$API_STATUS" == "200" ]]; then
  log "API OK (HTTP 200)"
else
  log "WARNING: API returned HTTP $API_STATUS"
fi

log "Done. okto-pulse rebuilt and redeployed."
