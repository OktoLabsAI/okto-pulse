"""Community in-memory KG adapters (spec R05-B, Onda A).

Implements the core ``CacheBackend`` / ``RateLimiter`` / ``SessionStore`` ports
with the SAME semantics as the core embedded providers (LRU+TTL cache, token
bucket, per-session asyncio.Lock + TTL expiry) — extracted to the Community
edition so the core concretes can be retired in R05-E (register-before-remove).

The session store reuses the SHARED domain types (``ConsolidationSession`` /
``compute_content_hash`` / ``SessionStatus``) from the core — those are domain
contracts, not Onda A adapters.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import timedelta
from typing import Any

from okto_pulse.core.kg.schemas import SessionStatus
from okto_pulse.core.kg.session_manager import (
    ConsolidationSession,
    _now,
    compute_content_hash,
)

_MAX_SIZE = 1000
_TTL_SECONDS = 60.0


class CommunityInMemoryCache:
    """LRU+TTL cache backend (CacheBackend port)."""

    def __init__(self, max_size: int = _MAX_SIZE, ttl_seconds: float = _TTL_SECONDS):
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, Any]] = {}
        self._board_index: dict[str, set[str]] = {}

    def _key(self, tool_name: str, board_id: str, params: dict) -> str:
        raw = json.dumps({"t": tool_name, "b": board_id, "p": params}, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, tool_name: str, board_id: str, params: dict) -> tuple[bool, Any]:
        key = self._key(tool_name, board_id, params)
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        ts, val = entry
        if time.monotonic() - ts > self._ttl:
            self._cache.pop(key, None)
            return False, None
        return True, val

    def put(self, tool_name: str, board_id: str, params: dict, value: Any) -> None:
        if len(self._cache) >= self._max_size:
            oldest_key = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest_key)
        key = self._key(tool_name, board_id, params)
        self._cache[key] = (time.monotonic(), value)
        self._board_index.setdefault(board_id, set()).add(key)

    def invalidate_board(self, board_id: str) -> int:
        keys = self._board_index.pop(board_id, set())
        for k in keys:
            self._cache.pop(k, None)
        return len(keys)

    def stats(self) -> dict:
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "ttl_seconds": self._ttl,
            "boards_tracked": len(self._board_index),
        }

    def clear(self) -> None:
        self._cache.clear()
        self._board_index.clear()


class CommunityInMemoryRateLimiter:
    """Sliding-window token bucket (RateLimiter port). 30 tokens / 60s / agent."""

    def __init__(self, rate: int = 30, window: float = 60.0):
        self._rate = rate
        self._window = window
        self._tokens: dict[str, list[float]] = {}

    def allow(self, agent_id: str) -> tuple[bool, int]:
        now = time.monotonic()
        times = self._tokens.setdefault(agent_id, [])
        cutoff = now - self._window
        self._tokens[agent_id] = [t for t in times if t > cutoff]
        times = self._tokens[agent_id]
        if len(times) >= self._rate:
            oldest = times[0]
            retry_after = int(self._window - (now - oldest)) + 1
            return False, max(1, retry_after)
        times.append(now)
        return True, 0

    def reset(self, agent_id: str) -> None:
        self._tokens.pop(agent_id, None)


class CommunityInMemorySessionStore:
    """Per-session asyncio.Lock + TTL expiry session store (SessionStore port)."""

    def __init__(self, default_ttl_seconds: int = 3600):
        self._sessions: dict[str, ConsolidationSession] = {}
        self._global_lock = asyncio.Lock()
        self._default_ttl = default_ttl_seconds

    @property
    def default_ttl_seconds(self) -> int:
        return self._default_ttl

    async def create(
        self,
        *,
        session_id: str,
        board_id: str,
        artifact_id: str,
        artifact_type: str,
        agent_id: str,
        raw_content: str,
        ttl_seconds: int | None = None,
    ) -> ConsolidationSession:
        ttl = ttl_seconds or self._default_ttl
        now = _now()
        content_hash = compute_content_hash(raw_content, artifact_id, board_id)
        session = ConsolidationSession(
            session_id=session_id,
            board_id=board_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            agent_id=agent_id,
            content_hash=content_hash,
            started_at=now,
            expires_at=now + timedelta(seconds=ttl),
            raw_content=raw_content,
        )
        async with self._global_lock:
            if session_id in self._sessions:
                raise ValueError(f"session_id already exists: {session_id}")
            self._sessions[session_id] = session
        return session

    async def get(self, session_id: str) -> ConsolidationSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.is_expired() and session.status == SessionStatus.OPEN:
            session.status = SessionStatus.EXPIRED
            async with self._global_lock:
                self._sessions.pop(session_id, None)
            return None
        return session

    async def remove(self, session_id: str) -> None:
        async with self._global_lock:
            self._sessions.pop(session_id, None)

    async def sweep_expired(self) -> int:
        count = 0
        async with self._global_lock:
            expired_ids = [
                sid
                for sid, s in self._sessions.items()
                if s.is_expired() and s.status == SessionStatus.OPEN
            ]
            for sid in expired_ids:
                self._sessions[sid].status = SessionStatus.EXPIRED
                del self._sessions[sid]
                count += 1
        return count

    async def active_count(self) -> int:
        async with self._global_lock:
            return sum(
                1 for s in self._sessions.values() if s.status == SessionStatus.OPEN
            )

    def clear_for_tests(self) -> None:
        self._sessions.clear()


__all__ = [
    "CommunityInMemoryCache",
    "CommunityInMemoryRateLimiter",
    "CommunityInMemorySessionStore",
]
