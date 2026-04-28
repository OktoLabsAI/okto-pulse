# DEPLOY.md — Okto Pulse Rebuild & Redeploy Guide

## Architecture

The okto-pulse deployment on this server (192.168.31.154) has two parts:

1. **Image build** — Docker builds from source using the two sibling repos in this folder.
   Source repos are root-owned, so the build must run as root on the server.

2. **Container lifecycle** — Managed by **Portainer stack 62**.
   The Portainer compose uses `image: okto-pulse:local` (no build block).
   Portainer only manages start/stop/redeploy — it does NOT build the image.

**Workflow**: build image (SSH as root) → tell Portainer to redeploy → container recreates.

---

## Option 1 — SSH (full rebuild + redeploy)

Run from **any machine with root SSH access** to the server. All commands execute on the server.

### One-liner (from outside the server)

```bash
ssh root@192.168.31.154 "cd /home/maheidem/docker/opencode/work/Oktolabs/deploy && ./rebuild.sh"
```

Force a full rebuild (no Docker layer cache, ~5 min):

```bash
ssh root@192.168.31.154 "cd /home/maheidem/docker/opencode/work/Oktolabs/deploy && NO_CACHE=1 ./rebuild.sh"
```

### What rebuild.sh does

1. `git pull` both repos (`okto-pulse` + `okto-pulse-core`)
2. `docker compose build` from `deploy/` dir (context = Oktolabs root, target = `local-runtime`)
3. Calls Portainer API to update stack 62 → container recreates with new image
4. Waits for healthy status + smoke tests `config.js` and `/api/v1/kg/settings`

### Manual steps (if you need control over each stage)

```bash
# SSH in first
ssh root@192.168.31.154

# Pull repos
git -C /home/maheidem/docker/opencode/work/Oktolabs/okto-pulse pull
git -C /home/maheidem/docker/opencode/work/Oktolabs/okto-pulse-core pull

# Build image
cd /home/maheidem/docker/opencode/work/Oktolabs/deploy
docker compose build           # fast (layer cache)
# docker compose build --no-cache   # slow (full rebuild)

# Redeploy via Portainer API (from inside server, use localhost)
CREDS=/home/maheidem/server-management/.claude.local.md
API_KEY=$(python3 get_cred.py api_key)
PASSWORD=$(python3 get_cred.py password)

JWT=$(curl -sk -X POST -H "Content-Type: application/json" \
  -d "{\"username\":\"maheidem\",\"password\":\"$PASSWORD\"}" \
  https://localhost:9443/api/auth | python3 -c "import sys,json; print(json.load(sys.stdin)['jwt'])")

COMPOSE=$(curl -sk -H "X-API-Key: $API_KEY" \
  https://localhost:9443/api/stacks/62/file | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['StackFileContent'])")

python3 - "$COMPOSE" "$JWT" << 'EOF'
import json, sys, urllib.request, ssl
compose, jwt = sys.argv[1], sys.argv[2]
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
body = json.dumps({'env': [], 'stackFileContent': compose, 'prune': False, 'pullImage': False}).encode()
req = urllib.request.Request('https://localhost:9443/api/stacks/62?endpointId=2',
  data=body, method='PUT',
  headers={'Authorization': f'Bearer {jwt}', 'Content-Type': 'application/json'})
with urllib.request.urlopen(req, context=ctx) as r:
  d = json.load(r); print('Updated:', d.get('Id'), d.get('Name'))
EOF
```

---

## Option 2 — Portainer Web UI

Access from any browser on the LAN: **https://192.168.31.154:9443**
Login: user `maheidem`, password from `.claude.local.md`.

**Important**: The Portainer UI can redeploy the container, but it cannot build the image.
Always run a build via SSH first if the source code changed.

### Redeploy an already-built image

1. Go to **Stacks** → **okto-pulse** (stack 62)
2. Click **Editor** tab — verify the compose has the correct `PUBLIC_*` env vars
3. Click **Update the stack** → container recreates with the current `okto-pulse:local` image

### Check logs

Stacks → okto-pulse → click the container → **Logs** tab.
Or: **Containers** → `okto-pulse` → **Logs**.

### Container quick-restart (no image change)

Containers → `okto-pulse` → restart icon.

---

## Portainer API Reference

| Context | Base URL |
|---------|----------|
| From inside the server | `https://localhost:9443/api` |
| From outside (LAN) | `https://192.168.31.154:9443/api` |

**Endpoint ID**: `2` | **Stack ID**: `62`
**Read auth**: `X-API-Key: <key>` | **Write auth**: JWT from `POST /api/auth`

Credentials are in `/home/maheidem/server-management/.claude.local.md`.
Use `get_cred.py api_key` or `get_cred.py password` (in this directory) to extract them.

| Operation | Method | Endpoint |
|-----------|--------|----------|
| Get stack | GET | `/api/stacks/62` |
| Get compose | GET | `/api/stacks/62/file` |
| Update + redeploy | PUT | `/api/stacks/62?endpointId=2` (JWT, body: stackFileContent + prune + pullImage) |
| List containers | GET | `/api/endpoints/2/docker/containers/json` |
| Container logs | GET | `/api/endpoints/2/docker/containers/<id>/logs?stdout=1&stderr=1&tail=50` |
| Restart container | POST | `/api/endpoints/2/docker/containers/<id>/restart` |

> `pullImage: false` — always required here since the image is built locally, not from a registry.

---

## Verification

Run from outside (via SSH) or directly on the server:

```bash
# Container healthy
ssh root@192.168.31.154 "docker inspect --format='{{.State.Health.Status}}' okto-pulse"
# → healthy

# config.js uses correct external host (critical — browser SPA reads this)
curl http://192.168.31.154:9100/config.js
# → API_URL: 'http://192.168.31.154:9100/api/v1'

# API responds
curl http://192.168.31.154:9100/api/v1/kg/settings
# → 200 JSON

# MCP port bound (401 = auth required = port is up)
curl -i http://192.168.31.154:9101/mcp
# → HTTP 401
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `git pull` fails — "dubious ownership" | Repos owned by root, running as non-root | SSH as root |
| `docker compose build` fails at COPY | Wrong working directory | Run from `deploy/` so `context: ..` resolves to Oktolabs root |
| Build succeeds but container shows old code | Docker layer cache reused stale layers | `NO_CACHE=1 ./rebuild.sh` |
| Portainer API 401 | Wrong key or expired JWT | Re-run credential + JWT fetch |
| config.js shows `127.0.0.1` | Container started without `PUBLIC_*` env vars | Verify Portainer stack 62 compose has all three `PUBLIC_*` vars; redeploy |
| Browser "Failed to load boards" | config.js returning `127.0.0.1` | See row above |
| Port 9100 unreachable from network | Ports bound to loopback in compose | Must be `"9100:8100"` not `"127.0.0.1:9100:8100"` |

---

## Files in This Directory

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Build compose — defines image build, source of truth for env vars |
| `rebuild.sh` | Automated full rebuild + redeploy (pull → build → Portainer update → verify) |
| `get_cred.py` | Helper: extracts Portainer credentials from `.claude.local.md` |
| `DEPLOY.md` | This file |
