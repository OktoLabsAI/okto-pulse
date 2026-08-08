"""Trusted Community runtime for Test Evidence V2.

CORE owns the transport-neutral contract. Community owns the concrete effects:
strict manifest loading under an installation-controlled root, real loopback
HTTP replay, and an immutable HMAC-authenticated receipt ledger. A client can
describe an attestation, but only this runtime can issue a receipt accepted by
write paths.
"""

from __future__ import annotations

# Runtime adapter, not a pytest module.  The filename is part of the public
# adapter surface, so opt out explicitly instead of renaming it and breaking
# imports; otherwise pytest attempts to collect the imported TestEvidence*
# contract dataclasses and emits misleading collection warnings.
__test__ = False

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from importlib import metadata
import inspect
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from okto_pulse.core.ports.test_evidence import (
    TestEvidenceExecutionRequest,
    TestEvidenceExecutionResult,
    TestEvidenceWriteVerification,
)
from okto_pulse.core.services.test_scenario_lifecycle import (
    EVIDENCE_V2_SCHEMA_VERSION,
    EvidenceVerificationResult,
    compute_execution_attestation_sha256,
    verify_mcp_replay_evidence_v2,
)


COMMUNITY_EVIDENCE_PRODUCER = "okto-pulse-community"
COMMUNITY_EVIDENCE_ADAPTER = "okto_pulse.community.adapters.test_evidence"
COMMUNITY_MANIFEST_SCHEMA = "okto-pulse-http-replay/v1"
COMMUNITY_MANIFEST_PURPOSE = "test_scenario_evidence"
COMMUNITY_RECEIPT_SCHEMA = "okto-pulse-evidence-receipt/v1"
COMMUNITY_MAX_LEDGER_RECEIPTS = 10_000
COMMUNITY_MAX_MANIFEST_BYTES = 1024 * 1024
_RECEIPT_RE = re.compile(r"^ev2r\.([0-9a-f]{32})\.([0-9a-f]{64})$")
_RECEIPT_FILE_RE = re.compile(r"^[0-9a-f]{32}\.json$")
_RECEIPT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECEIPT_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "issued_at",
        "board_id",
        "spec_id",
        "scenario_id",
        "scenario_sha256",
        "status",
        "actor_id",
        "manifest_ref",
        "manifest_sha256",
        "attestation_sha256",
        "run_id",
        "evidence_sha256",
        "secret_key_id",
        "signature",
    }
)
_LEGACY_RECEIPT_RECORD_KEYS = _RECEIPT_RECORD_KEYS - {"scenario_sha256"}


class CommunityTestEvidenceError(ValueError):
    """Raised when Community cannot produce or authenticate a proof."""


class _DuplicateJsonKeyError(ValueError):
    """Raised before a receipt object with ambiguous keys can be built."""


@dataclass(frozen=True, slots=True)
class ProductExecutionObservation:
    """Facts returned by a concrete product-runtime executor."""

    run_id: str
    outcome: str
    assertions: tuple[Mapping[str, Any], ...]
    executed_at: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceMigrationReport:
    scenarios: tuple[dict[str, Any], ...]
    canonical_v2: int
    promoted_v2: int
    legacy_unverified: int
    malformed: int


@dataclass(frozen=True, slots=True)
class PersistedEvidenceMigrationReport:
    specs_scanned: int
    specs_updated: int
    canonical_v2: int
    promoted_v2: int
    legacy_unverified: int
    malformed: int
    dry_run: bool


RuntimeExecutor = Callable[
    [Mapping[str, Any], str],
    ProductExecutionObservation | Awaitable[ProductExecutionObservation],
]


def _plain(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump(mode="python", exclude_none=True)
        return dict(result) if isinstance(result, Mapping) else None
    return None


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: object) -> str:
    return (
        "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    )


def _is_canonical_json_bytes(payload: bytes, value: object) -> bool:
    try:
        expected = _canonical_json(value).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(payload, expected)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _decode_receipt_json(payload: bytes) -> object:
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_nonstandard_json_constant,
    )


def _decode_manifest_json(payload: bytes, *, source: str) -> object:
    """Decode untrusted manifest JSON without erasing ambiguous input."""

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except _DuplicateJsonKeyError as exc:
        raise CommunityTestEvidenceError(
            f"evidence_v2.manifest_duplicate_key:{source}"
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CommunityTestEvidenceError(
            f"evidence_v2.manifest_invalid_json:{source}"
        ) from exc


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting evidence ledger")
        view = view[written:]


def _is_reparse_stat(value: os.stat_result) -> bool:
    """Detect POSIX symlinks and Windows reparse points/junctions."""

    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse_flag)


def _assert_no_reparse_chain(path: Path, *, allow_missing: bool = True) -> None:
    """Reject reparse points in every existing component of ``path``.

    The check is repeated before and after every security-sensitive handle
    operation. It intentionally uses ``lstat`` and never ``resolve`` so the
    allowlisted root itself cannot silently become an external target.
    """

    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    parts = [absolute, *absolute.parents]
    for component in reversed(parts):
        try:
            current = os.lstat(component)
        except FileNotFoundError:
            if allow_missing:
                continue
            raise CommunityTestEvidenceError(
                f"evidence_v2.secure_path_missing:{component}"
            ) from None
        except OSError as exc:
            raise CommunityTestEvidenceError(
                f"evidence_v2.secure_path_unreadable:{component}"
            ) from exc
        if _is_reparse_stat(current):
            raise CommunityTestEvidenceError(
                f"evidence_v2.reparse_point_forbidden:{component}"
            )


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
        int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000))),
    )


def _read_regular_file_secure(
    path: Path,
    *,
    max_bytes: int,
    missing_code: str,
    invalid_code: str,
) -> bytes:
    """Read a bounded regular file from one stable, no-follow handle."""

    _assert_no_reparse_chain(path, allow_missing=True)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise CommunityTestEvidenceError(missing_code) from None
    except OSError as exc:
        raise CommunityTestEvidenceError(invalid_code) from exc
    if _is_reparse_stat(before) or not stat.S_ISREG(before.st_mode):
        raise CommunityTestEvidenceError(invalid_code)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise CommunityTestEvidenceError(missing_code) from None
    except OSError as exc:
        raise CommunityTestEvidenceError(invalid_code) from exc
    try:
        opened = os.fstat(fd)
        if (
            _is_reparse_stat(opened)
            or not stat.S_ISREG(opened.st_mode)
            or (int(opened.st_dev), int(opened.st_ino))
            != (int(before.st_dev), int(before.st_ino))
        ):
            raise CommunityTestEvidenceError(
                "evidence_v2.secure_file_changed_before_open"
            )
        if opened.st_size > max_bytes:
            raise CommunityTestEvidenceError(
                f"evidence_v2.secure_file_too_large:{max_bytes}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise CommunityTestEvidenceError(
                    f"evidence_v2.secure_file_too_large:{max_bytes}"
                )
        after = os.fstat(fd)
        if _file_identity(opened) != _file_identity(after):
            raise CommunityTestEvidenceError(
                "evidence_v2.secure_file_changed_during_read"
            )
    finally:
        os.close(fd)
    try:
        final_path = os.lstat(path)
    except OSError as exc:
        raise CommunityTestEvidenceError(
            "evidence_v2.secure_file_path_changed"
        ) from exc
    _assert_no_reparse_chain(path, allow_missing=False)
    if _file_identity(before) != _file_identity(final_path):
        raise CommunityTestEvidenceError("evidence_v2.secure_file_path_changed")
    return b"".join(chunks)


def _ensure_secure_directory(path: Path) -> None:
    _assert_no_reparse_chain(path, allow_missing=True)
    path.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_chain(path, allow_missing=False)
    try:
        current = os.lstat(path)
    except OSError as exc:
        raise CommunityTestEvidenceError(
            "evidence_v2.secure_directory_invalid"
        ) from exc
    if _is_reparse_stat(current) or not stat.S_ISDIR(current.st_mode):
        raise CommunityTestEvidenceError("evidence_v2.secure_directory_invalid")


def _community_version() -> str:
    try:
        return metadata.version("okto-pulse")
    except metadata.PackageNotFoundError:
        return "0.3.1"


def _resolve_manifest_path(
    manifest_ref: str, *, manifest_root: Path
) -> tuple[Path, str]:
    """Resolve only canonical relative JSON refs inside ``manifest_root``."""

    if not isinstance(manifest_ref, str) or not manifest_ref.strip():
        raise CommunityTestEvidenceError("evidence_v2.manifest_ref_required")
    raw = manifest_ref.strip()
    if (
        raw.startswith(("/", "\\"))
        or "\\" in raw
        or ":" in raw
        or re.match(r"^[A-Za-z]:", raw)
    ):
        raise CommunityTestEvidenceError("evidence_v2.manifest_ref_must_be_relative")
    relative = PurePosixPath(raw)
    if (
        relative.is_absolute()
        or relative.as_posix() != raw
        or any(
            part in {"", ".", ".."} or part.endswith((" ", "."))
            for part in relative.parts
        )
    ):
        raise CommunityTestEvidenceError("evidence_v2.manifest_ref_path_traversal")
    if relative.suffix.lower() != ".json":
        raise CommunityTestEvidenceError("evidence_v2.manifest_ref_json_required")

    root = Path(os.path.abspath(os.fspath(manifest_root.expanduser())))
    _assert_no_reparse_chain(root, allow_missing=False)
    candidate = root.joinpath(*relative.parts)
    _assert_no_reparse_chain(candidate, allow_missing=True)
    return candidate, relative.as_posix()


def _manifest_bytes(manifest_ref: str, *, manifest_root: Path) -> tuple[bytes, str]:
    path, canonical_ref = _resolve_manifest_path(
        manifest_ref, manifest_root=manifest_root
    )
    try:
        payload = _read_regular_file_secure(
            path,
            max_bytes=COMMUNITY_MAX_MANIFEST_BYTES,
            missing_code=f"evidence_v2.manifest_not_found:{manifest_ref}",
            invalid_code="evidence_v2.manifest_secure_file_invalid",
        )
    except CommunityTestEvidenceError as exc:
        if str(exc).startswith("evidence_v2.secure_file_too_large"):
            raise CommunityTestEvidenceError("evidence_v2.manifest_too_large") from exc
        raise
    return payload, canonical_ref


def manifest_sha256(manifest: bytes) -> str:
    return f"sha256:{hashlib.sha256(manifest).hexdigest()}"


def _validate_http_path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise CommunityTestEvidenceError("evidence_v2.manifest_step_path_invalid")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or value.startswith("//"):
        raise CommunityTestEvidenceError("evidence_v2.manifest_step_path_invalid")
    if "\\" in value or ".." in Path(parsed.path).parts:
        raise CommunityTestEvidenceError("evidence_v2.manifest_step_path_invalid")
    return value


def validate_replay_manifest(
    decoded: object,
    *,
    board_id: str | None = None,
    spec_id: str | None = None,
    scenario_id: str | None = None,
    scenario_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize the one supported, non-programmable manifest."""

    if not isinstance(decoded, Mapping):
        raise CommunityTestEvidenceError("evidence_v2.manifest_object_required")
    root = dict(decoded)
    unexpected = set(root) - {
        "schema_version",
        "description",
        "purpose",
        "board_id",
        "spec_id",
        "scenario_id",
        "scenario_sha256",
        "steps",
    }
    if unexpected:
        raise CommunityTestEvidenceError(
            "evidence_v2.manifest_unexpected_fields:" + ",".join(sorted(unexpected))
        )
    if root.get("schema_version") != COMMUNITY_MANIFEST_SCHEMA:
        raise CommunityTestEvidenceError("evidence_v2.manifest_schema_invalid")
    if root.get("purpose") != COMMUNITY_MANIFEST_PURPOSE:
        raise CommunityTestEvidenceError("evidence_v2.manifest_purpose_invalid")
    description = root.get("description", "")
    if not isinstance(description, str) or len(description) > 4096:
        raise CommunityTestEvidenceError("evidence_v2.manifest_description_invalid")
    for field in ("board_id", "spec_id", "scenario_id"):
        value = root.get(field)
        if not isinstance(value, str) or not value.strip():
            raise CommunityTestEvidenceError(f"evidence_v2.manifest_{field}_required")
    manifest_scenario_sha256 = root.get("scenario_sha256")
    if not isinstance(manifest_scenario_sha256, str) or not _SHA256_RE.fullmatch(
        manifest_scenario_sha256
    ):
        raise CommunityTestEvidenceError("evidence_v2.manifest_scenario_sha256_invalid")
    for field, expected in (
        ("board_id", board_id),
        ("spec_id", spec_id),
        ("scenario_id", scenario_id),
        ("scenario_sha256", scenario_sha256),
    ):
        if expected is not None and root.get(field) != expected:
            raise CommunityTestEvidenceError(
                f"evidence_v2.manifest_{field}_binding_mismatch"
            )
    steps = root.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 50:
        raise CommunityTestEvidenceError("evidence_v2.manifest_steps_invalid")

    normalized_steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps):
        if not isinstance(raw_step, Mapping):
            raise CommunityTestEvidenceError(
                f"evidence_v2.manifest_step[{index}]_object_required"
            )
        step = dict(raw_step)
        unexpected_step = set(step) - {
            "name",
            "path",
            "expected_status",
            "assertions",
        }
        if unexpected_step:
            raise CommunityTestEvidenceError(
                f"evidence_v2.manifest_step[{index}]_unexpected_fields:"
                + ",".join(sorted(unexpected_step))
            )
        name = step.get("name")
        expected_status = step.get("expected_status")
        assertions = step.get("assertions", [])
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise CommunityTestEvidenceError(
                f"evidence_v2.manifest_step[{index}]_name_invalid"
            )
        if (
            not isinstance(expected_status, int)
            or isinstance(expected_status, bool)
            or not 100 <= expected_status <= 599
        ):
            raise CommunityTestEvidenceError(
                f"evidence_v2.manifest_step[{index}]_expected_status_invalid"
            )
        if not isinstance(assertions, list) or not 1 <= len(assertions) <= 50:
            raise CommunityTestEvidenceError(
                f"evidence_v2.manifest_step[{index}]_assertions_invalid"
            )
        normalized_assertions: list[dict[str, Any]] = []
        for assertion_index, raw_assertion in enumerate(assertions):
            if not isinstance(raw_assertion, Mapping):
                raise CommunityTestEvidenceError(
                    f"evidence_v2.manifest_step[{index}].assertion[{assertion_index}]_object_required"
                )
            assertion = dict(raw_assertion)
            if set(assertion) - {"name", "kind", "path", "expected"}:
                raise CommunityTestEvidenceError(
                    f"evidence_v2.manifest_step[{index}].assertion[{assertion_index}]_schema_invalid"
                )
            assertion_name = assertion.get("name")
            kind = assertion.get("kind")
            if not isinstance(assertion_name, str) or not assertion_name.strip():
                raise CommunityTestEvidenceError(
                    f"evidence_v2.manifest_step[{index}].assertion[{assertion_index}]_name_invalid"
                )
            if (
                kind not in {"json_equals", "body_contains"}
                or "expected" not in assertion
            ):
                raise CommunityTestEvidenceError(
                    f"evidence_v2.manifest_step[{index}].assertion[{assertion_index}]_kind_invalid"
                )
            if kind == "json_equals" and (
                not isinstance(assertion.get("path"), str)
                or not assertion["path"].strip()
            ):
                raise CommunityTestEvidenceError(
                    f"evidence_v2.manifest_step[{index}].assertion[{assertion_index}]_path_invalid"
                )
            if kind == "body_contains" and not isinstance(
                assertion.get("expected"), str
            ):
                raise CommunityTestEvidenceError(
                    f"evidence_v2.manifest_step[{index}].assertion[{assertion_index}]_expected_invalid"
                )
            normalized_assertions.append(assertion)
        normalized_steps.append(
            {
                "name": name.strip(),
                "path": _validate_http_path(step.get("path")),
                "expected_status": expected_status,
                "assertions": normalized_assertions,
            }
        )
    return {
        "schema_version": COMMUNITY_MANIFEST_SCHEMA,
        "description": description,
        "purpose": COMMUNITY_MANIFEST_PURPOSE,
        "board_id": str(root["board_id"]),
        "spec_id": str(root["spec_id"]),
        "scenario_id": str(root["scenario_id"]),
        "scenario_sha256": str(root["scenario_sha256"]),
        "steps": normalized_steps,
    }


def build_inline_replay_manifest(
    decoded: object,
    *,
    board_id: str,
    spec_id: str,
    scenario_id: str,
    scenario_sha256: str,
) -> dict[str, Any]:
    """Bind an MCP-only step description to trusted server context."""

    if not isinstance(decoded, Mapping):
        raise CommunityTestEvidenceError("evidence_v2.inline_replay_object_required")
    replay = dict(decoded)
    unexpected = set(replay) - {"description", "steps"}
    if unexpected:
        raise CommunityTestEvidenceError(
            "evidence_v2.inline_replay_unexpected_fields:"
            + ",".join(sorted(unexpected))
        )
    return validate_replay_manifest(
        {
            "schema_version": COMMUNITY_MANIFEST_SCHEMA,
            "description": replay.get("description", ""),
            "purpose": COMMUNITY_MANIFEST_PURPOSE,
            "board_id": board_id,
            "spec_id": spec_id,
            "scenario_id": scenario_id,
            "scenario_sha256": scenario_sha256,
            "steps": replay.get("steps"),
        },
        board_id=board_id,
        spec_id=spec_id,
        scenario_id=scenario_id,
        scenario_sha256=scenario_sha256,
    )


def _read_json_path(value: object, path: str) -> object:
    current = value
    for token in path.split("."):
        if isinstance(current, Mapping) and token in current:
            current = current[token]
        elif (
            isinstance(current, list) and token.isdigit() and int(token) < len(current)
        ):
            current = current[int(token)]
        else:
            return None
    return current


class CommunityHttpManifestExecutor:
    """Execute strict GET-only manifests against the live local Pulse API."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        transport: Any | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname or ""
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname.lower() == "localhost"
        if parsed.scheme not in {"http", "https"} or not loopback:
            raise CommunityTestEvidenceError(
                "evidence_v2.runtime_base_url_not_loopback"
            )
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def __call__(
        self, manifest: Mapping[str, Any], manifest_ref: str
    ) -> ProductExecutionObservation:
        del manifest_ref
        import httpx

        assertions: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            for step in manifest["steps"]:
                response = await client.get(step["path"])
                expected_status = step["expected_status"]
                status_ok = response.status_code == expected_status
                assertions.append(
                    {
                        "name": f"{step['name']}.http_status",
                        "expected": expected_status,
                        "observed": response.status_code,
                        "status": "passed" if status_ok else "failed",
                    }
                )
                parsed_json: object = None
                json_loaded = False
                for assertion in step["assertions"]:
                    expected = assertion["expected"]
                    if assertion["kind"] == "body_contains":
                        matched = expected in response.text
                        observed: object = expected if matched else response.text[:500]
                    else:
                        if not json_loaded:
                            try:
                                parsed_json = response.json()
                            except (ValueError, json.JSONDecodeError):
                                parsed_json = None
                            json_loaded = True
                        observed = _read_json_path(parsed_json, assertion["path"])
                        matched = _canonical_json(observed) == _canonical_json(expected)
                    assertions.append(
                        {
                            "name": f"{step['name']}.{assertion['name']}",
                            "expected": expected,
                            "observed": observed,
                            "status": "passed" if matched else "failed",
                        }
                    )
        outcome = (
            "passed"
            if all(item["status"] == "passed" for item in assertions)
            else "failed"
        )
        return ProductExecutionObservation(
            run_id=f"http-{secrets.token_hex(16)}",
            outcome=outcome,
            assertions=tuple(assertions),
            executed_at=datetime.now(timezone.utc).isoformat(),
        )


class CommunityEvidenceLedger:
    """Installation-local immutable receipt ledger authenticated by HMAC."""

    def __init__(self, *, evidence_root: Path) -> None:
        self.evidence_root = Path(
            os.path.abspath(os.fspath(evidence_root.expanduser()))
        )
        self.manifest_root = self.evidence_root / "manifests"
        self.receipt_root = self.evidence_root / "receipts"
        self.secret_path = self.evidence_root / "receipt.key"
        # A cache hit still requires a stable directory scan. It only avoids
        # reparsing and re-HMACing every immutable receipt when every entry's
        # name, identity, size and timestamps remain identical.
        self._validated_history_cache: (
            tuple[
                str,
                tuple[tuple[str, tuple[int, int, int, int, int]], ...],
            ]
            | None
        ) = None

    def _assert_secure_layout(self) -> None:
        _assert_no_reparse_chain(self.evidence_root, allow_missing=True)
        for path in (self.manifest_root, self.receipt_root, self.secret_path):
            _assert_no_reparse_chain(path, allow_missing=True)

    def _receipt_history_fingerprint(
        self, *, allow_missing: bool
    ) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
        """Enumerate a stable, bounded, canonical receipt directory."""

        _assert_no_reparse_chain(self.receipt_root, allow_missing=allow_missing)
        try:
            before = os.lstat(self.receipt_root)
        except FileNotFoundError:
            if allow_missing:
                return ()
            raise CommunityTestEvidenceError(
                "evidence_v2.receipt_ledger_missing"
            ) from None
        except OSError as exc:
            raise CommunityTestEvidenceError(
                "evidence_v2.receipt_ledger_corrupt"
            ) from exc
        if _is_reparse_stat(before) or not stat.S_ISDIR(before.st_mode):
            raise CommunityTestEvidenceError("evidence_v2.receipt_ledger_corrupt")

        entries: list[tuple[str, tuple[int, int, int, int, int]]] = []
        try:
            with os.scandir(self.receipt_root) as directory:
                for entry in directory:
                    if len(entries) >= COMMUNITY_MAX_LEDGER_RECEIPTS:
                        raise CommunityTestEvidenceError(
                            "evidence_v2.receipt_ledger_entry_limit_exceeded"
                        )
                    if not _RECEIPT_FILE_RE.fullmatch(entry.name):
                        raise CommunityTestEvidenceError(
                            "evidence_v2.receipt_ledger_noncanonical_entry"
                        )
                    current = entry.stat(follow_symlinks=False)
                    if _is_reparse_stat(current) or not stat.S_ISREG(current.st_mode):
                        raise CommunityTestEvidenceError(
                            "evidence_v2.receipt_ledger_nonregular_entry"
                        )
                    entries.append((entry.name, _file_identity(current)))
        except CommunityTestEvidenceError:
            raise
        except OSError as exc:
            raise CommunityTestEvidenceError(
                "evidence_v2.receipt_ledger_corrupt"
            ) from exc

        try:
            after = os.lstat(self.receipt_root)
        except OSError as exc:
            raise CommunityTestEvidenceError(
                "evidence_v2.receipt_ledger_changed_during_scan"
            ) from exc
        _assert_no_reparse_chain(self.receipt_root, allow_missing=False)
        if _file_identity(before) != _file_identity(after):
            raise CommunityTestEvidenceError(
                "evidence_v2.receipt_ledger_changed_during_scan"
            )
        return tuple(sorted(entries))

    @staticmethod
    def _receipt_record_schema_valid(
        record: Mapping[str, Any], *, allow_legacy: bool = False
    ) -> bool:
        record_keys = set(record)
        legacy_non_authoritative = record_keys == _LEGACY_RECEIPT_RECORD_KEYS
        if record_keys != _RECEIPT_RECORD_KEYS and not (
            allow_legacy and legacy_non_authoritative
        ):
            return False
        if record.get("schema_version") != COMMUNITY_RECEIPT_SCHEMA:
            return False
        if not isinstance(
            record.get("receipt_id"), str
        ) or not _RECEIPT_ID_RE.fullmatch(record["receipt_id"]):
            return False
        issued_at = record.get("issued_at")
        if not isinstance(issued_at, str):
            return False
        try:
            parsed_issued_at = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed_issued_at.tzinfo is None or parsed_issued_at.utcoffset() is None:
            return False
        for field in (
            "board_id",
            "spec_id",
            "scenario_id",
            "actor_id",
            "manifest_ref",
            "run_id",
        ):
            if not isinstance(record.get(field), str) or not record[field].strip():
                return False
        if record.get("status") not in {"passed", "automated", "failed"}:
            return False
        digest_fields = [
            "manifest_sha256",
            "attestation_sha256",
            "evidence_sha256",
        ]
        if not legacy_non_authoritative:
            digest_fields.append("scenario_sha256")
        for field in digest_fields:
            if not isinstance(record.get(field), str) or not _SHA256_RE.fullmatch(
                record[field]
            ):
                return False
        if not isinstance(record.get("secret_key_id"), str) or not _KEY_ID_RE.fullmatch(
            record["secret_key_id"]
        ):
            return False
        return isinstance(record.get("signature"), str) and bool(
            _SIGNATURE_RE.fullmatch(record["signature"])
        )

    def _validate_existing_receipts(
        self, key: bytes
    ) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
        """Authenticate the complete immutable history before appending.

        A signed pre-hardening record may prove only key continuity here. It is
        explicitly legacy/non-authoritative and is still rejected by
        :meth:`verify`, which never enables ``allow_legacy``.
        """

        key_fingerprint = hashlib.sha256(key).hexdigest()
        key_id = key_fingerprint[:16]
        fingerprint = self._receipt_history_fingerprint(allow_missing=False)
        if self._validated_history_cache == (key_fingerprint, fingerprint):
            return fingerprint

        seen_ids: set[str] = set()
        rotation_detected = False
        first_error: CommunityTestEvidenceError | None = None
        for filename, _identity in fingerprint:
            path = self.receipt_root / filename
            try:
                raw = _read_regular_file_secure(
                    path,
                    max_bytes=64 * 1024,
                    missing_code="evidence_v2.receipt_ledger_changed_during_scan",
                    invalid_code="evidence_v2.receipt_ledger_corrupt",
                )
            except (CommunityTestEvidenceError,) as exc:
                if first_error is None:
                    first_error = CommunityTestEvidenceError(
                        "evidence_v2.receipt_ledger_corrupt"
                    )
                    first_error.__cause__ = exc
                continue
            try:
                record = _decode_receipt_json(raw)
            except _DuplicateJsonKeyError:
                if first_error is None:
                    first_error = CommunityTestEvidenceError(
                        "evidence_v2.receipt_ledger_duplicate_key"
                    )
                continue
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                if first_error is None:
                    first_error = CommunityTestEvidenceError(
                        "evidence_v2.receipt_ledger_corrupt"
                    )
                    first_error.__cause__ = exc
                continue
            if not isinstance(record, dict) or not self._receipt_record_schema_valid(
                record, allow_legacy=True
            ):
                if first_error is None:
                    first_error = CommunityTestEvidenceError(
                        "evidence_v2.receipt_ledger_schema_invalid"
                    )
                continue
            if not _is_canonical_json_bytes(raw, record):
                if first_error is None:
                    first_error = CommunityTestEvidenceError(
                        "evidence_v2.receipt_ledger_noncanonical_record"
                    )
            receipt_id = str(record["receipt_id"])
            if f"{receipt_id}.json" != filename:
                if first_error is None:
                    first_error = CommunityTestEvidenceError(
                        "evidence_v2.receipt_ledger_filename_mismatch"
                    )
                continue
            if receipt_id in seen_ids:
                if first_error is None:
                    first_error = CommunityTestEvidenceError(
                        "evidence_v2.receipt_ledger_duplicate_id"
                    )
                continue
            seen_ids.add(receipt_id)
            if record.get("secret_key_id") != key_id:
                rotation_detected = True
                continue
            expected_signature = self._sign(record, key)
            if not hmac.compare_digest(expected_signature, str(record["signature"])):
                if first_error is None:
                    first_error = CommunityTestEvidenceError(
                        "evidence_v2.receipt_ledger_signature_invalid"
                    )

        # A changed key is more diagnostic than an injected malformed shadow
        # entry and must never be hidden by lexicographic ordering.
        if rotation_detected:
            raise CommunityTestEvidenceError("evidence_v2.receipt_secret_rotated")
        if first_error is not None:
            raise first_error
        final_fingerprint = self._receipt_history_fingerprint(allow_missing=False)
        if final_fingerprint != fingerprint:
            raise CommunityTestEvidenceError(
                "evidence_v2.receipt_ledger_changed_during_validation"
            )
        self._validated_history_cache = (key_fingerprint, final_fingerprint)
        return final_fingerprint

    def _secret(self, *, create: bool) -> bytes:
        self._assert_secure_layout()
        try:
            value = _read_regular_file_secure(
                self.secret_path,
                max_bytes=32,
                missing_code="evidence_v2.receipt_secret_missing",
                invalid_code="evidence_v2.receipt_secret_invalid",
            )
        except CommunityTestEvidenceError as exc:
            if str(exc) != "evidence_v2.receipt_secret_missing":
                raise
            if not create:
                raise
            if self._receipt_history_fingerprint(allow_missing=True):
                raise CommunityTestEvidenceError("evidence_v2.receipt_secret_missing")
            _ensure_secure_directory(self.evidence_root)
            value = secrets.token_bytes(32)
            try:
                fd = os.open(
                    self.secret_path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except FileExistsError:
                value = _read_regular_file_secure(
                    self.secret_path,
                    max_bytes=32,
                    missing_code="evidence_v2.receipt_secret_missing",
                    invalid_code="evidence_v2.receipt_secret_invalid",
                )
            else:
                try:
                    _write_all(fd, value)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                persisted = _read_regular_file_secure(
                    self.secret_path,
                    max_bytes=32,
                    missing_code="evidence_v2.receipt_secret_missing",
                    invalid_code="evidence_v2.receipt_secret_invalid",
                )
                if not hmac.compare_digest(value, persisted):
                    raise CommunityTestEvidenceError(
                        "evidence_v2.receipt_secret_changed_during_create"
                    )
        if len(value) != 32:
            raise CommunityTestEvidenceError("evidence_v2.receipt_secret_invalid")
        try:
            secret_stat = os.lstat(self.secret_path)
        except OSError as exc:
            raise CommunityTestEvidenceError(
                "evidence_v2.receipt_secret_invalid"
            ) from exc
        if int(getattr(secret_stat, "st_nlink", 1)) != 1:
            raise CommunityTestEvidenceError(
                "evidence_v2.receipt_secret_hardlink_forbidden"
            )
        if os.name != "nt" and stat.S_IMODE(secret_stat.st_mode) & 0o077:
            raise CommunityTestEvidenceError(
                "evidence_v2.receipt_secret_permissions_invalid"
            )
        return value

    @staticmethod
    def _sign(record: Mapping[str, Any], key: bytes) -> str:
        unsigned = {
            name: value for name, value in record.items() if name != "signature"
        }
        return hmac.new(
            key,
            _canonical_json(unsigned).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def issue(
        self,
        *,
        board_id: str,
        spec_id: str,
        scenario_id: str,
        scenario_sha256: str,
        status: str,
        actor_id: str,
        evidence: Mapping[str, Any],
    ) -> str:
        key = self._secret(create=True)
        key_fingerprint = hashlib.sha256(key).hexdigest()
        key_id = key_fingerprint[:16]
        self._assert_secure_layout()
        _ensure_secure_directory(self.receipt_root)
        history_fingerprint = self._validate_existing_receipts(key)
        attestation = _plain(evidence.get("execution_attestation")) or {}
        unsigned_evidence = {
            name: value
            for name, value in evidence.items()
            if name != "execution_receipt"
        }
        for _attempt in range(5):
            receipt_id = secrets.token_hex(16)
            record: dict[str, Any] = {
                "schema_version": COMMUNITY_RECEIPT_SCHEMA,
                "receipt_id": receipt_id,
                "issued_at": datetime.now(timezone.utc).isoformat(),
                "board_id": board_id,
                "spec_id": spec_id,
                "scenario_id": scenario_id,
                "scenario_sha256": scenario_sha256,
                "status": status,
                "actor_id": actor_id,
                "manifest_ref": evidence.get("manifest_ref"),
                "manifest_sha256": attestation.get("manifest_sha256"),
                "attestation_sha256": attestation.get("attestation_sha256"),
                "run_id": attestation.get("run_id"),
                "evidence_sha256": _sha256_json(unsigned_evidence),
                "secret_key_id": key_id,
            }
            signature = self._sign(record, key)
            record["signature"] = signature
            target = self.receipt_root / f"{receipt_id}.json"
            try:
                fd = os.open(
                    target,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
            except FileExistsError:
                continue
            try:
                payload = _canonical_json(record).encode("utf-8")
                _write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            persisted = _read_regular_file_secure(
                target,
                max_bytes=64 * 1024,
                missing_code="evidence_v2.receipt_persist_failed",
                invalid_code="evidence_v2.receipt_persist_failed",
            )
            if not hmac.compare_digest(payload, persisted):
                raise CommunityTestEvidenceError(
                    "evidence_v2.receipt_changed_during_persist"
                )
            try:
                target_identity = _file_identity(os.lstat(target))
            except OSError:
                self._validated_history_cache = None
            else:
                updated_fingerprint = tuple(
                    sorted((*history_fingerprint, (target.name, target_identity)))
                )
                self._validated_history_cache = (
                    key_fingerprint,
                    updated_fingerprint,
                )
            return f"ev2r.{receipt_id}.{signature}"
        raise CommunityTestEvidenceError("evidence_v2.receipt_id_collision")

    def verify(
        self,
        *,
        board_id: str,
        spec_id: str,
        scenario_id: str,
        scenario_sha256: str,
        status: str,
        actor_id: str | None,
        evidence: Mapping[str, Any],
    ) -> tuple[str, ...]:
        receipt = evidence.get("execution_receipt")
        match = _RECEIPT_RE.fullmatch(receipt) if isinstance(receipt, str) else None
        if match is None:
            return ("evidence_v2.execution_receipt_invalid",)
        receipt_id, supplied_signature = match.groups()
        target = self.receipt_root / f"{receipt_id}.json"
        try:
            raw = _read_regular_file_secure(
                target,
                max_bytes=64 * 1024,
                missing_code="evidence_v2.receipt_not_registered",
                invalid_code="evidence_v2.receipt_ledger_corrupt",
            )
        except CommunityTestEvidenceError as exc:
            return (str(exc),)
        try:
            record = _decode_receipt_json(raw)
        except _DuplicateJsonKeyError:
            return ("evidence_v2.receipt_ledger_duplicate_key",)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return ("evidence_v2.receipt_ledger_corrupt",)
        if not isinstance(record, dict) or not self._receipt_record_schema_valid(
            record
        ):
            return ("evidence_v2.receipt_ledger_schema_invalid",)
        if not _is_canonical_json_bytes(raw, record):
            return ("evidence_v2.receipt_ledger_noncanonical_record",)
        try:
            key = self._secret(create=False)
        except CommunityTestEvidenceError as exc:
            return (str(exc),)
        expected_signature = self._sign(record, key)
        stored_signature = record.get("signature")
        if record.get("secret_key_id") != hashlib.sha256(key).hexdigest()[:16]:
            return ("evidence_v2.receipt_secret_rotated",)
        if not (
            isinstance(stored_signature, str)
            and hmac.compare_digest(supplied_signature, stored_signature)
            and hmac.compare_digest(expected_signature, stored_signature)
        ):
            return ("evidence_v2.receipt_signature_invalid",)
        reasons: list[str] = []
        if record.get("receipt_id") != receipt_id:
            reasons.append("evidence_v2.receipt_id_binding_mismatch")
        for field, expected in (
            ("board_id", board_id),
            ("spec_id", spec_id),
            ("scenario_id", scenario_id),
            ("scenario_sha256", scenario_sha256),
            ("status", status),
        ):
            if record.get(field) != expected:
                reasons.append(f"evidence_v2.receipt_{field}_binding_mismatch")
        if actor_id is not None and record.get("actor_id") != actor_id:
            reasons.append("evidence_v2.receipt_actor_id_binding_mismatch")
        attestation = _plain(evidence.get("execution_attestation")) or {}
        for field, expected in (
            ("manifest_ref", evidence.get("manifest_ref")),
            ("manifest_sha256", attestation.get("manifest_sha256")),
            ("attestation_sha256", attestation.get("attestation_sha256")),
            ("run_id", attestation.get("run_id")),
        ):
            if record.get(field) != expected:
                reasons.append(f"evidence_v2.receipt_{field}_mismatch")
        unsigned_evidence = {
            name: value
            for name, value in evidence.items()
            if name != "execution_receipt"
        }
        if record.get("evidence_sha256") != _sha256_json(unsigned_evidence):
            reasons.append("evidence_v2.receipt_evidence_tampered")
        return tuple(reasons)


def _fsync_directory(path: Path) -> None:
    """Durably publish a directory entry where the platform supports it."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _persist_inline_manifest(
    manifest: Mapping[str, Any], *, ledger: CommunityEvidenceLedger
) -> tuple[bytes, str]:
    """Atomically persist canonical content-addressed manifest bytes.

    A hard-link publish is used as a no-clobber atomic primitive. Concurrent
    identical requests converge on the same bytes; an existing different or
    unsafe target is never overwritten.
    """

    try:
        payload = _canonical_json(manifest).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CommunityTestEvidenceError(
            "evidence_v2.inline_replay_not_canonical_json"
        ) from exc
    if len(payload) > COMMUNITY_MAX_MANIFEST_BYTES:
        raise CommunityTestEvidenceError("evidence_v2.manifest_too_large")

    digest = hashlib.sha256(payload).hexdigest()
    manifest_ref = f"inline-{digest}.json"
    _ensure_secure_directory(ledger.manifest_root)
    target, canonical_ref = _resolve_manifest_path(
        manifest_ref, manifest_root=ledger.manifest_root
    )

    def existing_bytes() -> bytes | None:
        try:
            return _read_regular_file_secure(
                target,
                max_bytes=COMMUNITY_MAX_MANIFEST_BYTES,
                missing_code="evidence_v2.inline_manifest_missing",
                invalid_code="evidence_v2.inline_manifest_target_invalid",
            )
        except CommunityTestEvidenceError as exc:
            if str(exc) == "evidence_v2.inline_manifest_missing":
                return None
            raise

    existing = existing_bytes()
    if existing is not None:
        if not hmac.compare_digest(existing, payload):
            raise CommunityTestEvidenceError(
                "evidence_v2.inline_manifest_content_conflict"
            )
        if int(getattr(os.lstat(target), "st_nlink", 1)) != 1:
            raise CommunityTestEvidenceError(
                "evidence_v2.inline_manifest_hardlink_forbidden"
            )
        return existing, canonical_ref

    temporary = ledger.manifest_root / (f".{manifest_ref}.{secrets.token_hex(16)}.tmp")
    _assert_no_reparse_chain(temporary, allow_missing=True)
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except OSError as exc:
        raise CommunityTestEvidenceError(
            "evidence_v2.inline_manifest_temp_create_failed"
        ) from exc
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)

    published = False
    try:
        persisted_temp = _read_regular_file_secure(
            temporary,
            max_bytes=COMMUNITY_MAX_MANIFEST_BYTES,
            missing_code="evidence_v2.inline_manifest_temp_missing",
            invalid_code="evidence_v2.inline_manifest_temp_invalid",
        )
        if not hmac.compare_digest(persisted_temp, payload):
            raise CommunityTestEvidenceError("evidence_v2.inline_manifest_temp_changed")
        _assert_no_reparse_chain(target, allow_missing=True)
        try:
            os.link(temporary, target, follow_symlinks=False)
            published = True
        except FileExistsError:
            existing = existing_bytes()
            if existing is None or not hmac.compare_digest(existing, payload):
                raise CommunityTestEvidenceError(
                    "evidence_v2.inline_manifest_content_conflict"
                ) from None
        except OSError as exc:
            raise CommunityTestEvidenceError(
                "evidence_v2.inline_manifest_publish_failed"
            ) from exc
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CommunityTestEvidenceError(
                "evidence_v2.inline_manifest_temp_cleanup_failed"
            ) from exc

    _fsync_directory(ledger.manifest_root)
    final = existing_bytes()
    if final is None or not hmac.compare_digest(final, payload):
        raise CommunityTestEvidenceError("evidence_v2.inline_manifest_persist_failed")
    try:
        target_stat = os.lstat(target)
    except OSError as exc:
        raise CommunityTestEvidenceError(
            "evidence_v2.inline_manifest_persist_failed"
        ) from exc
    if _is_reparse_stat(target_stat) or not stat.S_ISREG(target_stat.st_mode):
        raise CommunityTestEvidenceError("evidence_v2.inline_manifest_target_invalid")
    if int(getattr(target_stat, "st_nlink", 1)) != 1:
        raise CommunityTestEvidenceError(
            "evidence_v2.inline_manifest_hardlink_forbidden"
        )
    if published and manifest_sha256(final) != f"sha256:{digest}":
        raise CommunityTestEvidenceError("evidence_v2.inline_manifest_digest_mismatch")
    return final, canonical_ref


def _build_evidence_v2(
    *,
    manifest_ref: str,
    manifest: bytes,
    scenario_id: str,
    scenario_sha256: str,
    observation: ProductExecutionObservation,
    environment: str,
) -> dict[str, Any]:
    if not scenario_id.strip():
        raise CommunityTestEvidenceError("evidence_v2.scenario_id_required")
    executed_at = observation.executed_at or datetime.now(timezone.utc).isoformat()
    attestation: dict[str, Any] = {
        "schema_version": EVIDENCE_V2_SCHEMA_VERSION,
        "run_id": observation.run_id,
        "executed_at": executed_at,
        "scenario_id": scenario_id,
        "scenario_sha256": scenario_sha256,
        "outcome": observation.outcome,
        "product_runtime_exercised": True,
        "manifest_sha256": manifest_sha256(manifest),
        "assertions": [dict(assertion) for assertion in observation.assertions],
        "provenance": {
            "producer": COMMUNITY_EVIDENCE_PRODUCER,
            "producer_version": _community_version(),
            "adapter": COMMUNITY_EVIDENCE_ADAPTER,
            "environment": environment,
        },
    }
    attestation["attestation_sha256"] = compute_execution_attestation_sha256(
        attestation, manifest_ref=manifest_ref
    )
    return {
        "evidence_class": "mcp_replay_manifest",
        "manifest_ref": manifest_ref,
        "execution_attestation": attestation,
    }


async def run_manifest_and_build_evidence_v2(
    *,
    manifest_ref: str,
    board_id: str,
    spec_id: str,
    scenario_id: str,
    scenario_sha256: str,
    status: str,
    actor_id: str,
    executor: RuntimeExecutor,
    ledger: CommunityEvidenceLedger,
    environment: str = "local",
) -> dict[str, Any]:
    """Execute a valid real-runtime manifest, then durably issue its receipt."""

    manifest, canonical_ref = _manifest_bytes(
        manifest_ref, manifest_root=ledger.manifest_root
    )
    decoded = _decode_manifest_json(manifest, source=manifest_ref)
    normalized_manifest = validate_replay_manifest(
        decoded,
        board_id=board_id,
        spec_id=spec_id,
        scenario_id=scenario_id,
        scenario_sha256=scenario_sha256,
    )
    return await _execute_validated_manifest_and_build_evidence_v2(
        manifest=manifest,
        canonical_ref=canonical_ref,
        normalized_manifest=normalized_manifest,
        board_id=board_id,
        spec_id=spec_id,
        scenario_id=scenario_id,
        scenario_sha256=scenario_sha256,
        status=status,
        actor_id=actor_id,
        executor=executor,
        ledger=ledger,
        environment=environment,
    )


async def run_inline_replay_and_build_evidence_v2(
    *,
    inline_replay: object,
    board_id: str,
    spec_id: str,
    scenario_id: str,
    scenario_sha256: str,
    status: str,
    actor_id: str,
    executor: RuntimeExecutor,
    ledger: CommunityEvidenceLedger,
    environment: str = "local",
) -> dict[str, Any]:
    """Materialize, execute and receipt an MCP-only replay description."""

    if isinstance(inline_replay, str):
        if not inline_replay.strip():
            raise CommunityTestEvidenceError("evidence_v2.inline_replay_required")
        payload = inline_replay.encode("utf-8")
        if len(payload) > COMMUNITY_MAX_MANIFEST_BYTES:
            raise CommunityTestEvidenceError("evidence_v2.manifest_too_large")
        decoded = _decode_manifest_json(payload, source="inline")
    elif isinstance(inline_replay, Mapping):
        try:
            payload = _canonical_json(inline_replay).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CommunityTestEvidenceError(
                "evidence_v2.inline_replay_not_canonical_json"
            ) from exc
        if len(payload) > COMMUNITY_MAX_MANIFEST_BYTES:
            raise CommunityTestEvidenceError("evidence_v2.manifest_too_large")
        decoded = inline_replay
    else:
        raise CommunityTestEvidenceError("evidence_v2.inline_replay_object_required")
    normalized_manifest = build_inline_replay_manifest(
        decoded,
        board_id=board_id,
        spec_id=spec_id,
        scenario_id=scenario_id,
        scenario_sha256=scenario_sha256,
    )
    manifest, canonical_ref = _persist_inline_manifest(
        normalized_manifest, ledger=ledger
    )
    return await _execute_validated_manifest_and_build_evidence_v2(
        manifest=manifest,
        canonical_ref=canonical_ref,
        normalized_manifest=normalized_manifest,
        board_id=board_id,
        spec_id=spec_id,
        scenario_id=scenario_id,
        scenario_sha256=scenario_sha256,
        status=status,
        actor_id=actor_id,
        executor=executor,
        ledger=ledger,
        environment=environment,
    )


async def _execute_validated_manifest_and_build_evidence_v2(
    *,
    manifest: bytes,
    canonical_ref: str,
    normalized_manifest: Mapping[str, Any],
    board_id: str,
    spec_id: str,
    scenario_id: str,
    scenario_sha256: str,
    status: str,
    actor_id: str,
    executor: RuntimeExecutor,
    ledger: CommunityEvidenceLedger,
    environment: str,
) -> dict[str, Any]:
    """Execute already-validated bytes and append an authenticated receipt."""

    try:
        observed = executor(normalized_manifest, canonical_ref)
        if inspect.isawaitable(observed):
            observed = await observed
    except CommunityTestEvidenceError:
        raise
    except Exception as exc:
        raise CommunityTestEvidenceError(
            f"evidence_v2.runtime_execution_failed:{type(exc).__name__}"
        ) from exc
    if not isinstance(observed, ProductExecutionObservation):
        raise CommunityTestEvidenceError(
            "evidence_v2.executor_must_return_ProductExecutionObservation"
        )
    evidence = _build_evidence_v2(
        manifest_ref=canonical_ref,
        manifest=manifest,
        scenario_id=scenario_id,
        scenario_sha256=scenario_sha256,
        observation=observed,
        environment=environment,
    )
    # Validate executor facts before creating an immutable record. A temporary
    # opaque value satisfies only CORE's structural receipt check.
    provisional = {**evidence, "execution_receipt": "pending-server-receipt"}
    verdict = verify_mcp_replay_evidence_v2(
        status,
        provisional,
        scenario_id=scenario_id,
        scenario_sha256=scenario_sha256,
    )
    if not verdict.verified:
        raise CommunityTestEvidenceError(
            "invalid_execution_observation: " + ", ".join(verdict.reason_codes)
        )
    try:
        receipt = ledger.issue(
            board_id=board_id,
            spec_id=spec_id,
            scenario_id=scenario_id,
            scenario_sha256=scenario_sha256,
            status=status,
            actor_id=actor_id,
            evidence=evidence,
        )
    except CommunityTestEvidenceError:
        raise
    except OSError as exc:
        raise CommunityTestEvidenceError("evidence_v2.receipt_persist_failed") from exc
    evidence["execution_receipt"] = receipt
    return evidence


def normalize_test_scenario_evidence(
    evidence: object,
    *,
    scenario_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Normalize canonical V2 without inventing legacy execution facts."""

    normalized = _plain(evidence)
    if normalized is None:
        raise CommunityTestEvidenceError("evidence must be an object")
    embedded = _plain(normalized.get("mcp_replay_manifest"))
    if embedded and not normalized.get("manifest_ref"):
        candidate_ref = embedded.get("manifest_ref")
        candidate_attestation = embedded.get("execution_attestation")
        candidate_receipt = embedded.get("execution_receipt")
        if (
            isinstance(candidate_ref, str)
            and candidate_attestation is not None
            and candidate_receipt is not None
        ):
            candidate = {
                **normalized,
                "evidence_class": "mcp_replay_manifest",
                "manifest_ref": candidate_ref,
                "execution_attestation": candidate_attestation,
                "execution_receipt": candidate_receipt,
            }
            candidate.pop("mcp_replay_manifest", None)
            candidate_attestation_map = _plain(candidate_attestation)
            candidate_scenario_id = scenario_id or str(
                (candidate_attestation_map or {}).get("scenario_id") or ""
            )
            candidate_status = status or str(
                (candidate_attestation_map or {}).get("outcome") or "passed"
            )
            if verify_mcp_replay_evidence_v2(
                candidate_status, candidate, scenario_id=candidate_scenario_id
            ).verified:
                normalized = candidate
    if normalized.get("manifest_ref") is not None:
        normalized.setdefault("evidence_class", "mcp_replay_manifest")
        legacy = normalized.get("mcp_replay_manifest")
        if (
            isinstance(legacy, str)
            and legacy.strip() == str(normalized.get("manifest_ref")).strip()
        ):
            normalized.pop("mcp_replay_manifest", None)
    return normalized


def verify_community_evidence_v2(
    *,
    board_id: str,
    spec_id: str,
    status: str,
    scenario_id: str,
    scenario_sha256: str,
    evidence: object,
    ledger: CommunityEvidenceLedger,
    actor_id: str | None = None,
) -> EvidenceVerificationResult:
    """Verify CORE semantics, allowlisted bytes, ledger record, and HMAC."""

    normalized = normalize_test_scenario_evidence(
        evidence, scenario_id=scenario_id, status=status
    )
    verdict = verify_mcp_replay_evidence_v2(
        status,
        normalized,
        scenario_id=scenario_id,
        scenario_sha256=scenario_sha256,
    )
    if not verdict.verified:
        return verdict
    attestation = _plain(normalized.get("execution_attestation")) or {}
    provenance = _plain(attestation.get("provenance")) or {}
    reasons: list[str] = []
    if provenance.get("producer") != COMMUNITY_EVIDENCE_PRODUCER:
        reasons.append("evidence_v2.community_producer_required")
    if provenance.get("adapter") != COMMUNITY_EVIDENCE_ADAPTER:
        reasons.append("evidence_v2.community_adapter_required")
    try:
        manifest, canonical_ref = _manifest_bytes(
            str(normalized["manifest_ref"]), manifest_root=ledger.manifest_root
        )
    except CommunityTestEvidenceError as exc:
        reasons.append(str(exc))
    else:
        if canonical_ref != normalized.get("manifest_ref"):
            reasons.append("evidence_v2.manifest_ref_noncanonical")
        if manifest_sha256(manifest) != attestation.get("manifest_sha256"):
            reasons.append("evidence_v2.manifest_content_hash_mismatch")
        try:
            validate_replay_manifest(
                _decode_manifest_json(
                    manifest,
                    source=str(normalized["manifest_ref"]),
                ),
                board_id=board_id,
                spec_id=spec_id,
                scenario_id=scenario_id,
                scenario_sha256=scenario_sha256,
            )
        except CommunityTestEvidenceError as exc:
            reasons.append(str(exc))
    reasons.extend(
        ledger.verify(
            board_id=board_id,
            spec_id=spec_id,
            scenario_id=scenario_id,
            scenario_sha256=scenario_sha256,
            status=status,
            actor_id=actor_id,
            evidence=normalized,
        )
    )
    if reasons:
        return EvidenceVerificationResult(
            False,
            tuple(dict.fromkeys(reasons)),
            contract_version=EVIDENCE_V2_SCHEMA_VERSION,
        )
    return verdict


class CommunityTestEvidenceWriteVerifier:
    """Concrete write verifier backed by the installation ledger."""

    def __init__(self, *, ledger: CommunityEvidenceLedger) -> None:
        self._ledger = ledger

    def verify(
        self,
        *,
        board_id: str,
        spec_id: str,
        status: str,
        scenario_id: str,
        scenario_sha256: str,
        actor_id: str | None,
        evidence: object,
    ) -> TestEvidenceWriteVerification:
        verdict = verify_community_evidence_v2(
            board_id=board_id,
            spec_id=spec_id,
            status=status,
            scenario_id=scenario_id,
            scenario_sha256=scenario_sha256,
            actor_id=actor_id,
            evidence=evidence,
            ledger=self._ledger,
        )
        return TestEvidenceWriteVerification(
            verified=verdict.verified,
            reason_codes=verdict.reason_codes,
        )


class CommunityTestEvidenceExecutionIssuer:
    """Port implementation that emits a receipt only after a real replay."""

    def __init__(
        self,
        *,
        ledger: CommunityEvidenceLedger,
        executor: RuntimeExecutor,
        environment: str = "local",
    ) -> None:
        self._ledger = ledger
        self._executor = executor
        self._environment = environment

    async def execute(
        self, request: TestEvidenceExecutionRequest
    ) -> TestEvidenceExecutionResult:
        has_manifest_ref = bool(request.manifest_ref and request.manifest_ref.strip())
        has_inline_replay = request.inline_replay is not None and not (
            isinstance(request.inline_replay, str) and not request.inline_replay.strip()
        )
        if has_manifest_ref == has_inline_replay:
            raise CommunityTestEvidenceError(
                "evidence_v2.replay_source_exactly_one_required"
            )
        common = {
            "board_id": request.board_id,
            "spec_id": request.spec_id,
            "scenario_id": request.scenario_id,
            "scenario_sha256": request.scenario_sha256,
            "status": request.status,
            "actor_id": request.actor_id,
            "executor": self._executor,
            "ledger": self._ledger,
            "environment": self._environment,
        }
        if has_inline_replay:
            evidence = await run_inline_replay_and_build_evidence_v2(
                inline_replay=request.inline_replay,
                **common,
            )
        else:
            evidence = await run_manifest_and_build_evidence_v2(
                manifest_ref=str(request.manifest_ref),
                **common,
            )
        return TestEvidenceExecutionResult(evidence=evidence)


def migrate_test_scenario_evidence(
    scenarios: Sequence[Mapping[str, Any]],
) -> EvidenceMigrationReport:
    """Pure JSON-column migration; legacy values stay unverified."""

    migrated: list[dict[str, Any]] = []
    canonical_v2 = promoted_v2 = legacy_unverified = malformed = 0
    for original in scenarios:
        scenario = dict(original)
        for field in ("evidence", "latest_evidence"):
            raw = scenario.get(field)
            if raw is None:
                continue
            raw_map = _plain(raw)
            if raw_map is None:
                malformed += 1
                continue
            was_canonical = bool(
                raw_map.get("manifest_ref")
                and raw_map.get("execution_attestation")
                and raw_map.get("execution_receipt")
            )
            normalized = normalize_test_scenario_evidence(
                raw_map,
                scenario_id=str(scenario.get("id"))
                if scenario.get("id") is not None
                else None,
                status=str(scenario.get("status"))
                if scenario.get("status") is not None
                else None,
            )
            is_canonical = bool(
                normalized.get("manifest_ref")
                and normalized.get("execution_attestation")
                and normalized.get("execution_receipt")
            )
            scenario[field] = normalized
            if is_canonical:
                if was_canonical:
                    canonical_v2 += 1
                else:
                    promoted_v2 += 1
            elif (
                normalized.get("mcp_replay_manifest") is not None
                or normalized.get("manifest_ref") is not None
                or normalized.get("execution_attestation") is not None
            ):
                legacy_unverified += 1
        migrated.append(scenario)
    return EvidenceMigrationReport(
        scenarios=tuple(migrated),
        canonical_v2=canonical_v2,
        promoted_v2=promoted_v2,
        legacy_unverified=legacy_unverified,
        malformed=malformed,
    )


async def migrate_persisted_test_scenario_evidence(
    session: object,
    *,
    board_id: str | None = None,
    dry_run: bool = True,
) -> PersistedEvidenceMigrationReport:
    """Scan Community SQL specs and persist only lossless V2 promotions."""

    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from okto_pulse.community.adapters.sqlalchemy_models import Spec

    statement = select(Spec)
    if board_id is not None:
        statement = statement.where(Spec.board_id == board_id)
    execute = getattr(session, "execute", None)
    if not callable(execute):
        raise TypeError("session must provide async execute()")
    result = await execute(statement)
    specs = list(result.scalars().all())
    totals = {
        "canonical_v2": 0,
        "promoted_v2": 0,
        "legacy_unverified": 0,
        "malformed": 0,
    }
    updated = 0
    for spec in specs:
        original = list(spec.test_scenarios or [])
        report = migrate_test_scenario_evidence(original)
        for key in totals:
            totals[key] += int(getattr(report, key))
        migrated = list(report.scenarios)
        if migrated != original:
            updated += 1
            if not dry_run:
                spec.test_scenarios = migrated
                flag_modified(spec, "test_scenarios")
    if not dry_run:
        flush = getattr(session, "flush", None)
        if callable(flush):
            await flush()
    return PersistedEvidenceMigrationReport(
        specs_scanned=len(specs),
        specs_updated=updated,
        canonical_v2=totals["canonical_v2"],
        promoted_v2=totals["promoted_v2"],
        legacy_unverified=totals["legacy_unverified"],
        malformed=totals["malformed"],
        dry_run=dry_run,
    )


__all__ = [
    "COMMUNITY_EVIDENCE_ADAPTER",
    "COMMUNITY_EVIDENCE_PRODUCER",
    "COMMUNITY_MANIFEST_SCHEMA",
    "COMMUNITY_MANIFEST_PURPOSE",
    "COMMUNITY_MAX_MANIFEST_BYTES",
    "CommunityEvidenceLedger",
    "CommunityHttpManifestExecutor",
    "CommunityTestEvidenceError",
    "CommunityTestEvidenceExecutionIssuer",
    "CommunityTestEvidenceWriteVerifier",
    "EvidenceMigrationReport",
    "PersistedEvidenceMigrationReport",
    "ProductExecutionObservation",
    "build_inline_replay_manifest",
    "manifest_sha256",
    "migrate_test_scenario_evidence",
    "migrate_persisted_test_scenario_evidence",
    "normalize_test_scenario_evidence",
    "run_manifest_and_build_evidence_v2",
    "run_inline_replay_and_build_evidence_v2",
    "validate_replay_manifest",
    "verify_community_evidence_v2",
]
