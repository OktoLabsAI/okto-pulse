#!/usr/bin/env python3
"""Smoke test for the Okto Pulse embedding provider.

Hits GET /api/v1/kg/settings on a running community instance and exits:
  0 — when embedding_provider_name looks like a SentenceTransformer* provider
      AND is_loaded is true (healthy semantic search).
  1 — otherwise, with a one-line diagnostic showing what came back.

Usage:
    python scripts/smoke_embedding.py
    python scripts/smoke_embedding.py --url http://localhost:8100
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

_DEFAULT_URL = "http://localhost:8100"
_ENDPOINT = "/api/v1/kg/settings"
_HEALTHY_PREFIX = "SentenceTransformer"


def _fetch(url: str, timeout: float) -> dict[str, Any]:
    req = urllib_request.Request(url, headers={"Accept": "application/json"})
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=_DEFAULT_URL,
        help=f"Base URL of the running okto-pulse instance (default: {_DEFAULT_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="HTTP timeout in seconds (default: 5)",
    )
    args = parser.parse_args(argv)

    target = args.url.rstrip("/") + _ENDPOINT

    try:
        payload = _fetch(target, args.timeout)
    except urllib_error.URLError as exc:
        print(f"FAIL — could not reach {target}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"FAIL — {target} returned non-JSON: {exc}", file=sys.stderr)
        return 1

    name = str(payload.get("embedding_provider_name") or payload.get("embedding_provider") or "")
    is_loaded = bool(payload.get("is_loaded"))
    is_stub = bool(payload.get("is_stub"))
    model = payload.get("model_name")
    dim = payload.get("embedding_dimension")

    healthy = name.startswith(_HEALTHY_PREFIX) and is_loaded and not is_stub
    if healthy:
        print(
            f"OK — provider={name} model={model} dim={dim} is_loaded={is_loaded}"
        )
        return 0

    print(
        "FAIL — "
        f"provider={name!r} model={model!r} dim={dim} is_loaded={is_loaded} is_stub={is_stub}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
