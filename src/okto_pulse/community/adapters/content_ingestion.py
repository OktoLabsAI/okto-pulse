"""Community content-ingestion adapter for MCP content references."""

from __future__ import annotations

import ipaddress
import logging
import mimetypes
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, unquote

import httpx

from okto_pulse.core.ports.content_ingestion import (
    ContentIngestionError,
    IngestedBinaryContent,
    IngestedTextContent,
)

_MAX_REDIRECTS = 5
_LOCAL_SCHEMES = {"", "local", "file"}
_REMOTE_SCHEMES = {"http", "https"}
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_TYPES = {
    "application/json",
    "application/ld+json",
    "application/markdown",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
}
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ValidatedRemoteTarget:
    original_url: str
    connect_url: str
    hostname: str
    host_header: str
    validated_ip: str


def _is_blocked_ip(ip: str) -> bool:
    parsed = ipaddress.ip_address(ip)
    return (
        parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_private
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _raise_ssrf_blocked(host: str, ip: str | None, reason: str) -> None:
    _LOGGER.warning(
        "blocked remote content fetch host=%s ip=%s reason=%s",
        host,
        ip or "",
        reason,
    )
    raise ContentIngestionError("ssrf_blocked", "remote content host is not allowed")


def _host_header_for(parsed) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        return f"{host}:{parsed.port}"
    return host


def _url_host_for_ip(ip: str) -> str:
    return f"[{ip}]" if ":" in ip else ip


def _connect_url_for(raw: str, validated_ip: str) -> str:
    parsed = urlparse(raw)
    host = _url_host_for_ip(validated_ip)
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunparse(
        (
            parsed.scheme,
            host,
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def _validate_remote_url(raw: str) -> _ValidatedRemoteTarget:
    parsed = urlparse(raw)
    if parsed.scheme not in _REMOTE_SCHEMES:
        raise ContentIngestionError("unsupported_reference", "unsupported content reference scheme")
    if not parsed.hostname:
        raise ContentIngestionError("invalid_reference", "remote content reference is missing a host")

    host = parsed.hostname.strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        _raise_ssrf_blocked(host, None, "localhost")
    try:
        if _is_blocked_ip(host):
            _raise_ssrf_blocked(host, host, "literal_ip_blocked")
    except ValueError:
        pass

    try:
        addresses = socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ContentIngestionError("invalid_reference", "remote content host could not be resolved")
    resolved_ips = [addr[-1][0] for addr in addresses]
    for ip in resolved_ips:
        if _is_blocked_ip(ip):
            _raise_ssrf_blocked(host, ip, "resolved_ip_blocked")
    validated_ip = resolved_ips[0]
    return _ValidatedRemoteTarget(
        original_url=raw,
        connect_url=_connect_url_for(raw, validated_ip),
        hostname=host,
        host_header=_host_header_for(parsed),
        validated_ip=validated_ip,
    )


class CommunityContentIngestionResolver:
    """Resolve local-first MCP references outside the core runtime."""

    def __init__(self, *, root: str | Path | None = None, timeout_s: float = 30.0):
        default_root = os.environ.get("OKTO_PULSE_CONTENT_ROOT") or os.getcwd()
        self.root = Path(root or default_root).expanduser().resolve()
        self.timeout_s = timeout_s

    def _local_path(self, reference: str) -> Path:
        parsed = urlparse(reference)
        if parsed.scheme not in _LOCAL_SCHEMES:
            raise ContentIngestionError("unsupported_reference", "unsupported content reference scheme")

        if parsed.scheme == "file":
            raw_path = unquote(parsed.path)
        elif parsed.scheme == "local":
            raw_path = unquote(f"{parsed.netloc}{parsed.path}")
        else:
            raw_path = reference

        if not raw_path:
            raise ContentIngestionError("invalid_reference", "local content reference is empty")

        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ContentIngestionError("not_found", "local content reference was not found")
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ContentIngestionError("path_outside_root", "local content reference is outside root")
        if not resolved.is_file():
            raise ContentIngestionError("not_file", "local content reference is not a file")
        return resolved

    @staticmethod
    def _mime_for_path(path: Path) -> str | None:
        return mimetypes.guess_type(path.name)[0]

    @staticmethod
    def _text_allowed(mime_type: str | None) -> bool:
        if not mime_type:
            return True
        return mime_type.startswith(_TEXT_MIME_PREFIXES) or mime_type in _TEXT_MIME_TYPES

    async def _remote_bytes(self, reference: str, *, max_bytes: int) -> IngestedBinaryContent:
        target = _validate_remote_url(reference)
        async with httpx.AsyncClient(follow_redirects=False, timeout=self.timeout_s) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                extensions = {
                    "validated_ip": target.validated_ip,
                    "effective_connect_ip": target.validated_ip,
                }
                if target.original_url.startswith("https://"):
                    extensions["sni_hostname"] = target.hostname
                response = await client.get(
                    target.connect_url,
                    headers={"Host": target.host_header},
                    extensions=extensions,
                )
                if 300 <= response.status_code < 400 and response.headers.get("location"):
                    target = _validate_remote_url(urljoin(target.original_url, response.headers["location"]))
                    continue
                response.raise_for_status()
                declared_size = response.headers.get("content-length")
                if declared_size:
                    try:
                        if int(declared_size) > max_bytes:
                            raise ContentIngestionError("content_too_large", "remote content exceeds limit")
                    except ValueError:
                        pass
                data = response.content
                if len(data) > max_bytes:
                    raise ContentIngestionError("content_too_large", "remote content exceeds limit")
                mime_type = response.headers.get("content-type", "").split(";", 1)[0] or None
                return IngestedBinaryContent(data=data, mime_type=mime_type, source="remote")
        raise ContentIngestionError("too_many_redirects", "remote content redirected too many times")

    async def resolve_binary(
        self, reference: str, *, max_bytes: int
    ) -> IngestedBinaryContent:
        parsed = urlparse(reference)
        if parsed.scheme in _REMOTE_SCHEMES:
            return await self._remote_bytes(reference, max_bytes=max_bytes)

        path = self._local_path(reference)
        try:
            if path.stat().st_size > max_bytes:
                raise ContentIngestionError("content_too_large", "local content exceeds limit")
            return IngestedBinaryContent(
                data=path.read_bytes(),
                mime_type=self._mime_for_path(path),
                source="local",
            )
        except OSError:
            raise ContentIngestionError("read_failed", "local content could not be read")

    async def resolve_text(self, reference: str, *, max_bytes: int) -> IngestedTextContent:
        binary = await self.resolve_binary(reference, max_bytes=max_bytes)
        if not self._text_allowed(binary.mime_type):
            raise ContentIngestionError("unsupported_media_type", "content is not a supported text type")
        try:
            text = binary.data.decode("utf-8")
        except UnicodeDecodeError:
            raise ContentIngestionError("invalid_encoding", "content is not valid UTF-8 text")
        return IngestedTextContent(text=text, mime_type=binary.mime_type, source=binary.source)


def register_community_content_ingestion_resolver(
    *, root: str | Path | None = None
) -> CommunityContentIngestionResolver:
    from okto_pulse.core.runtime_registry import register_content_ingestion_resolver

    resolver = CommunityContentIngestionResolver(root=root)
    register_content_ingestion_resolver(resolver)
    return resolver


__all__ = [
    "CommunityContentIngestionResolver",
    "register_community_content_ingestion_resolver",
]
