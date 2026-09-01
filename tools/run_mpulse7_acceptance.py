"""Execute the frozen M-PULSE-7 bilateral acceptance gate.

The CLI is the certifying entry point and admits only the two reviewed real
Community factories plus this module's subprocess watchdogs.  Programmatic
factory/runner injection is retained for tests, whose receipts are explicitly
non-certifying.  Factories receive a :class:`GateBackendContext` and return an
object exposing the mutation, lifecycle and supplement seams documented by
``GateBackend`` below.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import importlib
import importlib.util
import inspect
import json
import math
import multiprocessing
import os
import platform
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any, Literal, Protocol, TypeAlias, TypeVar, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
for _import_root in (REPO_ROOT / "src", REPO_ROOT / "tests"):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

CORE_AUTHORITY_MODULE = "okto_pulse.core.kg.schema_contract"
GRAFX_AUTHORITY_MODULE = "okto_grafx"
CORE_CHECKOUT_ENV = "OKTO_PULSE_CORE_REPO"
GRAFX_CHECKOUT_ENV = "OKTO_E2E_GRAFX_REPO"
_FACTORY_AUTHORITY_MODULE = "mpulse7_acceptance_backends"
REQUIRED_GRAFX_DESCRIPTOR_REVALIDATION = "generation"
_CRASH_AUTHORITY_MODULE = "mpulse7_crash_harness"
_IMPORT_AUTHORITY_FORMAT = "okto-pulse-community-python-import-authority/1"
# Operational deadlock containment for setup/finalize; not a performance SLO.
_ISOLATED_WORKER_CONTROL_TIMEOUT_SECONDS = 300
_ISOLATED_WORKER_EVENT_POLL_SECONDS = 0.05
_OBSERVED_IMPORT_AUTHORITIES: dict[str, dict[str, Any]] = {}
_OBSERVED_IMPORT_AUTHORITIES_BY_ORIGIN: dict[str, list[dict[str, Any]]] = {}
_PENDING_IMPORT_AUTHORITY_NAMES: set[str] = set()
_PENDING_IMPORT_AUTHORITY_ORIGINS: dict[str, str] = {}
_IMPORT_AUTHORITY_CHECKOUTS: dict[str, dict[str, Any]] = {}
_ACTIVE_PRODUCTIVE_PYTHON_CATALOG: dict[str, dict[str, str]] | None = None
_CACHED_PRODUCTIVE_PYTHON_CATALOG: (
    tuple[tuple[tuple[str, str, str], ...], dict[str, dict[str, str]], dict[str, Any]]
    | None
) = None


def _code_constant_authority(value: Any, *, include_filename: bool) -> Any:
    if isinstance(value, CodeType):
        return {
            "code": _code_object_authority(
                value,
                include_filename=include_filename,
            )
        }
    if value is None:
        return {"none": True}
    if value is Ellipsis:
        return {"ellipsis": True}
    if type(value) is bool:
        return {"bool": value}
    if type(value) is int:
        return {"int": str(value)}
    if type(value) is float:
        return {"float64": struct.pack(">d", value).hex()}
    if type(value) is complex:
        return {
            "complex128": (
                struct.pack(">d", value.real).hex(),
                struct.pack(">d", value.imag).hex(),
            )
        }
    if type(value) is str:
        return {"str": value}
    if type(value) is bytes:
        return {"bytes": value.hex()}
    if type(value) is tuple:
        return {
            "tuple": [
                _code_constant_authority(item, include_filename=include_filename)
                for item in value
            ]
        }
    if type(value) is frozenset:
        items = [
            _code_constant_authority(item, include_filename=include_filename)
            for item in value
        ]
        items.sort(
            key=lambda item: json.dumps(
                item,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return {"frozenset": items}
    raise TypeError(f"unsupported code constant: {type(value).__name__}")


def _code_object_authority(
    code: CodeType,
    *,
    include_filename: bool = True,
) -> dict[str, Any]:
    authority = {
        "argcount": code.co_argcount,
        "cellvars": list(code.co_cellvars),
        "code": code.co_code.hex(),
        "consts": [
            _code_constant_authority(value, include_filename=include_filename)
            for value in code.co_consts
        ],
        "exceptiontable": code.co_exceptiontable.hex(),
        "firstlineno": code.co_firstlineno,
        "flags": code.co_flags,
        "freevars": list(code.co_freevars),
        "kwonlyargcount": code.co_kwonlyargcount,
        "linetable": code.co_linetable.hex(),
        "name": code.co_name,
        "names": list(code.co_names),
        "nlocals": code.co_nlocals,
        "posonlyargcount": code.co_posonlyargcount,
        "qualname": code.co_qualname,
        "stacksize": code.co_stacksize,
        "varnames": list(code.co_varnames),
    }
    if include_filename:
        authority["filename"] = code.co_filename
    return authority


def _code_object_sha256(code: CodeType, *, include_filename: bool = True) -> str:
    encoded = json.dumps(
        _code_object_authority(code, include_filename=include_filename),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _origin_key(value: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(value)))


def _register_import_authority_checkout(
    label: str,
    checkout_root: Path,
    *,
    source_roots: Sequence[Path],
    source_files: Sequence[Path] = (),
) -> None:
    root = checkout_root.resolve()
    _IMPORT_AUTHORITY_CHECKOUTS[label] = {
        "checkout_root": root,
        "source_files": frozenset(path.resolve() for path in source_files),
        "source_roots": tuple(path.resolve() for path in source_roots),
    }


def _refresh_import_authority_checkouts() -> None:
    _register_import_authority_checkout(
        "community",
        REPO_ROOT,
        source_roots=(REPO_ROOT / "src", REPO_ROOT / "tools"),
        source_files=(REPO_ROOT / "tests" / "mpulse7_gate_support.py",),
    )
    for label, variable in (
        ("core", CORE_CHECKOUT_ENV),
        ("okto_grafx", GRAFX_CHECKOUT_ENV),
    ):
        raw = os.environ.get(variable)
        if type(raw) is str and bool(raw):
            checkout = Path(raw).resolve()
            _register_import_authority_checkout(
                label,
                checkout,
                source_roots=(checkout / "src",),
            )


def _productive_checkout_for_origin(origin: Path) -> str | None:
    if origin.suffix.casefold() != ".py":
        return None
    resolved = origin.resolve()
    for label, checkout in _IMPORT_AUTHORITY_CHECKOUTS.items():
        if resolved in checkout["source_files"]:
            return label
        for source_root in checkout["source_roots"]:
            try:
                resolved.relative_to(source_root)
            except ValueError:
                continue
            return label
    return None


_refresh_import_authority_checkouts()


def _captured_import_authority(
    module_name: str,
    origin: Path,
    code: CodeType,
) -> dict[str, Any]:
    try:
        source_sha256: str | None = hashlib.sha256(origin.read_bytes()).hexdigest()
        error: str | None = None
    except OSError as failure:
        source_sha256 = None
        error = f"{type(failure).__name__}: {failure}"
    return {
        "code_filename": code.co_filename,
        "code_sha256": _code_object_sha256(code),
        "error": error,
        "format": _IMPORT_AUTHORITY_FORMAT,
        "module": module_name,
        "origin": str(origin),
        "portable_code_sha256": _code_object_sha256(code, include_filename=False),
        "source_sha256": source_sha256,
    }


_RUNNER_IMPORT_AUTHORITY = _captured_import_authority(
    "run_mpulse7_acceptance",
    Path(__file__).resolve(),
    sys._getframe().f_code,
)
_OBSERVED_IMPORT_AUTHORITIES_BY_ORIGIN.setdefault(
    _origin_key(_RUNNER_IMPORT_AUTHORITY["origin"]), []
).append(_RUNNER_IMPORT_AUTHORITY)


def _module_source_suffixes(module_name: str) -> tuple[str, str]:
    relative = os.path.join(*module_name.split("."))
    return (
        os.path.normcase(relative + ".py"),
        os.path.normcase(os.path.join(relative, "__init__.py")),
    )


def _import_authority_audit_hook(event: str, arguments: tuple[Any, ...]) -> None:
    if event != "exec" or not arguments:
        return
    code = arguments[0]
    if not isinstance(code, CodeType) or code.co_filename.startswith("<"):
        return
    key = _origin_key(code.co_filename)
    origin = Path(code.co_filename).resolve()
    checkout_label = _productive_checkout_for_origin(origin)
    module_name = _PENDING_IMPORT_AUTHORITY_ORIGINS.get(key)
    if module_name is None and _PENDING_IMPORT_AUTHORITY_NAMES:
        normalized = os.path.normcase(os.path.normpath(code.co_filename))
        for candidate in tuple(_PENDING_IMPORT_AUTHORITY_NAMES):
            if normalized.endswith(_module_source_suffixes(candidate)):
                module_name = candidate
                break
    if module_name is None and checkout_label is None:
        return
    try:
        authority = _captured_import_authority(
            module_name or "",
            origin,
            code,
        )
        authority["checkout"] = checkout_label
    except BaseException as failure:  # noqa: BLE001 - import audit boundary
        authority = {
            "error": f"{type(failure).__name__}: {failure}",
            "format": _IMPORT_AUTHORITY_FORMAT,
            "module": module_name or "",
        }
    if checkout_label is not None and _ACTIVE_PRODUCTIVE_PYTHON_CATALOG is not None:
        catalog_entry = _ACTIVE_PRODUCTIVE_PYTHON_CATALOG.get(key)
        if (
            catalog_entry is None
            or authority.get("error") is not None
            or authority.get("source_sha256") != catalog_entry["source_sha256"]
            or authority.get("portable_code_sha256") != catalog_entry["code_sha256"]
        ):
            raise GateFailure(
                "certification blocked productive Python import outside the "
                "authenticated source catalog"
            )
    _OBSERVED_IMPORT_AUTHORITIES_BY_ORIGIN.setdefault(key, []).append(authority)
    if module_name is not None:
        _OBSERVED_IMPORT_AUTHORITIES[module_name] = authority
        _PENDING_IMPORT_AUTHORITY_NAMES.discard(module_name)
        for pending_origin, candidate in tuple(
            _PENDING_IMPORT_AUTHORITY_ORIGINS.items()
        ):
            if candidate == module_name:
                del _PENDING_IMPORT_AUTHORITY_ORIGINS[pending_origin]


try:
    sys.addaudithook(_import_authority_audit_hook)
    _IMPORT_AUTHORITY_AUDIT_INSTALLED = True
except RuntimeError:
    _IMPORT_AUTHORITY_AUDIT_INSTALLED = False


def _register_module_import_authority(
    module_name: str,
    *,
    discover_origin: bool = True,
) -> None:
    """Arm one exact module import for in-memory code/source measurement."""

    if module_name in _OBSERVED_IMPORT_AUTHORITIES:
        return
    _PENDING_IMPORT_AUTHORITY_NAMES.add(module_name)
    if not discover_origin:
        return
    try:
        specification = importlib.util.find_spec(module_name)
    except (ImportError, AttributeError, ValueError):
        return
    origin = None if specification is None else specification.origin
    if type(origin) is str and origin not in {"built-in", "frozen"}:
        _PENDING_IMPORT_AUTHORITY_ORIGINS[_origin_key(origin)] = module_name


for _authority_module in (
    GRAFX_AUTHORITY_MODULE,
    _FACTORY_AUTHORITY_MODULE,
    _CRASH_AUTHORITY_MODULE,
):
    _register_module_import_authority(_authority_module)
_register_module_import_authority(CORE_AUTHORITY_MODULE, discover_origin=False)

from mpulse7_gate_support import (
    DeterministicGraphModel,
    SplitMix64,
    board_result_supplement_sha256,
    canonical_json_bytes,
    canonical_sha256,
    crash_points_sha256,
    evaluate_trace,
    expand_trace,
    load_gate_manifest,
)
from okto_pulse.core.kg.interfaces.graph_store import QueryFilters
from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetIntent,
    ProjectionEdgeRef,
    ProjectionNodeRef,
)
from okto_pulse.core.kg.schema_contract import NODE_TYPES

from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)

BACKENDS = ("ladybug", "grafx")
RECEIPT_FORMAT = "okto-pulse-community-m-pulse-7-acceptance-receipt/1"
MANIFEST_PATH = REPO_ROOT / "tests" / "fixtures" / ("m_pulse_7_acceptance_gate_v1.json")
TOOLS_ROOT = REPO_ROOT / "tools"
RUNNER_SOURCE_PATH = TOOLS_ROOT / "run_mpulse7_acceptance.py"
FACTORY_SOURCE_PATH = TOOLS_ROOT / "mpulse7_acceptance_backends.py"
CRASH_HARNESS_SOURCE_PATH = TOOLS_ROOT / "mpulse7_crash_harness.py"
CERTIFICATION_FACTORY_REFS = {
    "ladybug": "mpulse7_acceptance_backends:ladybug_factory",
    "grafx": "mpulse7_acceptance_backends:grafx_factory",
}
CERTIFICATION_BOARD_RUNNER_REF = (
    "tools/run_mpulse7_acceptance.py:run_isolated_board_query"
)
CERTIFICATION_PULSE_RUNNER_REF = (
    "tools/run_mpulse7_acceptance.py:run_isolated_pulse_corpus_case"
)
CERTIFICATION_CRASH_HARNESS_REF = "mpulse7_crash_harness:run_crash_point"
CERTIFICATION_PROCESS_AUTHORITY_FORMAT = (
    "okto-pulse-community-m-pulse-7-process-authority/1"
)

# These four values are an authority outside the self-describing JSON documents.
# Changing the frozen manifest/corpus requires an explicit new gate version and
# corresponding code review, rather than recomputing digests inside altered input.
CERTIFICATION_MANIFEST_FILE_SHA256 = (
    "94beae63c97da124e0dd5681926c5be72688ff38c96d45d4d49d18e63dfa2f9b"
)
CERTIFICATION_MANIFEST_CANONICAL_SHA256 = (
    "b3cbdfdcd3989dcf2d0f0831c57905336e0e2bb05d0f66876242feca1fa03012"
)
CERTIFICATION_PULSE_CORPUS_FILE_SHA256 = (
    "0997747ed8bb9172d05781a62e5f81e7694630b173aaa152ac9ea28daec9d13f"
)
CERTIFICATION_PULSE_CORPUS_LOGICAL_SHA256 = (
    "b29334edf6e7c1e6b9419a4f3add84ede4baad94fdeaecb0c679261a78f241cc"
)
_BOARD_QUERY_METHODS = frozenset(
    {
        "edge_exists",
        "find_active_by_source_ref",
        "find_by_artifact",
        "find_by_topic",
        "find_contradictions",
        "find_node_types",
        "get_alternatives",
        "get_constraint_detail",
        "get_learnings_for_area",
        "get_schema_info",
        "get_schema_version",
        "list_node_properties",
        "list_schema_objects",
        "traverse_supersedence",
        "vector_search",
    }
)
_T = TypeVar("_T")
GateExecutionMode: TypeAlias = Literal["test_only", "certification"]


class GateFailure(RuntimeError):
    """The frozen gate could not prove its acceptance conditions."""


@dataclass(frozen=True, slots=True)
class GateBackendContext:
    """Stable, pickle-safe identity passed to every backend factory."""

    backend: str
    board_id: str
    workspace: str
    run_id: str
    certification_process_authority_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class _IsolatedOperationControl:
    """Spawn-safe handshake that keeps setup outside the operation watchdog."""

    operation_finished: Any
    operation_finished_ns: Any
    operation_started: Any
    operation_started_ns: Any
    ready: Any
    start: Any


class GateBackend(Protocol):
    """Closed integration seam required from a real gate backend.

    ``reopen_recover_verify_fingerprint`` must close every active handle,
    reopen from durable routing state, run backend recovery, call
    ``verify("all")``, and return the closed evidence validated by this runner.
    The method must also leave ``semantic_store`` and ``graph_transaction``
    pointing at the reopened runtime for the next operation.

    The runner never gives a backend an expected fingerprint.  It independently
    reads ``observe_fingerprints`` after recovery and compares the observed
    trace-model digest with its frozen oracle.  ``logical_graph_sha256`` is the
    M-PULSE-5 logical fingerprint and is compared bilaterally.

    Corpus and supplement callbacks return closed backend-neutral evidence;
    backend names, paths and physical identifiers are deliberately excluded
    from those semantic results so bilateral comparison is meaningful.  Raw
    write-family callbacks receive the complete corpus entry authenticated at
    gate start and must not reopen an independent corpus file.
    """

    semantic_store: Any
    graph_transaction: Any

    def identity(self) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...

    def reopen_recover_verify_fingerprint(
        self,
        *,
        after_operations: int,
        verify_scope: str,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...

    def observe_fingerprints(
        self,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...

    def run_crash_point(
        self, point: Mapping[str, Any]
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...

    def run_pulse_corpus_case(
        self, entry: Mapping[str, Any]
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...

    def run_raw_execute_family(
        self, entry: Mapping[str, Any]
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...

    def run_receipt_bound_scenario(
        self, scenario_id: str
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]: ...

    def close(self) -> None | Awaitable[None]: ...


GateBackendFactory: TypeAlias = Callable[
    [GateBackendContext], GateBackend | Awaitable[GateBackend]
]


@dataclass(frozen=True, slots=True)
class IsolatedQueryResult:
    case_id: str
    fingerprint_logical_graph_sha256: str
    fingerprint_trace_model_sha256: str
    generation: str
    ordering: str
    result_sha256: str
    row_count: int
    storage_identity: str
    worker_pid: int
    execution_authority_sha256: str | None = None


IsolatedQueryRunner: TypeAlias = Callable[
    [GateBackendFactory, GateBackendContext, Mapping[str, Any], int],
    IsolatedQueryResult | Awaitable[IsolatedQueryResult],
]


@dataclass(frozen=True, slots=True)
class IsolatedPulseCorpusResult:
    entry_class: str
    entry_id: str
    fingerprint_logical_graph_sha256: str
    fingerprint_trace_model_sha256: str
    generation: str
    result_sha256: str
    status: str
    storage_identity: str
    worker_pid: int
    execution_authority_sha256: str | None = None


IsolatedPulseCorpusRunner: TypeAlias = Callable[
    [GateBackendFactory, GateBackendContext, Mapping[str, Any], int],
    IsolatedPulseCorpusResult | Awaitable[IsolatedPulseCorpusResult],
]


@dataclass(frozen=True, slots=True)
class FrozenGateInputs:
    manifest: dict[str, Any]
    operations: tuple[dict[str, Any], ...]
    pulse_corpus: dict[str, Any]
    manifest_path: Path
    pulse_corpus_path: Path
    manifest_file_sha256: str
    manifest_canonical_sha256: str
    pulse_corpus_file_sha256: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GateFailure(message)


def _resolve_corpus_path(
    manifest_path: Path,
    explicit_path: Path | None,
) -> Path:
    if explicit_path is not None:
        return explicit_path.resolve()
    environment_root = os.environ.get("OKTO_E2E_GRAFX_REPO")
    candidates = []
    if environment_root:
        candidates.append(Path(environment_root))
    community_root = manifest_path.resolve().parents[2]
    candidates.extend(
        (
            community_root.parent / "okto_grafx",
            community_root / "okto-grafx",
        )
    )
    relative = Path("tests/corpus/pulse_query_corpus_1_0.json")
    for root in candidates:
        candidate = root / relative
        if candidate.is_file():
            return candidate.resolve()
    raise GateFailure(
        "the frozen Pulse query corpus could not be resolved; pass "
        "--pulse-corpus or set OKTO_E2E_GRAFX_REPO"
    )


def _load_json_document(path: Path) -> Any:
    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise GateFailure(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise GateFailure(f"non-standard JSON constant in {path}: {value}")

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=strict_object,
            parse_constant=reject_constant,
        )
    except GateFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as failure:
        raise GateFailure(
            f"cannot authenticate JSON document {path}: {failure}"
        ) from failure


def _verify_pulse_corpus(
    path: Path, manifest: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    try:
        physical = path.read_bytes()
    except OSError as failure:
        raise GateFailure(
            f"cannot read frozen Pulse query corpus {path}: {failure}"
        ) from failure
    expected = manifest["pulse_query_corpus"]
    physical_sha256 = _sha256_bytes(physical)
    _require(
        physical_sha256 == expected["physical_file_sha256"],
        "the physical Pulse query corpus digest differs from the manifest",
    )
    document = _load_json_document(path)
    _require(type(document) is dict, "the Pulse query corpus must be a JSON object")
    assert isinstance(document, dict)
    declared_digest = document.get("digest")
    digest_payload = {key: value for key, value in document.items() if key != "digest"}
    logical_digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    _require(
        declared_digest == logical_digest == expected["digest"],
        "the logical Pulse query corpus digest differs from the manifest",
    )
    _require(
        document.get("descriptor") == expected["descriptor"],
        "the Pulse query corpus descriptor differs from the manifest",
    )
    _require(
        document.get("entry_count") == expected["entry_count"],
        "the Pulse query corpus entry count differs from the manifest",
    )
    entries = document.get("entries")
    _require(type(entries) is list, "the Pulse query corpus entries must be a list")
    assert isinstance(entries, list)
    _require(
        len(entries) == int(expected["entry_count"]) == 97,
        "the Pulse query corpus must retain exactly 97 entries",
    )
    _require(
        all(type(entry) is dict for entry in entries),
        "every Pulse query corpus entry must be an object",
    )
    entry_ids = [str(entry.get("id")) for entry in entries]
    _require(
        len(set(entry_ids)) == len(entry_ids),
        "the Pulse query corpus contains duplicate entry IDs",
    )
    class_counts = {
        name: sum(entry.get("class") == name for entry in entries)
        for name in ("read", "write", "fragment")
    }
    _require(
        class_counts
        == {
            "read": int(expected["read_entry_count"]),
            "write": int(expected["write_entry_count"]),
            "fragment": int(expected["fragment_entry_count"]),
        },
        "the Pulse query corpus class distribution differs from the manifest",
    )
    return physical_sha256, document


def verify_frozen_inputs(
    manifest_path: Path = MANIFEST_PATH,
    *,
    pulse_corpus_path: Path | None = None,
    certification: bool = False,
) -> FrozenGateInputs:
    """Load and authenticate every executable input.

    Test-only callers receive the self-consistent fixture they name.  A
    certification caller is additionally pinned to the externally versioned
    manifest/corpus paths and four digests compiled into this runner.
    """

    manifest_path = manifest_path.resolve()
    if certification:
        _require(
            manifest_path == MANIFEST_PATH.resolve(),
            "certification requires the exact frozen M-PULSE-7 manifest path",
        )
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = load_gate_manifest(manifest_path)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as failure:
        raise GateFailure(
            f"cannot authenticate M-PULSE-7 manifest: {failure}"
        ) from failure

    try:
        operations = expand_trace(manifest)
        evaluation = evaluate_trace(manifest)
    except (AssertionError, KeyError, TypeError, ValueError) as failure:
        raise GateFailure(
            f"cannot expand the frozen M-PULSE-7 trace: {failure}"
        ) from failure

    trace = manifest["trace"]
    schema = trace["schema_authority"]
    layouts = [
        [entry.logical_type, entry.from_type, entry.to_type]
        for entry in PULSE_RELATIONSHIP_LAYOUT.entries
    ]
    _require(
        len(NODE_TYPES) == int(schema["node_type_count"])
        and canonical_sha256(list(NODE_TYPES)) == schema["node_types_sha256"],
        "the runtime node-type authority differs from the frozen manifest",
    )
    _require(
        len(layouts) == int(schema["relationship_layout_count"])
        and canonical_sha256(layouts) == schema["relationship_layouts_sha256"],
        "the runtime relationship-layout authority differs from the frozen manifest",
    )
    _require(
        int(trace["operation_count"]) == len(operations) == 10_000,
        "the M-PULSE-7 trace must contain exactly 10,000 operations",
    )
    _require(
        evaluation.trace_sha256 == trace["expanded_trace_sha256"],
        "the expanded trace digest differs from the frozen digest",
    )
    _require(
        list(evaluation.checkpoints) == trace["checkpoints"],
        "the trace checkpoint oracle differs from the frozen checkpoints",
    )
    _require(
        evaluation.final_fingerprint_sha256 == trace["final_model_fingerprint_sha256"],
        "the final trace fingerprint differs from the frozen fingerprint",
    )
    _require(
        evaluation.final_census == trace["checkpoints"][-1]["census"],
        "the final trace census differs from the frozen census",
    )

    recovery = manifest["reopen_recovery_cycles"]
    recovery_boundaries = tuple(int(value) for value in recovery["after_operations"])
    _require(
        int(recovery["count"]) == len(recovery_boundaries) == 3,
        "the gate must retain exactly three reopen/recovery cycles",
    )
    _require(
        recovery_boundaries == (2500, 5000, 7500),
        "the reopen/recovery boundaries are not the frozen boundaries",
    )
    _require(
        tuple(item["after_operations"] for item in evaluation.recovery_cycles)
        == recovery_boundaries,
        "the recovery oracle boundaries differ from the manifest",
    )

    supplement = manifest["board_result_supplement"]
    queries = supplement["queries"]
    _require(len(queries) == 19, "the Board result supplement must contain 19 cases")
    _require(
        len({str(case["id"]) for case in queries}) == 19,
        "the Board result supplement contains duplicate case IDs",
    )
    _require(
        all(
            case.get("ordering") in {"ordered", "multiset"}
            and case.get("method") in _BOARD_QUERY_METHODS
            for case in queries
        ),
        "the Board result supplement contains an open or invalid query case",
    )
    _require(
        board_result_supplement_sha256(manifest) == supplement["queries_sha256"],
        "the Board result supplement digest differs from the manifest",
    )
    _require(
        crash_points_sha256(manifest) == manifest["crash_points"]["points_sha256"],
        "the crash-point digest differs from the manifest",
    )

    raw = manifest["raw_execute_supplement"]
    raw_ids = tuple(str(value) for value in raw["family_ids"])
    _require(
        int(raw["family_count"]) == len(raw_ids) == len(set(raw_ids)) == 21,
        "the raw execute supplement must contain 21 unique frozen IDs",
    )
    scenarios = manifest["receipt_bound_scenarios"]
    _require(
        len(scenarios) == 4 and len({str(value["id"]) for value in scenarios}) == 4,
        "the receipt-bound supplement must contain four unique frozen scenarios",
    )
    _require(
        manifest["benchmark_contract"]["backends"] == list(BACKENDS),
        "the benchmark backend order must remain Ladybug then Grafx",
    )
    acceptance = manifest["acceptance"]
    _require(
        acceptance["maximum_unexplained_divergences"] == 0
        and acceptance["maximum_verify_failures"] == 0
        and acceptance["query_timeout_failures_allowed"] == 0
        and acceptance["required_verify_scope"] == "all",
        "the frozen fail-closed acceptance thresholds changed",
    )

    corpus_path = _resolve_corpus_path(manifest_path, pulse_corpus_path)
    if certification:
        expected_corpus_path = _resolve_corpus_path(MANIFEST_PATH.resolve(), None)
        _require(
            corpus_path == expected_corpus_path,
            "certification requires the exact frozen Pulse corpus path",
        )
    corpus_sha256, corpus = _verify_pulse_corpus(corpus_path, manifest)
    corpus_entries = cast(list[dict[str, Any]], corpus["entries"])
    corpus_entries_by_id = {str(entry["id"]): entry for entry in corpus_entries}
    _require(
        raw.get("corpus_digest") == manifest["pulse_query_corpus"]["digest"],
        "the raw execute supplement is not bound to the Pulse corpus digest",
    )
    _require(
        all(
            family_id in corpus_entries_by_id
            and corpus_entries_by_id[family_id].get("class") == "write"
            and corpus_entries_by_id[family_id].get("classification")
            == "already_supported"
            for family_id in raw_ids
        ),
        "the raw execute supplement is not bound to supported write entries",
    )
    inputs = FrozenGateInputs(
        manifest=manifest,
        operations=operations,
        pulse_corpus=corpus,
        manifest_path=manifest_path,
        pulse_corpus_path=corpus_path,
        manifest_file_sha256=_sha256_bytes(manifest_bytes),
        manifest_canonical_sha256=canonical_sha256(manifest),
        pulse_corpus_file_sha256=corpus_sha256,
    )
    if certification:
        _require_certification_input_digests(inputs)
    return inputs


def _require_certification_input_digests(inputs: FrozenGateInputs) -> None:
    """Validate frozen inputs against authority not supplied by those inputs."""

    _require(
        inputs.manifest_file_sha256 == CERTIFICATION_MANIFEST_FILE_SHA256,
        "certification manifest physical SHA-256 differs from frozen authority",
    )
    _require(
        inputs.manifest_canonical_sha256 == CERTIFICATION_MANIFEST_CANONICAL_SHA256,
        "certification manifest canonical SHA-256 differs from frozen authority",
    )
    _require(
        inputs.pulse_corpus_file_sha256 == CERTIFICATION_PULSE_CORPUS_FILE_SHA256,
        "certification Pulse corpus physical SHA-256 differs from frozen authority",
    )
    _require(
        inputs.manifest["pulse_query_corpus"]["digest"]
        == inputs.pulse_corpus.get("digest")
        == CERTIFICATION_PULSE_CORPUS_LOGICAL_SHA256,
        "certification Pulse corpus logical SHA-256 differs from frozen authority",
    )


def _source_file_record(
    source: str | os.PathLike[str] | None,
    *,
    expected_path: Path,
    label: str,
) -> dict[str, str]:
    _require(source is not None, f"certification {label} source file is unavailable")
    observed = Path(source).resolve()
    expected = expected_path.resolve()
    _require(
        observed == expected and observed.parent == TOOLS_ROOT.resolve(),
        f"certification {label} source file is outside the expected tools path",
    )
    try:
        relative = observed.relative_to(REPO_ROOT.resolve()).as_posix()
        digest = _sha256_bytes(observed.read_bytes())
    except (OSError, ValueError) as failure:
        raise GateFailure(
            f"cannot authenticate certification {label} source file: {failure}"
        ) from failure
    return {"path": relative, "sha256": digest}


def _git_text(repository: Path, label: str, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            text=True,
        )
    except (OSError, UnicodeError) as failure:
        raise GateFailure(
            f"cannot inspect {label} git authority: {failure}"
        ) from failure
    _require(
        completed.returncode == 0,
        f"cannot inspect {label} git authority: "
        + (completed.stderr.strip() or "git command failed"),
    )
    return completed.stdout.strip()


def _canonical_git_head(repository: Path, label: str) -> str:
    head = _git_text(repository, label, "rev-parse", "HEAD")
    _require(
        len(head) in {40, 64}
        and all(character in "0123456789abcdef" for character in head),
        f"{label} git HEAD is not a canonical object ID",
    )
    return head


def _community_git_authority(
    source_records: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    repository = REPO_ROOT.resolve()
    head = _canonical_git_head(repository, "Community")
    top_level = Path(
        _git_text(repository, "Community", "rev-parse", "--show-toplevel")
    ).resolve()
    _require(
        top_level == repository,
        "Community source is not under the exact certification checkout",
    )
    tracked_paths = sorted({record["path"] for record in source_records.values()})
    for relative_path in tracked_paths:
        _git_text(
            repository,
            "Community",
            "ls-files",
            "--error-unmatch",
            "--",
            relative_path,
        )
    clean = (
        _git_text(
            repository,
            "Community",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        == ""
    )
    _require(clean, "certification requires a clean Community git worktree")
    return {
        "checkout_root": str(repository),
        "clean": True,
        "head": head,
        "tracked_source_files": tracked_paths,
    }


def _checkout_root_from_environment(variable: str, label: str) -> Path:
    raw = os.environ.get(variable)
    _require(
        type(raw) is str and bool(raw),
        f"certification requires {variable} for the exact {label} checkout",
    )
    root = Path(cast(str, raw)).resolve()
    _require(root.is_dir(), f"certification {label} checkout does not exist")
    top_level = Path(_git_text(root, label, "rev-parse", "--show-toplevel")).resolve()
    _require(
        top_level == root,
        f"{label} module is not under the exact certification checkout",
    )
    return root


def _validated_self_import_authority(
    authority: Mapping[str, Any],
    *,
    origin: Path,
    label: str,
) -> dict[str, str]:
    """Authenticate the top-level code object executing this exact process."""

    _require(
        authority.get("format") == _IMPORT_AUTHORITY_FORMAT
        and authority.get("error") is None
        and authority.get("origin") == str(origin),
        f"certification {label} in-memory import authority is unavailable",
    )
    try:
        source = origin.read_bytes()
        compiled = compile(
            source,
            str(authority["code_filename"]),
            "exec",
            dont_inherit=True,
            optimize=sys.flags.optimize,
        )
    except (OSError, SyntaxError, TypeError, ValueError) as failure:
        raise GateFailure(
            f"cannot validate certification {label} in-memory import authority: "
            f"{failure}"
        ) from failure
    source_sha256 = _sha256_bytes(source)
    code_sha256 = _code_object_sha256(compiled)
    _require(
        authority.get("source_sha256") == source_sha256,
        f"certification {label} imported source bytes differ from current source",
    )
    _require(
        authority.get("code_sha256") == code_sha256,
        f"certification {label} loaded code differs from current source",
    )
    return {
        "code_sha256": code_sha256,
        "source_sha256": source_sha256,
    }


def _validated_origin_import_authority(
    origin: Path,
    *,
    label: str,
) -> dict[str, str]:
    """Validate every top-level code object ever executed from one source path."""

    resolved = origin.resolve()
    observations = _OBSERVED_IMPORT_AUTHORITIES_BY_ORIGIN.get(_origin_key(resolved), [])
    _require(
        _IMPORT_AUTHORITY_AUDIT_INSTALLED and bool(observations),
        f"certification {label} was loaded before import authority measurement",
    )
    try:
        source = resolved.read_bytes()
    except OSError as failure:
        raise GateFailure(
            f"cannot validate certification {label} imported source bytes: {failure}"
        ) from failure
    source_sha256 = _sha256_bytes(source)
    observed_code_sha256: set[str] = set()
    for authority in observations:
        _require(
            authority.get("format") == _IMPORT_AUTHORITY_FORMAT
            and authority.get("error") is None
            and authority.get("origin") == str(resolved),
            f"certification {label} import authority is invalid",
        )
        _require(
            authority.get("source_sha256") == source_sha256,
            f"certification {label} imported source bytes differ from current source",
        )
        try:
            current_code = compile(
                source,
                str(authority["code_filename"]),
                "exec",
                dont_inherit=True,
                optimize=sys.flags.optimize,
            )
        except (SyntaxError, TypeError, ValueError) as failure:
            raise GateFailure(
                f"cannot validate certification {label} loaded code: {failure}"
            ) from failure
        current_code_sha256 = _code_object_sha256(current_code)
        portable_code_sha256 = _code_object_sha256(
            current_code,
            include_filename=False,
        )
        _require(
            authority.get("code_sha256") == current_code_sha256,
            f"certification {label} loaded code differs from current source",
        )
        _require(
            authority.get("portable_code_sha256") == portable_code_sha256,
            f"certification {label} portable loaded code differs from current source",
        )
        observed_code_sha256.add(current_code_sha256)
    _require(
        len(observed_code_sha256) == 1,
        f"certification {label} executed under multiple code origins",
    )
    return {
        "code_sha256": next(iter(observed_code_sha256)),
        "source_sha256": source_sha256,
    }


def _validated_module_import_authority(
    module: ModuleType,
    *,
    module_name: str,
    origin: Path,
    label: str,
) -> dict[str, str]:
    """Validate source bytes and the exact top-level code observed at import."""

    authority = _OBSERVED_IMPORT_AUTHORITIES.get(module_name)
    _require(
        _IMPORT_AUTHORITY_AUDIT_INSTALLED
        and type(authority) is dict
        and authority.get("format") == _IMPORT_AUTHORITY_FORMAT
        and authority.get("error") is None,
        f"certification {label} was loaded before import authority measurement",
    )
    _require(
        authority.get("module") == module_name
        and authority.get("origin") == str(origin),
        f"certification {label} loaded module origin differs from its import authority",
    )
    observed = _validated_origin_import_authority(origin, label=label)
    loader = getattr(module, "__loader__", None)
    get_code = getattr(loader, "get_code", None)
    _require(
        callable(get_code),
        f"certification {label} import loader exposes no executable code authority",
    )
    try:
        current_code = get_code(module_name)
    except (ImportError, OSError, TypeError, ValueError) as failure:
        raise GateFailure(
            f"cannot validate certification {label} loaded code: {failure}"
        ) from failure
    _require(
        isinstance(current_code, CodeType),
        f"certification {label} import loader returned no code object",
    )
    code_sha256 = _code_object_sha256(current_code)
    _require(
        authority.get("code_sha256") == code_sha256 == observed["code_sha256"],
        f"certification {label} loaded code differs from current source",
    )
    return observed


def _module_checkout_authority(
    *,
    label: str,
    module_name: str,
    checkout_environment: str,
) -> dict[str, Any]:
    root = _checkout_root_from_environment(checkout_environment, label)
    _register_module_import_authority(module_name)
    try:
        module = importlib.import_module(module_name)
    except ImportError as failure:
        raise GateFailure(
            f"cannot import certification {label} authority module: {failure}"
        ) from failure
    source = getattr(module, "__file__", None)
    spec_origin = getattr(getattr(module, "__spec__", None), "origin", None)
    _require(
        type(source) is str and type(spec_origin) is str,
        f"certification {label} module has no physical origin",
    )
    origin = Path(cast(str, source)).resolve()
    _require(
        origin == Path(cast(str, spec_origin)).resolve(),
        f"certification {label} module file/spec origins differ",
    )
    expected_source_root = (root / "src").resolve()
    try:
        origin.relative_to(expected_source_root)
        relative_origin = origin.relative_to(root).as_posix()
    except ValueError as failure:
        raise GateFailure(
            f"certification {label} module origin is outside the exact checkout"
        ) from failure
    _git_text(
        root,
        label,
        "ls-files",
        "--error-unmatch",
        "--",
        relative_origin,
    )
    tracked_clean = (
        _git_text(
            root,
            label,
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
        )
        == ""
    )
    _require(
        tracked_clean,
        f"certification requires a tracked-clean {label} worktree",
    )
    try:
        source_sha256 = _sha256_bytes(origin.read_bytes())
    except OSError as failure:
        raise GateFailure(
            f"cannot authenticate certification {label} module origin: {failure}"
        ) from failure
    import_authority = _validated_module_import_authority(
        module,
        module_name=module_name,
        origin=origin,
        label=label,
    )
    return {
        "checkout_root": str(root),
        "head": _canonical_git_head(root, label),
        "module": module_name,
        "module_import_authority": import_authority,
        "module_origin": relative_origin,
        "module_sha256": source_sha256,
        "tracked_clean": True,
        "untracked_allowed": True,
    }


def _tracked_productive_python_paths(label: str) -> list[str]:
    checkout = _IMPORT_AUTHORITY_CHECKOUTS[label]
    checkout_root = cast(Path, checkout["checkout_root"])
    tracked = _git_text(checkout_root, label, "ls-files").splitlines()
    selected = []
    for relative_path in tracked:
        path = Path(relative_path)
        if path.suffix.casefold() != ".py":
            continue
        if label == "community":
            included = (
                bool(path.parts)
                and path.parts[0] in {"src", "tools"}
                or relative_path == "tests/mpulse7_gate_support.py"
            )
        else:
            included = bool(path.parts) and path.parts[0] == "src"
        if included:
            selected.append(relative_path)
    _require(selected, f"certification {label} Python source catalog is empty")
    return sorted(selected)


def _productive_python_catalog(
    *,
    community_git: Mapping[str, Any],
    dependency_checkouts: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    """Build stable authority for every tracked productive Python source."""

    global _ACTIVE_PRODUCTIVE_PYTHON_CATALOG
    global _CACHED_PRODUCTIVE_PYTHON_CATALOG

    _refresh_import_authority_checkouts()
    cache_key = tuple(
        sorted(
            (
                label,
                str(_IMPORT_AUTHORITY_CHECKOUTS[label]["checkout_root"]),
                str(
                    community_git["head"]
                    if label == "community"
                    else dependency_checkouts[label]["head"]
                ),
            )
            for label in ("community", "core", "okto_grafx")
        )
    )
    if (
        _CACHED_PRODUCTIVE_PYTHON_CATALOG is not None
        and _CACHED_PRODUCTIVE_PYTHON_CATALOG[0] == cache_key
    ):
        catalog = _CACHED_PRODUCTIVE_PYTHON_CATALOG[1]
        summary = _CACHED_PRODUCTIVE_PYTHON_CATALOG[2]
        _ACTIVE_PRODUCTIVE_PYTHON_CATALOG = catalog
        return catalog, summary

    catalog: dict[str, dict[str, str]] = {}
    entries_by_checkout: dict[str, list[dict[str, str]]] = {
        "community": [],
        "core": [],
        "okto_grafx": [],
    }
    for label in ("community", "core", "okto_grafx"):
        checkout_root = cast(
            Path,
            _IMPORT_AUTHORITY_CHECKOUTS[label]["checkout_root"],
        )
        for relative_path in _tracked_productive_python_paths(label):
            origin = (checkout_root / relative_path).resolve()
            _require(
                _productive_checkout_for_origin(origin) == label,
                f"certification {label} catalog source escapes its exact roots",
            )
            try:
                source = origin.read_bytes()
                code = compile(
                    source,
                    str(origin),
                    "exec",
                    dont_inherit=True,
                    optimize=sys.flags.optimize,
                )
            except (OSError, SyntaxError, TypeError, ValueError) as failure:
                raise GateFailure(
                    f"cannot build certification {label} Python catalog for "
                    f"{relative_path}: {failure}"
                ) from failure
            entry = {
                "checkout": label,
                "code_sha256": _code_object_sha256(
                    code,
                    include_filename=False,
                ),
                "source_path": relative_path,
                "source_sha256": _sha256_bytes(source),
            }
            key = _origin_key(origin)
            _require(
                key not in catalog,
                "certification Python source catalog contains duplicate origins",
            )
            catalog[key] = entry
            entries_by_checkout[label].append(entry)

    checkout_summaries = {
        label: {
            "catalog_sha256": canonical_sha256(entries_by_checkout[label]),
            "file_count": len(entries_by_checkout[label]),
        }
        for label in ("community", "core", "okto_grafx")
    }
    all_entries = [catalog[key] for key in sorted(catalog)]
    summary = {
        "catalog_sha256": canonical_sha256(all_entries),
        "checkouts": checkout_summaries,
        "policy": "all-loaded-python-imports-match-tracked-catalog",
        "validated": True,
    }
    _CACHED_PRODUCTIVE_PYTHON_CATALOG = (cache_key, catalog, summary)
    _ACTIVE_PRODUCTIVE_PYTHON_CATALOG = catalog
    return catalog, summary


def _loaded_productive_python_authority(
    catalog: Mapping[str, Mapping[str, str]],
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate every observed/loaded source against the stable full catalog."""

    loaded_origins: set[str] = set()
    for module in tuple(sys.modules.values()):
        if not isinstance(module, ModuleType):
            continue
        specification = getattr(module, "__spec__", None)
        candidates = (
            getattr(specification, "origin", None),
            getattr(module, "__file__", None),
        )
        for candidate in candidates:
            if type(candidate) is not str or candidate in {"built-in", "frozen"}:
                continue
            origin = Path(candidate).resolve()
            if _productive_checkout_for_origin(origin) is not None:
                loaded_origins.add(_origin_key(origin))
                break

    observed_origins = {
        key
        for key, observations in _OBSERVED_IMPORT_AUTHORITIES_BY_ORIGIN.items()
        if observations
        and type(observations[0].get("origin")) is str
        and _productive_checkout_for_origin(Path(cast(str, observations[0]["origin"])))
        is not None
    }
    for key in loaded_origins:
        _require(
            key in observed_origins,
            "certification productive Python module was preloaded before import "
            "authority measurement",
        )
    for key in observed_origins:
        entry = catalog.get(key)
        _require(
            entry is not None,
            "certification loaded productive Python source outside the tracked catalog",
        )
        for authority in _OBSERVED_IMPORT_AUTHORITIES_BY_ORIGIN[key]:
            _require(
                authority.get("format") == _IMPORT_AUTHORITY_FORMAT
                and authority.get("error") is None
                and authority.get("source_sha256") == entry["source_sha256"],
                "certification productive source differs from its catalog",
            )
            _require(
                authority.get("portable_code_sha256") == entry["code_sha256"],
                "certification productive loaded code differs from its catalog",
            )
    return dict(summary)


def collect_certification_process_authority() -> dict[str, Any]:
    """Collect canonical source authority inside any certification process."""

    _register_module_import_authority(GRAFX_AUTHORITY_MODULE)
    _register_module_import_authority(_FACTORY_AUTHORITY_MODULE)
    _register_module_import_authority(_CRASH_AUTHORITY_MODULE)
    expected_factories = {
        backend: resolve_factory(reference)
        for backend, reference in CERTIFICATION_FACTORY_REFS.items()
    }
    runner_record = _source_file_record(
        __file__,
        expected_path=RUNNER_SOURCE_PATH,
        label="runner",
    )
    runner_record["module_import_authority"] = _validated_self_import_authority(
        _RUNNER_IMPORT_AUTHORITY,
        origin=RUNNER_SOURCE_PATH.resolve(),
        label="runner",
    )
    for backend in BACKENDS:
        _source_file_record(
            inspect.getsourcefile(expected_factories[backend]),
            expected_path=FACTORY_SOURCE_PATH,
            label=f"{backend} factory",
        )
    factory_record = _source_file_record(
        inspect.getsourcefile(expected_factories["ladybug"]),
        expected_path=FACTORY_SOURCE_PATH,
        label="factory module",
    )
    factory_module = importlib.import_module(_FACTORY_AUTHORITY_MODULE)
    factory_record["module_import_authority"] = _validated_module_import_authority(
        factory_module,
        module_name=_FACTORY_AUTHORITY_MODULE,
        origin=FACTORY_SOURCE_PATH.resolve(),
        label="factory module",
    )
    _source_file_record(
        inspect.getsourcefile(run_isolated_board_query),
        expected_path=RUNNER_SOURCE_PATH,
        label="Board subprocess runner",
    )
    _source_file_record(
        inspect.getsourcefile(run_isolated_pulse_corpus_case),
        expected_path=RUNNER_SOURCE_PATH,
        label="Pulse corpus subprocess runner",
    )
    try:
        crash_module = importlib.import_module("mpulse7_crash_harness")
        crash_handler = crash_module.run_crash_point
    except (ImportError, AttributeError) as failure:
        raise GateFailure(
            f"cannot resolve certification crash harness authority: {failure}"
        ) from failure
    _require(callable(crash_handler), "certification crash harness is not callable")
    crash_record = _source_file_record(
        inspect.getsourcefile(crash_handler),
        expected_path=CRASH_HARNESS_SOURCE_PATH,
        label="crash harness",
    )
    crash_record["module_import_authority"] = _validated_module_import_authority(
        crash_module,
        module_name=_CRASH_AUTHORITY_MODULE,
        origin=CRASH_HARNESS_SOURCE_PATH.resolve(),
        label="crash harness",
    )
    source_records = {
        "crash_harness": crash_record,
        "factory": factory_record,
        "runner": runner_record,
    }
    community_git = _community_git_authority(source_records)
    dependency_checkouts = {
        "core": _module_checkout_authority(
            label="Core",
            module_name=CORE_AUTHORITY_MODULE,
            checkout_environment=CORE_CHECKOUT_ENV,
        ),
        "okto_grafx": _module_checkout_authority(
            label="okto_grafx",
            module_name=GRAFX_AUTHORITY_MODULE,
            checkout_environment=GRAFX_CHECKOUT_ENV,
        ),
    }
    productive_catalog, productive_catalog_summary = _productive_python_catalog(
        community_git=community_git,
        dependency_checkouts=dependency_checkouts,
    )
    loaded_python_sources = _loaded_productive_python_authority(
        productive_catalog,
        productive_catalog_summary,
    )
    return {
        "authority_format": CERTIFICATION_PROCESS_AUTHORITY_FORMAT,
        "community_git": community_git,
        "dependency_checkouts": dependency_checkouts,
        "loaded_python_sources": loaded_python_sources,
        "source_files": source_records,
    }


def _require_dependency_revision_authority(
    process_authority: Mapping[str, Any],
    source_revisions: Mapping[str, Any],
) -> None:
    dependencies = process_authority["dependency_checkouts"]
    _require(
        dependencies["core"]["head"] == source_revisions.get("core"),
        "certification Core HEAD differs from the authenticated manifest revision",
    )
    _require(
        dependencies["okto_grafx"]["head"] == source_revisions.get("okto_grafx_corpus"),
        "certification okto_grafx HEAD differs from the authenticated manifest revision",
    )


def _certification_authority(
    factories: Mapping[str, GateBackendFactory],
    *,
    query_runner: IsolatedQueryRunner,
    pulse_corpus_runner: IsolatedPulseCorpusRunner,
    source_revisions: Mapping[str, Any],
) -> dict[str, Any]:
    """Authenticate the only callables allowed to emit a certifiable receipt."""

    expected_factories = {
        backend: resolve_factory(reference)
        for backend, reference in CERTIFICATION_FACTORY_REFS.items()
    }
    for backend in BACKENDS:
        _require(
            factories[backend] is expected_factories[backend],
            f"certification requires the exact {backend} factory authority",
        )
    _require(
        query_runner is run_isolated_board_query,
        "certification requires the standard Board subprocess runner",
    )
    _require(
        pulse_corpus_runner is run_isolated_pulse_corpus_case,
        "certification requires the standard Pulse corpus subprocess runner",
    )

    process_authority = collect_certification_process_authority()
    _require_dependency_revision_authority(process_authority, source_revisions)
    process_authority_sha256 = canonical_sha256(process_authority)
    return {
        "crash_harness_ref": CERTIFICATION_CRASH_HARNESS_REF,
        "factory_refs": dict(CERTIFICATION_FACTORY_REFS),
        "process_authority": process_authority,
        "process_authority_sha256": process_authority_sha256,
        "query_runner_refs": {
            "board": CERTIFICATION_BOARD_RUNNER_REF,
            "pulse_corpus": CERTIFICATION_PULSE_RUNNER_REF,
        },
        "source_revisions": {
            "core": source_revisions.get("core"),
            "okto_grafx_corpus": source_revisions.get("okto_grafx_corpus"),
        },
    }


def _worker_execution_authority_sha256(
    factory: GateBackendFactory,
    context: GateBackendContext,
) -> str | None:
    expected_digest = context.certification_process_authority_sha256
    if expected_digest is None:
        return None
    expected_factory = resolve_factory(CERTIFICATION_FACTORY_REFS[context.backend])
    _require(
        factory is expected_factory,
        f"{context.backend} worker executed a non-certification factory",
    )
    observed_digest = canonical_sha256(collect_certification_process_authority())
    _require(
        observed_digest == expected_digest,
        f"{context.backend} worker execution authority differs from supervisor",
    )
    return observed_digest


async def _maybe_await(value: _T | Awaitable[_T]) -> _T:
    if inspect.isawaitable(value):
        return await cast(Awaitable[_T], value)
    return cast(_T, value)


async def _open_backend(
    factory: GateBackendFactory,
    context: GateBackendContext,
) -> GateBackend:
    try:
        backend = await _maybe_await(factory(context))
    except BaseException as failure:
        raise GateFailure(
            f"{context.backend} backend factory failed: "
            f"{type(failure).__name__}: {failure}"
        ) from failure
    for attribute in (
        "semantic_store",
        "graph_transaction",
        "identity",
        "reopen_recover_verify_fingerprint",
        "observe_fingerprints",
        "run_crash_point",
        "run_pulse_corpus_case",
        "run_raw_execute_family",
        "run_receipt_bound_scenario",
        "close",
    ):
        if not hasattr(backend, attribute):
            raise GateFailure(
                f"{context.backend} backend factory omitted required member {attribute}"
            )
    return backend


async def _close_backend(backend: GateBackend) -> None:
    await _maybe_await(backend.close())


def _sha256_text(value: Any, *, field: str) -> str:
    _require(
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value),
        f"{field} must be a lowercase SHA-256 digest",
    )
    return cast(str, value)


def _stable_storage_identity(identity: Mapping[str, Any]) -> tuple[str, str]:
    storage_identity = identity.get("storage_identity")
    generation = identity.get("generation")
    _require(
        type(storage_identity) is str and bool(storage_identity),
        "backend identity omitted storage_identity",
    )
    _require(
        type(generation) is str and bool(generation),
        "backend identity omitted generation",
    )
    return cast(str, storage_identity), cast(str, generation)


def _require_same_storage(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
    *,
    context: str,
) -> None:
    expected_storage = _stable_storage_identity(expected)
    observed_storage = _stable_storage_identity(observed)
    _require(
        observed_storage == expected_storage,
        f"{context} opened a different storage identity/generation",
    )


async def _observed_fingerprints(
    backend: GateBackend, context: GateBackendContext
) -> dict[str, str]:
    value = await _maybe_await(backend.observe_fingerprints())
    _require(
        type(value) is dict,
        f"{context.backend} fingerprint observation must be a JSON object",
    )
    fingerprints = dict(value)
    _require(
        set(fingerprints) == {"logical_graph_sha256", "trace_model_sha256"},
        f"{context.backend} fingerprint observation is not the closed shape",
    )
    return {
        "logical_graph_sha256": _sha256_text(
            fingerprints["logical_graph_sha256"],
            field=f"{context.backend} logical_graph_sha256",
        ),
        "trace_model_sha256": _sha256_text(
            fingerprints["trace_model_sha256"],
            field=f"{context.backend} trace_model_sha256",
        ),
    }


def _projection_intent(payload: Mapping[str, Any]) -> ProjectionActiveSetIntent:
    return ProjectionActiveSetIntent(
        owner_type=str(payload["owner_type"]),
        owner_id=str(payload["owner_id"]),
        namespace=str(payload["namespace"]),
        owner_node_id=(
            None
            if payload.get("owner_node_id") is None
            else str(payload["owner_node_id"])
        ),
        active_nodes=tuple(
            ProjectionNodeRef(
                node_type=str(value["node_type"]),
                node_id=str(value["node_id"]),
                source_artifact_ref=str(value["source_artifact_ref"]),
            )
            for value in payload.get("active_nodes", ())
        ),
        active_edges=tuple(
            ProjectionEdgeRef(
                edge_type=str(value["edge_type"]),
                from_type=str(value["from_type"]),
                to_type=str(value["to_type"]),
                from_id=str(value["from_id"]),
                to_id=str(value["to_id"]),
                rule_id=str(value["rule_id"]),
            )
            for value in payload.get("active_edges", ())
        ),
    )


async def _dispatch_store_operation(
    store: Any,
    board_id: str,
    family: str,
    payload: Mapping[str, Any],
) -> None:
    if family == "create_node":
        await _maybe_await(
            store.create_node(
                board_id,
                payload["node_type"],
                payload["node_id"],
                dict(payload["attrs"]),
            )
        )
    elif family == "create_edge":
        await _maybe_await(
            store.create_edge(
                board_id,
                payload["edge_type"],
                payload["from_id"],
                payload["to_id"],
                dict(payload["attrs"]),
                from_type=payload["from_type"],
                to_type=payload["to_type"],
            )
        )
    elif family == "update_node":
        await _maybe_await(
            store.update_node(
                board_id,
                payload["node_type"],
                payload["node_id"],
                dict(payload["attrs"]),
            )
        )
    elif family == "mark_superseded":
        await _maybe_await(
            store.mark_superseded(
                board_id,
                payload["node_type"],
                payload["node_id"],
                superseded_by=payload["superseded_by"],
                superseded_at=payload["superseded_at"],
                revocation_reason=payload["revocation_reason"],
            )
        )
    elif family == "increment_attestation":
        await _maybe_await(
            store.increment_attestation(
                board_id,
                payload["node_type"],
                payload["node_id"],
                attested_at=payload["attested_at"],
            )
        )
    elif family == "delete_edges_by_session":
        await _maybe_await(
            store.delete_edges_by_session(board_id, payload["session_id"])
        )
    elif family == "delete_nodes_by_session":
        await _maybe_await(
            store.delete_nodes_by_session(board_id, payload["session_id"])
        )
    else:
        raise GateFailure(f"unsupported SemanticGraphStore trace family: {family}")


async def _dispatch_scope_operation(
    scope: Any,
    family: str,
    payload: Mapping[str, Any],
) -> None:
    if family == "create_node":
        await _maybe_await(
            scope.create_node(
                payload["node_type"],
                payload["node_id"],
                dict(payload["attrs"]),
                source_session_id=payload["source_session_id"],
            )
        )
    elif family == "create_edge":
        await _maybe_await(
            scope.create_edge(
                payload["edge_type"],
                payload["from_type"],
                payload["to_type"],
                payload["from_id"],
                payload["to_id"],
                dict(payload["attrs"]),
            )
        )
    elif family == "update_node":
        await _maybe_await(
            scope.update_node(
                payload["node_type"], payload["node_id"], dict(payload["attrs"])
            )
        )
    elif family == "replace_node_payload":
        await _maybe_await(
            scope.replace_node_payload(
                payload["node_type"],
                payload["node_id"],
                dict(payload["attrs"]),
                source_session_id=payload["source_session_id"],
            )
        )
    elif family == "mark_superseded":
        await _maybe_await(
            scope.mark_superseded(
                payload["node_type"],
                payload["node_id"],
                superseded_by=payload["superseded_by"],
                superseded_at=payload["superseded_at"],
                revocation_reason=payload["revocation_reason"],
            )
        )
    elif family == "increment_attestation":
        await _maybe_await(
            scope.increment_attestation(
                payload["node_type"],
                payload["node_id"],
                attested_at=payload["attested_at"],
            )
        )
    elif family == "replace_with_source_deleted_tombstone":
        await _maybe_await(
            scope.replace_with_source_deleted_tombstone(
                payload["node_type"],
                payload["node_id"],
                graph_layer=payload["graph_layer"],
                maturity_status=payload["maturity_status"],
                revocation_reason=payload["revocation_reason"],
                relevance_score=payload["relevance_score"],
            )
        )
    elif family == "reconcile_spec_lineage_parent":
        await _maybe_await(
            scope.reconcile_spec_lineage_parent(
                payload["source_id"], payload["target_id"], dict(payload["attrs"])
            )
        )
    elif family == "clear_spec_lineage_parent":
        await _maybe_await(scope.clear_spec_lineage_parent(payload["source_id"]))
    elif family == "reconcile_projection_active_set":
        await _maybe_await(
            scope.reconcile_projection_active_set(_projection_intent(payload))
        )
    elif family == "delete_edges_by_session":
        await _maybe_await(scope.delete_edges_by_session(payload["session_id"]))
    elif family == "delete_nodes_by_session":
        await _maybe_await(
            scope.delete_nodes_by_session(
                payload["session_id"], tuple(payload["node_types"])
            )
        )
    else:
        raise GateFailure(f"unsupported GraphTransactionScope trace family: {family}")


async def _execute_operation(
    backend: GateBackend,
    context: GateBackendContext,
    operation: Mapping[str, Any],
) -> None:
    method = str(operation["method"])
    family = str(operation["family"])
    payload = cast(Mapping[str, Any], operation["payload"])
    try:
        if method.startswith("SemanticGraphStore."):
            _require(
                method == f"SemanticGraphStore.{family}",
                f"trace method/family mismatch: {method} / {family}",
            )
            await _dispatch_store_operation(
                backend.semantic_store, context.board_id, family, payload
            )
            return
        expected_scope_methods = {
            f"GraphTransactionScope.{family}",
            f"GraphTransactionScopeExtension.{family}",
        }
        _require(
            method in expected_scope_methods,
            f"trace method/family mismatch: {method} / {family}",
        )
        scope = await _maybe_await(backend.graph_transaction.begin(context.board_id))
        try:
            await _dispatch_scope_operation(scope, family, payload)
            await _maybe_await(scope.commit())
        except BaseException as primary:
            try:
                await _maybe_await(scope.rollback())
            except BaseException as cleanup:  # noqa: BLE001 - preserve primary
                primary.add_note(
                    "transaction rollback also failed: "
                    f"{type(cleanup).__name__}: {cleanup}"
                )
            raise
    except GateFailure:
        raise
    except BaseException as failure:
        raise GateFailure(
            f"{context.backend} failed {operation['operation_id']} "
            f"({method}): {type(failure).__name__}: {failure}"
        ) from failure


def _current_rss_bytes() -> int:
    if sys.platform == "win32":

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(
            handle,
            ctypes.byref(counters),
            counters.cb,
        )
        if not ok:
            raise GateFailure("Windows could not report process RSS")
        return int(counters.WorkingSetSize)
    status = Path("/proc/self/statm")
    if status.is_file():
        try:
            resident_pages = int(status.read_text(encoding="ascii").split()[1])
            return resident_pages * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, IndexError):
            pass
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024
    except (ImportError, OSError, ValueError) as failure:
        raise GateFailure(
            f"the process RSS could not be measured: {failure}"
        ) from failure


def _percentile_ms(latencies_ns: Sequence[int], percentile: float) -> float:
    _require(bool(latencies_ns), "cannot calculate a percentile without operations")
    ordered = sorted(latencies_ns)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index] / 1_000_000, 6)


async def _backend_identity(
    backend: GateBackend, context: GateBackendContext
) -> dict[str, Any]:
    value = await _maybe_await(backend.identity())
    _require(type(value) is dict, f"{context.backend} identity must be a JSON object")
    identity = dict(value)
    _require(
        identity.get("backend") == context.backend,
        f"{context.backend} factory returned a mismatched backend identity",
    )
    _require(
        type(identity.get("backend_version")) is str
        and bool(identity["backend_version"]),
        f"{context.backend} identity omitted backend_version",
    )
    if context.backend == "grafx":
        _require(
            identity.get("descriptor_revalidation")
            == REQUIRED_GRAFX_DESCRIPTOR_REVALIDATION,
            "grafx identity did not prove descriptor_revalidation=generation",
        )
    _stable_storage_identity(identity)
    canonical_json_bytes(identity)
    return identity


async def _run_backend_trace(
    inputs: FrozenGateInputs,
    factory: GateBackendFactory,
    context: GateBackendContext,
) -> dict[str, Any]:
    backend = await _open_backend(factory, context)
    _worker_execution_authority_sha256(factory, context)
    primary: BaseException | None = None
    try:
        identity = await _backend_identity(backend, context)
        checkpoints = {
            int(value["after_operations"]): value
            for value in inputs.manifest["trace"]["checkpoints"]
        }
        recovery_boundaries = frozenset(
            int(value)
            for value in inputs.manifest["reopen_recovery_cycles"]["after_operations"]
        )
        expected_scope = str(inputs.manifest["acceptance"]["required_verify_scope"])
        latencies: list[int] = []
        recoveries: list[dict[str, Any]] = []
        peak_rss = _current_rss_bytes()
        started_ns = time.perf_counter_ns()
        for operation in inputs.operations:
            operation_started_ns = time.perf_counter_ns()
            await _execute_operation(backend, context, operation)
            latencies.append(time.perf_counter_ns() - operation_started_ns)
            peak_rss = max(peak_rss, _current_rss_bytes())
            sequence = int(operation["sequence"])
            if sequence not in recovery_boundaries:
                continue
            expected_fingerprint = str(
                checkpoints[sequence]["model_fingerprint_sha256"]
            )
            observation = await _maybe_await(
                backend.reopen_recover_verify_fingerprint(
                    after_operations=sequence,
                    verify_scope=expected_scope,
                )
            )
            _require(
                type(observation) is dict,
                f"{context.backend} recovery callback returned a non-object",
            )
            observed = dict(observation)
            _require(
                set(observed)
                == {
                    "after_operations",
                    "closed",
                    "fingerprint_logical_graph_sha256",
                    "fingerprint_trace_model_sha256",
                    "generation",
                    "recovered",
                    "reopened",
                    "storage_identity",
                    "verify_ok",
                    "verify_scope",
                },
                f"{context.backend} recovery boundary {sequence} returned an open evidence shape",
            )
            for flag in ("closed", "reopened", "recovered", "verify_ok"):
                _require(
                    observed.get(flag) is True,
                    f"{context.backend} recovery boundary {sequence} did not prove {flag}",
                )
            _require(
                observed.get("after_operations") == sequence,
                f"{context.backend} recovery boundary returned a mismatched sequence",
            )
            _require(
                observed.get("verify_scope") == expected_scope,
                f"{context.backend} recovery boundary {sequence} did not verify all",
            )
            reopened_identity = await _backend_identity(backend, context)
            _require_same_storage(
                identity,
                reopened_identity,
                context=f"{context.backend} recovery boundary {sequence}",
            )
            storage_identity, generation = _stable_storage_identity(reopened_identity)
            _require(
                observed.get("storage_identity") == storage_identity
                and observed.get("generation") == generation,
                f"{context.backend} recovery boundary {sequence} identity evidence diverged",
            )
            fingerprints = await _observed_fingerprints(backend, context)
            _require(
                observed.get("fingerprint_trace_model_sha256")
                == fingerprints["trace_model_sha256"]
                and observed.get("fingerprint_logical_graph_sha256")
                == fingerprints["logical_graph_sha256"],
                f"{context.backend} recovery boundary {sequence} fingerprint evidence diverged",
            )
            _require(
                fingerprints["trace_model_sha256"] == expected_fingerprint,
                f"{context.backend} recovery boundary {sequence} diverged",
            )
            recoveries.append(
                {
                    "after_operations": sequence,
                    "fingerprint_logical_graph_sha256": fingerprints[
                        "logical_graph_sha256"
                    ],
                    "fingerprint_trace_model_sha256": fingerprints[
                        "trace_model_sha256"
                    ],
                    "verify_ok": True,
                    "verify_scope": expected_scope,
                }
            )
        elapsed_ns = time.perf_counter_ns() - started_ns
        final_identity = await _backend_identity(backend, context)
        _require_same_storage(
            identity,
            final_identity,
            context=f"{context.backend} final trace",
        )
        final_fingerprints = await _observed_fingerprints(backend, context)
        expected_final = str(inputs.manifest["trace"]["final_model_fingerprint_sha256"])
        _require(
            final_fingerprints["trace_model_sha256"] == expected_final,
            f"{context.backend} final logical fingerprint diverged",
        )
        operation_count = len(inputs.operations)
        metrics = {
            "latency_ms_p50": _percentile_ms(latencies, 0.50),
            "latency_ms_p90": _percentile_ms(latencies, 0.90),
            "latency_ms_p99": _percentile_ms(latencies, 0.99),
            "peak_memory_bytes": peak_rss,
            "throughput_ops_per_second": round(
                operation_count / (elapsed_ns / 1_000_000_000), 6
            ),
        }
        return {
            "backend": context.backend,
            "backend_version": identity["backend_version"],
            "final_fingerprint_logical_graph_sha256": final_fingerprints[
                "logical_graph_sha256"
            ],
            "final_fingerprint_trace_model_sha256": final_fingerprints[
                "trace_model_sha256"
            ],
            "identity": identity,
            "metrics": metrics,
            "operation_count": operation_count,
            "reopen_recovery_cycle_count": len(recoveries),
            "recoveries": recoveries,
        }
    except BaseException as failure:
        primary = failure
        raise
    finally:
        try:
            await _close_backend(backend)
        except BaseException as cleanup:
            if primary is None:
                raise GateFailure(
                    f"{context.backend} backend close failed: "
                    f"{type(cleanup).__name__}: {cleanup}"
                ) from cleanup
            primary.add_note(
                f"{context.backend} backend close also failed: "
                f"{type(cleanup).__name__}: {cleanup}"
            )


def _require_backend_trace_parity(records: Sequence[Mapping[str, Any]]) -> None:
    _require(len(records) == 2, "bilateral trace evidence requires two backends")
    by_backend = {str(record["backend"]): record for record in records}
    _require(
        set(by_backend) == set(BACKENDS),
        "bilateral trace evidence omitted a backend",
    )
    ladybug = by_backend["ladybug"]
    grafx = by_backend["grafx"]
    _require(
        ladybug["final_fingerprint_logical_graph_sha256"]
        == grafx["final_fingerprint_logical_graph_sha256"],
        "the final M-PULSE-5 logical fingerprints differ between backends",
    )
    ladybug_recoveries = ladybug["recoveries"]
    grafx_recoveries = grafx["recoveries"]
    _require(
        len(ladybug_recoveries) == len(grafx_recoveries),
        "the backend recovery evidence counts differ",
    )
    for left, right in zip(ladybug_recoveries, grafx_recoveries, strict=True):
        _require(
            left["after_operations"] == right["after_operations"]
            and left["fingerprint_logical_graph_sha256"]
            == right["fingerprint_logical_graph_sha256"],
            "the recovery M-PULSE-5 logical fingerprints differ between backends",
        )


def _unit_vector(specification: Mapping[str, Any]) -> list[float]:
    _require(
        specification.get("generator") == "splitmix64-unit-vector/1",
        "the Board supplement named an unsupported vector generator",
    )
    seed_text = specification.get("seed")
    dimensions = specification.get("dimensions")
    _require(
        type(seed_text) is str
        and seed_text.startswith("0x")
        and type(dimensions) is int
        and dimensions > 0,
        "the Board supplement vector specification is invalid",
    )
    random = SplitMix64(int(seed_text, 16))
    scale = float(1 << 64)
    values = [(random.next_u64() / scale) * 2.0 - 1.0 for _ in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values))
    _require(norm > 0.0, "the Board supplement generated a zero vector")
    return [value / norm for value in values]


def _query_arguments(value: Any, *, board_id: str) -> Any:
    if value == "${board_id}":
        return board_id
    if type(value) is dict:
        if "generator" in value:
            return _unit_vector(value)
        return {
            str(key): _query_arguments(item, board_id=board_id)
            for key, item in value.items()
        }
    if type(value) is list:
        return [_query_arguments(item, board_id=board_id) for item in value]
    return value


def _json_value(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        _require(math.isfinite(value), "query result contains a non-finite float")
        return value
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            _require(type(key) is str, "query result mapping key is not text")
            result[key] = _json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise GateFailure(
        f"query result contains a non-canonical value: {type(value).__name__}"
    )


def _normalize_query_result(result: Any, ordering: str) -> tuple[Any, int]:
    canonical = _json_value(result)
    if isinstance(canonical, list):
        rows = canonical
    else:
        rows = [canonical]
    if ordering == "ordered":
        return rows, len(rows)
    _require(ordering == "multiset", f"unsupported query ordering: {ordering}")
    encoded_rows = sorted(canonical_json_bytes(row) for row in rows)
    return [json.loads(row) for row in encoded_rows], len(rows)


def _execute_board_case(
    backend: GateBackend,
    context: GateBackendContext,
    case: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    fingerprints: Mapping[str, str],
    execution_authority_sha256: str | None,
) -> IsolatedQueryResult:
    case_id = str(case["id"])
    method = str(case["method"])
    ordering = str(case["ordering"])
    _require(method in _BOARD_QUERY_METHODS, f"open Board query method: {method}")
    arguments = _query_arguments(case["arguments"], board_id=context.board_id)
    _require(
        type(arguments) is dict, f"Board query {case_id} arguments are not an object"
    )
    if type(arguments.get("filters")) is dict:
        arguments["filters"] = QueryFilters(**arguments["filters"])
    result = getattr(backend.semantic_store, method)(**arguments)
    if inspect.isawaitable(result):
        raise GateFailure(f"Board query {case_id} unexpectedly returned an awaitable")
    normalized, row_count = _normalize_query_result(result, ordering)
    storage_identity, generation = _stable_storage_identity(identity)
    return IsolatedQueryResult(
        case_id=case_id,
        fingerprint_logical_graph_sha256=fingerprints["logical_graph_sha256"],
        fingerprint_trace_model_sha256=fingerprints["trace_model_sha256"],
        generation=generation,
        ordering=ordering,
        result_sha256=canonical_sha256(normalized),
        row_count=row_count,
        storage_identity=storage_identity,
        worker_pid=os.getpid(),
        execution_authority_sha256=execution_authority_sha256,
    )


def _release_isolated_operation(control: _IsolatedOperationControl | None) -> None:
    """Declare authenticated readiness and wait for the supervisor to start."""

    if control is None:
        return
    control.ready.set()
    _require(
        control.start.wait(_ISOLATED_WORKER_CONTROL_TIMEOUT_SECONDS),
        "isolated worker supervisor did not release the prepared operation",
    )
    with control.operation_started_ns.get_lock():
        control.operation_started_ns.value = time.monotonic_ns()
    control.operation_started.set()


def _finish_isolated_operation(control: _IsolatedOperationControl | None) -> None:
    if control is not None:
        with control.operation_finished_ns.get_lock():
            control.operation_finished_ns.value = time.monotonic_ns()
        control.operation_finished.set()


async def _query_worker_async(
    output_path: Path,
    factory: GateBackendFactory,
    context: GateBackendContext,
    case: Mapping[str, Any],
    control: _IsolatedOperationControl | None = None,
) -> None:
    backend: GateBackend | None = None
    primary: BaseException | None = None
    try:
        execution_authority_sha256 = _worker_execution_authority_sha256(
            factory,
            context,
        )
        backend = await _open_backend(factory, context)
        _worker_execution_authority_sha256(factory, context)
        identity = await _backend_identity(backend, context)
        fingerprints = await _observed_fingerprints(backend, context)
        _release_isolated_operation(control)
        try:
            result = _execute_board_case(
                backend,
                context,
                case,
                identity=identity,
                fingerprints=fingerprints,
                execution_authority_sha256=execution_authority_sha256,
            )
        finally:
            _finish_isolated_operation(control)
        after_identity = await _backend_identity(backend, context)
        _require_same_storage(
            identity,
            after_identity,
            context=f"Board query {case['id']}",
        )
        _require(
            await _observed_fingerprints(backend, context) == fingerprints,
            f"Board query {case['id']} mutated its fixed view",
        )
        payload: dict[str, Any] = {"worker_status": "ok", **asdict(result)}
    except BaseException as failure:  # noqa: BLE001 - child evidence boundary
        primary = failure
        payload = {
            "worker_status": "failed",
            "error_type": type(failure).__name__,
            "error": str(failure),
            "traceback": traceback.format_exc(limit=20),
        }
    finally:
        if backend is not None:
            try:
                await _close_backend(backend)
            except BaseException as cleanup:  # noqa: BLE001 - child evidence
                if primary is None:
                    payload = {
                        "worker_status": "failed",
                        "error_type": type(cleanup).__name__,
                        "error": f"backend close failed: {cleanup}",
                        "traceback": traceback.format_exc(limit=20),
                    }
                else:
                    payload["close_error"] = f"{type(cleanup).__name__}: {cleanup}"
    output_path.write_bytes(canonical_json_bytes(payload))


def _query_worker_entry(
    output_path: str,
    factory: GateBackendFactory,
    context: GateBackendContext,
    case: Mapping[str, Any],
    control: _IsolatedOperationControl | None = None,
) -> None:
    asyncio.run(
        _query_worker_async(Path(output_path), factory, context, case, control)
    )


def _wait_for_isolated_worker_event(
    process: Any,
    event: Any,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if event.is_set():
            return True
        if not process.is_alive():
            return event.is_set()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return event.is_set()
        event.wait(min(_ISOLATED_WORKER_EVENT_POLL_SECONDS, remaining))


def _stop_isolated_worker(process: Any) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    _require(
        not process.is_alive(),
        "isolated worker remained alive after terminate and kill",
    )


def _isolated_operation_timestamp(counter: Any, *, label: str) -> int:
    with counter.get_lock():
        value = int(counter.value)
    _require(value > 0, f"isolated worker omitted its {label} timestamp")
    return value


def _run_isolated_operation_worker(
    *,
    case_label: str,
    output_prefix: str,
    target: Callable[..., None],
    target_args: tuple[Any, ...],
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run one operation with separate fail-closed setup/operation/finalize bounds."""

    spawn = multiprocessing.get_context("spawn")
    control = _IsolatedOperationControl(
        operation_finished=spawn.Event(),
        operation_finished_ns=spawn.Value(ctypes.c_longlong, 0),
        operation_started=spawn.Event(),
        operation_started_ns=spawn.Value(ctypes.c_longlong, 0),
        ready=spawn.Event(),
        start=spawn.Event(),
    )
    with tempfile.TemporaryDirectory(prefix=output_prefix) as temporary:
        output_path = Path(temporary) / "result.json"
        process = spawn.Process(
            target=target,
            args=(str(output_path), *target_args, control),
            daemon=False,
        )
        try:
            process.start()
        except BaseException as failure:
            raise GateFailure(
                f"cannot start isolated {case_label}: "
                f"{type(failure).__name__}: {failure}"
            ) from failure
        try:
            ready = _wait_for_isolated_worker_event(
                process,
                control.ready,
                _ISOLATED_WORKER_CONTROL_TIMEOUT_SECONDS,
            )
            _require(
                ready,
                f"{case_label} worker did not reach authenticated readiness within "
                f"{_ISOLATED_WORKER_CONTROL_TIMEOUT_SECONDS}s",
            )
            control.start.set()
            started = _wait_for_isolated_worker_event(
                process,
                control.operation_started,
                _ISOLATED_WORKER_CONTROL_TIMEOUT_SECONDS,
            )
            _require(
                started,
                f"{case_label} worker did not start its prepared operation within "
                f"{_ISOLATED_WORKER_CONTROL_TIMEOUT_SECONDS}s",
            )
            operation_started_ns = _isolated_operation_timestamp(
                control.operation_started_ns,
                label="operation-start",
            )
            elapsed_ns = max(0, time.monotonic_ns() - operation_started_ns)
            remaining_seconds = max(
                0.0,
                timeout_seconds - elapsed_ns / 1_000_000_000,
            )
            finished = _wait_for_isolated_worker_event(
                process,
                control.operation_finished,
                remaining_seconds,
            )
            if not finished:
                with control.operation_finished_ns.get_lock():
                    finished = int(control.operation_finished_ns.value) > 0
            _require(
                finished,
                f"{case_label} exceeded the real {timeout_seconds}s "
                "operation watchdog",
            )
            operation_finished_ns = _isolated_operation_timestamp(
                control.operation_finished_ns,
                label="operation-finish",
            )
            _require(
                operation_finished_ns >= operation_started_ns,
                "isolated worker operation timestamps are not monotonic",
            )
            _require(
                operation_finished_ns - operation_started_ns
                <= timeout_seconds * 1_000_000_000,
                f"{case_label} exceeded the real {timeout_seconds}s "
                "operation watchdog",
            )

            process.join(_ISOLATED_WORKER_CONTROL_TIMEOUT_SECONDS)
            _require(
                not process.is_alive(),
                f"{case_label} worker did not finish post-validation and close within "
                f"{_ISOLATED_WORKER_CONTROL_TIMEOUT_SECONDS}s",
            )
            _require(
                process.exitcode == 0,
                f"{case_label} worker exited with code {process.exitcode}",
            )
            _require(
                control.operation_started.is_set(),
                f"{case_label} worker exited without starting its operation",
            )
            _require(
                control.operation_finished.is_set(),
                f"{case_label} worker exited without completing its operation",
            )
            operation_started_ns = _isolated_operation_timestamp(
                control.operation_started_ns,
                label="operation-start",
            )
            operation_finished_ns = _isolated_operation_timestamp(
                control.operation_finished_ns,
                label="operation-finish",
            )
            _require(
                operation_finished_ns >= operation_started_ns,
                "isolated worker operation timestamps are not monotonic",
            )
            _require(
                operation_finished_ns - operation_started_ns
                <= timeout_seconds * 1_000_000_000,
                f"{case_label} exceeded the real {timeout_seconds}s "
                "operation watchdog",
            )
            _require(output_path.is_file(), f"{case_label} produced no receipt")
            document = _load_json_document(output_path)
            _require(type(document) is dict, "isolated worker receipt is not an object")
            return document
        except BaseException:
            _stop_isolated_worker(process)
            raise


def run_isolated_board_query(
    factory: GateBackendFactory,
    context: GateBackendContext,
    case: Mapping[str, Any],
    timeout_seconds: int,
) -> IsolatedQueryResult:
    """Run one Board case with a real 30s operation-only watchdog."""

    _require(timeout_seconds > 0, "the query watchdog must be positive")
    document = _run_isolated_operation_worker(
        case_label=f"Board query {case['id']}",
        output_prefix="mpulse7-query-",
        target=_query_worker_entry,
        target_args=(factory, context, dict(case)),
        timeout_seconds=timeout_seconds,
    )
    if document.get("worker_status") != "ok":
        raise GateFailure(
            f"Board query {case['id']} failed in its worker: "
            f"{document.get('error_type')}: {document.get('error')}"
        )
    try:
        return IsolatedQueryResult(
            case_id=str(document["case_id"]),
            fingerprint_logical_graph_sha256=_sha256_text(
                document["fingerprint_logical_graph_sha256"],
                field="isolated Board logical_graph_sha256",
            ),
            fingerprint_trace_model_sha256=_sha256_text(
                document["fingerprint_trace_model_sha256"],
                field="isolated Board trace_model_sha256",
            ),
            generation=str(document["generation"]),
            ordering=str(document["ordering"]),
            result_sha256=str(document["result_sha256"]),
            row_count=int(document["row_count"]),
            storage_identity=str(document["storage_identity"]),
            worker_pid=int(document["worker_pid"]),
            execution_authority_sha256=(
                None
                if document.get("execution_authority_sha256") is None
                else _sha256_text(
                    document["execution_authority_sha256"],
                    field="isolated Board execution authority SHA-256",
                )
            ),
        )
    except (KeyError, TypeError, ValueError) as failure:
        raise GateFailure(
            f"Board query {case['id']} returned an invalid worker receipt"
        ) from failure


async def _run_queries(
    inputs: FrozenGateInputs,
    factories: Mapping[str, GateBackendFactory],
    contexts: Mapping[str, GateBackendContext],
    query_runner: IsolatedQueryRunner,
    backend_records: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cases = inputs.manifest["board_result_supplement"]["queries"]
    timeout = int(
        inputs.manifest["board_result_supplement"]["external_timeout_seconds"]
    )
    _require(timeout == 30, "the Board query watchdog must remain exactly 30 seconds")
    receipts: dict[str, dict[str, IsolatedQueryResult]] = {
        backend: {} for backend in BACKENDS
    }
    for backend in BACKENDS:
        for case in cases:
            result = await _maybe_await(
                query_runner(factories[backend], contexts[backend], case, timeout)
            )
            _require(
                isinstance(result, IsolatedQueryResult),
                f"{backend} query runner returned an invalid receipt",
            )
            _require(
                type(result.worker_pid) is int
                and result.worker_pid > 0
                and result.worker_pid != os.getpid(),
                f"{backend} Board query {result.case_id} was not externally isolated",
            )
            expected_execution_authority = contexts[
                backend
            ].certification_process_authority_sha256
            if expected_execution_authority is not None:
                _require(
                    result.execution_authority_sha256 == expected_execution_authority,
                    f"{backend} Board query {result.case_id} executed under "
                    "different source authority",
                )
            _require(
                result.case_id == case["id"] and result.ordering == case["ordering"],
                f"{backend} query runner returned a mismatched case receipt",
            )
            expected = backend_records[backend]
            expected_storage, expected_generation = _stable_storage_identity(
                cast(Mapping[str, Any], expected["identity"])
            )
            _require(
                result.storage_identity == expected_storage
                and result.generation == expected_generation,
                f"{backend} Board query {result.case_id} opened the wrong generation",
            )
            _require(
                result.fingerprint_trace_model_sha256
                == expected["final_fingerprint_trace_model_sha256"]
                and result.fingerprint_logical_graph_sha256
                == expected["final_fingerprint_logical_graph_sha256"],
                f"{backend} Board query {result.case_id} opened the wrong fingerprint",
            )
            _sha256_text(
                result.result_sha256,
                field=f"{backend} Board query {result.case_id} result_sha256",
            )
            receipts[backend][result.case_id] = result

    comparisons: list[dict[str, Any]] = []
    divergences: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["id"])
        ladybug = receipts["ladybug"][case_id]
        grafx = receipts["grafx"][case_id]
        equal = ladybug.result_sha256 == grafx.result_sha256
        comparison = {
            "case_id": case_id,
            "execution_authority_sha256": ladybug.execution_authority_sha256,
            "grafx_result_sha256": grafx.result_sha256,
            "ladybug_result_sha256": ladybug.result_sha256,
            "ordering": case["ordering"],
            "results_equal": equal,
        }
        comparisons.append(comparison)
        if not equal:
            divergences.append(comparison)
    return comparisons, divergences


def _normalize_pulse_corpus_callback(
    value: Any,
    *,
    entry: Mapping[str, Any],
    identity: Mapping[str, Any],
    fingerprints: Mapping[str, str],
    execution_authority_sha256: str | None = None,
) -> IsolatedPulseCorpusResult:
    entry_id = str(entry["id"])
    entry_class = str(entry["class"])
    _require(
        type(value) is dict,
        f"Pulse corpus case {entry_id} returned no closed evidence object",
    )
    document = dict(value)
    _require(
        set(document) == {"class", "classification", "id", "result", "status"},
        f"Pulse corpus case {entry_id} returned an open evidence shape",
    )
    expected_status = "not_executable" if entry_class == "fragment" else "executed"
    _require(
        document["id"] == entry_id
        and document["class"] == entry_class
        and document["classification"] == entry["classification"]
        and document["status"] == expected_status,
        f"Pulse corpus case {entry_id} returned mismatched classification evidence",
    )
    raw_result = document["result"]
    _require(
        type(raw_result) is dict,
        f"Pulse corpus case {entry_id} returned an untyped result",
    )
    result_document = dict(raw_result)
    outcome = result_document.get("outcome")
    if entry_class == "fragment":
        _require(
            result_document == {"outcome": "fragment"},
            f"Pulse corpus fragment {entry_id} returned executable evidence",
        )
        result: Any = result_document
    elif outcome == "rows":
        _require(
            set(result_document) == {"outcome", "rows"},
            f"Pulse corpus case {entry_id} returned an open rows result",
        )
        ordering = str(entry["expected"].get("ordering") or "multiset")
        normalized, row_count = _normalize_query_result(
            result_document["rows"], ordering
        )
        result = {
            "outcome": "rows",
            "row_count": row_count,
            "rows": normalized,
        }
    elif outcome == "effect":
        _require(
            set(result_document) == {"effect", "outcome"},
            f"Pulse corpus case {entry_id} returned an open effect result",
        )
        result = {
            "effect": _json_value(result_document["effect"]),
            "outcome": "effect",
        }
    elif outcome == "error":
        _require(
            set(result_document) == {"error_code", "error_type", "outcome"}
            and type(result_document["error_code"]) is str
            and bool(result_document["error_code"])
            and type(result_document["error_type"]) is str
            and bool(result_document["error_type"]),
            f"Pulse corpus case {entry_id} returned an untyped error",
        )
        expected_error = entry["expected"].get("error")
        if entry["classification"] == "generic_gap":
            _require(
                type(expected_error) is dict
                and result_document["error_code"] == expected_error.get("code")
                and result_document["error_type"] == expected_error.get("type"),
                f"generic-gap Pulse corpus case {entry_id} returned the wrong typed error",
            )
        result = result_document
    else:
        raise GateFailure(
            f"Pulse corpus case {entry_id} returned an unknown typed outcome"
        )
    expected_kind = str(entry["expected"]["kind"])
    if entry["classification"] in {"already_supported", "duplicate_text"}:
        _require(
            outcome == expected_kind,
            f"supported Pulse corpus case {entry_id} returned {outcome}, "
            f"expected {expected_kind}",
        )
    elif entry["classification"] == "generic_gap":
        _require(
            outcome in {expected_kind, "error"},
            f"generic-gap Pulse corpus case {entry_id} returned an invalid outcome",
        )
    storage_identity, generation = _stable_storage_identity(identity)
    return IsolatedPulseCorpusResult(
        entry_class=entry_class,
        entry_id=entry_id,
        fingerprint_logical_graph_sha256=fingerprints["logical_graph_sha256"],
        fingerprint_trace_model_sha256=fingerprints["trace_model_sha256"],
        generation=generation,
        result_sha256=canonical_sha256(result),
        status=expected_status,
        storage_identity=storage_identity,
        worker_pid=os.getpid(),
        execution_authority_sha256=execution_authority_sha256,
    )


async def _pulse_corpus_worker_async(
    output_path: Path,
    factory: GateBackendFactory,
    context: GateBackendContext,
    entry: Mapping[str, Any],
    control: _IsolatedOperationControl | None = None,
) -> None:
    backend: GateBackend | None = None
    primary: BaseException | None = None
    try:
        execution_authority_sha256 = _worker_execution_authority_sha256(
            factory,
            context,
        )
        backend = await _open_backend(factory, context)
        _worker_execution_authority_sha256(factory, context)
        identity = await _backend_identity(backend, context)
        fingerprints = await _observed_fingerprints(backend, context)
        _release_isolated_operation(control)
        try:
            callback_value = await _maybe_await(backend.run_pulse_corpus_case(entry))
            result = _normalize_pulse_corpus_callback(
                callback_value,
                entry=entry,
                identity=identity,
                fingerprints=fingerprints,
                execution_authority_sha256=execution_authority_sha256,
            )
        finally:
            _finish_isolated_operation(control)
        after_identity = await _backend_identity(backend, context)
        _require_same_storage(
            identity,
            after_identity,
            context=f"Pulse corpus case {entry['id']}",
        )
        _require(
            await _observed_fingerprints(backend, context) == fingerprints,
            f"Pulse corpus case {entry['id']} mutated its fixed source view",
        )
        payload: dict[str, Any] = {"worker_status": "ok", **asdict(result)}
    except BaseException as failure:  # noqa: BLE001 - child evidence boundary
        primary = failure
        payload = {
            "worker_status": "failed",
            "error_type": type(failure).__name__,
            "error": str(failure),
            "traceback": traceback.format_exc(limit=20),
        }
    finally:
        if backend is not None:
            try:
                await _close_backend(backend)
            except BaseException as cleanup:  # noqa: BLE001 - child evidence
                if primary is None:
                    payload = {
                        "worker_status": "failed",
                        "error_type": type(cleanup).__name__,
                        "error": f"backend close failed: {cleanup}",
                        "traceback": traceback.format_exc(limit=20),
                    }
                else:
                    payload["close_error"] = f"{type(cleanup).__name__}: {cleanup}"
    output_path.write_bytes(canonical_json_bytes(payload))


def _pulse_corpus_worker_entry(
    output_path: str,
    factory: GateBackendFactory,
    context: GateBackendContext,
    entry: Mapping[str, Any],
    control: _IsolatedOperationControl | None = None,
) -> None:
    asyncio.run(
        _pulse_corpus_worker_async(Path(output_path), factory, context, entry, control)
    )


def run_isolated_pulse_corpus_case(
    factory: GateBackendFactory,
    context: GateBackendContext,
    entry: Mapping[str, Any],
    timeout_seconds: int,
) -> IsolatedPulseCorpusResult:
    """Run one Pulse corpus entry with a real 30s operation-only watchdog."""

    _require(timeout_seconds > 0, "the Pulse corpus watchdog must be positive")
    document = _run_isolated_operation_worker(
        case_label=f"Pulse corpus case {entry['id']}",
        output_prefix="mpulse7-pulse-query-",
        target=_pulse_corpus_worker_entry,
        target_args=(factory, context, dict(entry)),
        timeout_seconds=timeout_seconds,
    )
    if document.get("worker_status") != "ok":
        raise GateFailure(
            f"Pulse corpus case {entry['id']} failed in its worker: "
            f"{document.get('error_type')}: {document.get('error')}"
        )
    try:
        return IsolatedPulseCorpusResult(
            entry_class=str(document["entry_class"]),
            entry_id=str(document["entry_id"]),
            fingerprint_logical_graph_sha256=_sha256_text(
                document["fingerprint_logical_graph_sha256"],
                field="isolated Pulse logical_graph_sha256",
            ),
            fingerprint_trace_model_sha256=_sha256_text(
                document["fingerprint_trace_model_sha256"],
                field="isolated Pulse trace_model_sha256",
            ),
            generation=str(document["generation"]),
            result_sha256=_sha256_text(
                document["result_sha256"],
                field="isolated Pulse result_sha256",
            ),
            status=str(document["status"]),
            storage_identity=str(document["storage_identity"]),
            worker_pid=int(document["worker_pid"]),
            execution_authority_sha256=(
                None
                if document.get("execution_authority_sha256") is None
                else _sha256_text(
                    document["execution_authority_sha256"],
                    field="isolated Pulse execution authority SHA-256",
                )
            ),
        )
    except (KeyError, TypeError, ValueError) as failure:
        raise GateFailure(
            f"Pulse corpus case {entry['id']} returned an invalid worker receipt"
        ) from failure


async def _run_pulse_corpus(
    inputs: FrozenGateInputs,
    factories: Mapping[str, GateBackendFactory],
    contexts: Mapping[str, GateBackendContext],
    corpus_runner: IsolatedPulseCorpusRunner,
    backend_records: Mapping[str, Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    entries = cast(list[dict[str, Any]], inputs.pulse_corpus["entries"])
    timeout = int(inputs.manifest["pulse_query_corpus"]["external_timeout_seconds"])
    _require(timeout == 30, "the Pulse corpus watchdog must remain exactly 30 seconds")
    receipts: dict[str, dict[str, IsolatedPulseCorpusResult]] = {
        backend: {} for backend in BACKENDS
    }
    for backend in BACKENDS:
        expected = backend_records[backend]
        expected_storage, expected_generation = _stable_storage_identity(
            cast(Mapping[str, Any], expected["identity"])
        )
        for entry in entries:
            result = await _maybe_await(
                corpus_runner(factories[backend], contexts[backend], entry, timeout)
            )
            _require(
                isinstance(result, IsolatedPulseCorpusResult),
                f"{backend} Pulse corpus runner returned an invalid receipt",
            )
            _require(
                type(result.worker_pid) is int
                and result.worker_pid > 0
                and result.worker_pid != os.getpid(),
                f"{backend} Pulse corpus case {result.entry_id} was not externally isolated",
            )
            expected_execution_authority = contexts[
                backend
            ].certification_process_authority_sha256
            if expected_execution_authority is not None:
                _require(
                    result.execution_authority_sha256 == expected_execution_authority,
                    f"{backend} Pulse corpus case {result.entry_id} executed under "
                    "different source authority",
                )
            expected_status = (
                "not_executable" if entry["class"] == "fragment" else "executed"
            )
            _require(
                result.entry_id == entry["id"]
                and result.entry_class == entry["class"]
                and result.status == expected_status,
                f"{backend} Pulse corpus runner returned a mismatched case receipt",
            )
            _require(
                result.storage_identity == expected_storage
                and result.generation == expected_generation,
                f"{backend} Pulse corpus case {result.entry_id} opened the wrong generation",
            )
            _require(
                result.fingerprint_trace_model_sha256
                == expected["final_fingerprint_trace_model_sha256"]
                and result.fingerprint_logical_graph_sha256
                == expected["final_fingerprint_logical_graph_sha256"],
                f"{backend} Pulse corpus case {result.entry_id} opened the wrong fingerprint",
            )
            _sha256_text(
                result.result_sha256,
                field=f"{backend} Pulse corpus case {result.entry_id} result_sha256",
            )
            receipts[backend][result.entry_id] = result

    comparisons: list[dict[str, Any]] = []
    unexplained_divergences: list[dict[str, Any]] = []
    explained_divergences: list[dict[str, Any]] = []
    for entry in entries:
        entry_id = str(entry["id"])
        ladybug = receipts["ladybug"][entry_id]
        grafx = receipts["grafx"][entry_id]
        equal = (
            ladybug.status == grafx.status
            and ladybug.result_sha256 == grafx.result_sha256
        )
        comparison = {
            "class": entry["class"],
            "classification": entry["classification"],
            "entry_id": entry_id,
            "execution_authority_sha256": ladybug.execution_authority_sha256,
            "grafx_result_sha256": grafx.result_sha256,
            "ladybug_result_sha256": ladybug.result_sha256,
            "results_equal": equal,
            "status": ladybug.status,
        }
        comparisons.append(comparison)
        if not equal:
            if entry["classification"] == "generic_gap":
                explained_divergences.append(comparison)
            elif entry["class"] != "fragment":
                unexplained_divergences.append(comparison)
    return comparisons, unexplained_divergences, explained_divergences


def _trace_fingerprints_at(
    operations: Sequence[Mapping[str, Any]], boundaries: frozenset[int]
) -> dict[int, str]:
    model = DeterministicGraphModel()
    fingerprints: dict[int, str] = {}
    for operation in operations:
        model.apply(operation)
        sequence = int(operation["sequence"])
        if sequence in boundaries:
            fingerprints[sequence] = model.fingerprint_sha256()
    _require(
        set(fingerprints) == set(boundaries),
        "the crash-point fingerprint oracle omitted a frozen boundary",
    )
    return fingerprints


def _validated_crash_evidence(
    value: Any,
    *,
    point: Mapping[str, Any],
    expected_trace_fingerprint: str,
    expected_execution_authority_sha256: str | None = None,
) -> dict[str, Any]:
    point_id = str(point["id"])
    _require(
        type(value) is dict,
        f"crash point {point_id} returned no closed evidence object",
    )
    document = dict(value)
    expected_keys = {
        "after_operation",
        "absence_verified",
        "crash_exit_code",
        "crash_process_pid",
        "expected_recovery",
        "fingerprint_logical_graph_sha256",
        "fingerprint_observation_phase",
        "fingerprint_trace_model_sha256",
        "hook",
        "id",
        "observed_recovery",
        "recovered",
        "recovered_generation",
        "recovered_storage_identity",
        "recovery_process_pid",
        "verify_ok",
        "verify_scope",
    }
    if expected_execution_authority_sha256 is not None:
        expected_keys.update(
            {
                "crash_execution_authority_sha256",
                "recovery_execution_authority_sha256",
            }
        )
    _require(
        set(document) == expected_keys,
        f"crash point {point_id} returned an open evidence shape",
    )
    _require(
        document["id"] == point_id
        and document["hook"] == point["hook"]
        and document["after_operation"] == point["after_operation"]
        and document["expected_recovery"] == point["expected_recovery"],
        f"crash point {point_id} returned mismatched frozen coordinates",
    )
    _require(
        document["observed_recovery"] == point["expected_recovery"],
        f"crash point {point_id} did not observe its expected recovery",
    )
    privacy_point = point["hook"] in {
        "after_privacy_invalidation_fsync",
        "after_all_copy_sweeps",
    }
    expected_verify_scope = "aggregate_absence" if privacy_point else "all"
    _require(
        document["recovered"] is True
        and document["verify_ok"] is True
        and document["verify_scope"] == expected_verify_scope,
        f"crash point {point_id} did not prove its recovery verification",
    )
    expected_phase = "pre_invalidation" if privacy_point else "post_recovery"
    _require(
        document["fingerprint_observation_phase"] == expected_phase,
        f"crash point {point_id} reported a fingerprint from the wrong phase",
    )
    crash_pid = document["crash_process_pid"]
    recovery_pid = document["recovery_process_pid"]
    crash_exit_code = document["crash_exit_code"]
    _require(
        type(crash_pid) is int
        and crash_pid > 0
        and type(recovery_pid) is int
        and recovery_pid > 0
        and crash_pid != recovery_pid,
        f"crash point {point_id} did not isolate crash and recovery processes",
    )
    _require(
        type(crash_exit_code) is int and crash_exit_code != 0,
        f"crash point {point_id} did not observe a crashed process",
    )
    if privacy_point:
        _require(
            document["absence_verified"] is True
            and document["recovered_storage_identity"] == "absent"
            and document["recovered_generation"] == "absent",
            f"privacy crash point {point_id} did not prove aggregate absence",
        )
    else:
        _require(
            document["absence_verified"] is False
            and type(document["recovered_storage_identity"]) is str
            and bool(document["recovered_storage_identity"])
            and document["recovered_storage_identity"] != "absent"
            and type(document["recovered_generation"]) is str
            and bool(document["recovered_generation"])
            and document["recovered_generation"] != "absent",
            f"crash point {point_id} omitted recovered storage identity/generation",
        )
    trace_fingerprint = _sha256_text(
        document["fingerprint_trace_model_sha256"],
        field=f"crash point {point_id} trace_model_sha256",
    )
    logical_fingerprint = _sha256_text(
        document["fingerprint_logical_graph_sha256"],
        field=f"crash point {point_id} logical_graph_sha256",
    )
    _require(
        trace_fingerprint == expected_trace_fingerprint,
        f"crash point {point_id} recovered the wrong logical state",
    )
    result = {
        "after_operation": int(point["after_operation"]),
        "absence_verified": document["absence_verified"],
        "crash_exit_code": crash_exit_code,
        "crash_process_pid": crash_pid,
        "fingerprint_logical_graph_sha256": logical_fingerprint,
        "fingerprint_observation_phase": expected_phase,
        "fingerprint_trace_model_sha256": trace_fingerprint,
        "hook": point["hook"],
        "id": point_id,
        "observed_recovery": document["observed_recovery"],
        "recovered_generation": document["recovered_generation"],
        "recovered_storage_identity": document["recovered_storage_identity"],
        "recovery_process_pid": recovery_pid,
        "verify_ok": True,
        "verify_scope": expected_verify_scope,
    }
    if expected_execution_authority_sha256 is not None:
        crash_authority = _sha256_text(
            document["crash_execution_authority_sha256"],
            field=f"crash point {point_id} crash execution authority SHA-256",
        )
        recovery_authority = _sha256_text(
            document["recovery_execution_authority_sha256"],
            field=f"crash point {point_id} recovery execution authority SHA-256",
        )
        _require(
            crash_authority
            == recovery_authority
            == expected_execution_authority_sha256,
            f"crash point {point_id} executed under different source authority",
        )
        result.update(
            {
                "crash_execution_authority_sha256": crash_authority,
                "recovery_execution_authority_sha256": recovery_authority,
            }
        )
    return result


async def _run_crash_points(
    inputs: FrozenGateInputs,
    factory: GateBackendFactory,
    context: GateBackendContext,
    expected_backend_record: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Execute each cross-backend rollout crash point exactly once."""

    points = cast(list[dict[str, Any]], inputs.manifest["crash_points"]["points"])
    boundaries = frozenset(int(point["after_operation"]) for point in points)
    expected_fingerprints = _trace_fingerprints_at(inputs.operations, boundaries)
    backend = await _open_backend(factory, context)
    _worker_execution_authority_sha256(factory, context)
    primary: BaseException | None = None
    try:
        identity = await _backend_identity(backend, context)
        _require_same_storage(
            cast(Mapping[str, Any], expected_backend_record["identity"]),
            identity,
            context="crash harness supervisor",
        )
        fingerprints = await _observed_fingerprints(backend, context)
        _require(
            fingerprints["trace_model_sha256"]
            == expected_backend_record["final_fingerprint_trace_model_sha256"]
            and fingerprints["logical_graph_sha256"]
            == expected_backend_record["final_fingerprint_logical_graph_sha256"],
            "crash harness supervisor opened the wrong fixed trace state",
        )
        receipts: list[dict[str, Any]] = []
        for point in points:
            value = await _maybe_await(backend.run_crash_point(dict(point)))
            receipts.append(
                _validated_crash_evidence(
                    value,
                    point=point,
                    expected_trace_fingerprint=expected_fingerprints[
                        int(point["after_operation"])
                    ],
                    expected_execution_authority_sha256=(
                        context.certification_process_authority_sha256
                    ),
                )
            )
            after_identity = await _backend_identity(backend, context)
            _require_same_storage(
                identity,
                after_identity,
                context=f"crash point {point['id']} supervisor",
            )
            _require(
                await _observed_fingerprints(backend, context) == fingerprints,
                f"crash point {point['id']} mutated the fixed trace state",
            )
        _require(
            len(receipts) == len(points) == 11,
            "the crash harness did not execute all 11 frozen points",
        )
        return receipts
    except BaseException as failure:
        primary = failure
        raise
    finally:
        try:
            await _close_backend(backend)
        except BaseException as cleanup:
            if primary is None:
                raise GateFailure(
                    f"crash harness backend close failed: {cleanup}"
                ) from cleanup
            primary.add_note(f"crash harness backend close also failed: {cleanup}")


def _closed_callback_receipt(
    value: Any,
    *,
    expected_id: str,
    callback_name: str,
) -> dict[str, Any]:
    _require(type(value) is dict, f"{callback_name} {expected_id} returned no object")
    document = dict(value)
    _require(
        set(document) == {"id", "result", "status"},
        f"{callback_name} {expected_id} returned an open evidence shape",
    )
    _require(
        document["id"] == expected_id and document["status"] == "passed",
        f"{callback_name} {expected_id} did not return its closed success receipt",
    )
    try:
        digest = canonical_sha256(_json_value(document["result"]))
    except (TypeError, ValueError) as failure:
        raise GateFailure(
            f"{callback_name} {expected_id} returned non-canonical evidence"
        ) from failure
    return {"id": expected_id, "receipt_sha256": digest}


async def _run_closed_supplements(
    inputs: FrozenGateInputs,
    factory: GateBackendFactory,
    context: GateBackendContext,
    expected_backend_record: Mapping[str, Any],
) -> dict[str, Any]:
    backend = await _open_backend(factory, context)
    _worker_execution_authority_sha256(factory, context)
    primary: BaseException | None = None
    try:
        identity = await _backend_identity(backend, context)
        _require_same_storage(
            cast(Mapping[str, Any], expected_backend_record["identity"]),
            identity,
            context=f"{context.backend} supplement harness",
        )
        fingerprints = await _observed_fingerprints(backend, context)
        _require(
            fingerprints["trace_model_sha256"]
            == expected_backend_record["final_fingerprint_trace_model_sha256"]
            and fingerprints["logical_graph_sha256"]
            == expected_backend_record["final_fingerprint_logical_graph_sha256"],
            f"{context.backend} supplement harness opened the wrong fingerprint",
        )
        raw_receipts = []
        corpus_entries = {
            str(entry["id"]): entry
            for entry in cast(list[dict[str, Any]], inputs.pulse_corpus["entries"])
        }
        for family_id in inputs.manifest["raw_execute_supplement"]["family_ids"]:
            authenticated_entry = corpus_entries[str(family_id)]
            value = await _maybe_await(
                backend.run_raw_execute_family(dict(authenticated_entry))
            )
            raw_receipts.append(
                _closed_callback_receipt(
                    value,
                    expected_id=str(family_id),
                    callback_name="raw execute family",
                )
            )
        scenario_receipts = []
        for scenario in inputs.manifest["receipt_bound_scenarios"]:
            scenario_id = str(scenario["id"])
            value = await _maybe_await(backend.run_receipt_bound_scenario(scenario_id))
            scenario_receipts.append(
                _closed_callback_receipt(
                    value,
                    expected_id=scenario_id,
                    callback_name="receipt-bound scenario",
                )
            )
        after_identity = await _backend_identity(backend, context)
        _require_same_storage(
            identity,
            after_identity,
            context=f"{context.backend} supplement harness",
        )
        _require(
            await _observed_fingerprints(backend, context) == fingerprints,
            f"{context.backend} supplements mutated the fixed trace state",
        )
        return {
            "backend": context.backend,
            "raw_execute_families": raw_receipts,
            "receipt_bound_scenarios": scenario_receipts,
        }
    except BaseException as failure:
        primary = failure
        raise
    finally:
        try:
            await _close_backend(backend)
        except BaseException as cleanup:
            if primary is None:
                raise GateFailure(
                    f"{context.backend} supplement backend close failed: {cleanup}"
                ) from cleanup
            primary.add_note(
                f"{context.backend} supplement backend close also failed: {cleanup}"
            )


def _require_supplement_parity(supplements: Sequence[Mapping[str, Any]]) -> None:
    _require(len(supplements) == 2, "bilateral supplements require two receipts")
    by_backend = {str(value["backend"]): value for value in supplements}
    _require(set(by_backend) == set(BACKENDS), "a supplement backend is absent")
    _require(
        by_backend["ladybug"]["raw_execute_families"]
        == by_backend["grafx"]["raw_execute_families"],
        "raw execute supplement results differ between Ladybug and Grafx",
    )
    _require(
        by_backend["ladybug"]["receipt_bound_scenarios"]
        == by_backend["grafx"]["receipt_bound_scenarios"],
        "receipt-bound scenario results differ between Ladybug and Grafx",
    )


def _host_identity() -> tuple[str, dict[str, str]]:
    identity = {
        "machine": platform.machine(),
        "node": socket.gethostname(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
    }
    return canonical_sha256(identity), identity


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = canonical_json_bytes(receipt) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
        if os.name != "nt":
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


async def run_acceptance_gate(
    *,
    factories: Mapping[str, GateBackendFactory],
    workspace: Path,
    receipt_path: Path,
    manifest_path: Path = MANIFEST_PATH,
    pulse_corpus_path: Path | None = None,
    board_id: str = "m-pulse-7-acceptance",
    run_id: str | None = None,
    query_runner: IsolatedQueryRunner = run_isolated_board_query,
    pulse_corpus_runner: IsolatedPulseCorpusRunner = run_isolated_pulse_corpus_case,
    execution_mode: GateExecutionMode = "test_only",
) -> dict[str, Any]:
    """Execute the bilateral gate and atomically persist its receipt.

    Only ``certification`` mode may set ``acceptance.passed``.  That mode pins
    every executable authority and is used by the CLI.  Injectable factories
    and runners remain available solely for non-certifying tests.
    """

    _require(
        set(factories) == set(BACKENDS)
        and all(callable(factories[key]) for key in BACKENDS),
        "factories must contain exactly callable ladybug and grafx entries",
    )
    _require(
        execution_mode in {"test_only", "certification"},
        "execution_mode must be test_only or certification",
    )
    _require(
        type(board_id) is str and bool(board_id), "board_id must be non-empty text"
    )
    certification = execution_mode == "certification"
    inputs = verify_frozen_inputs(
        manifest_path,
        pulse_corpus_path=pulse_corpus_path,
        certification=certification,
    )
    certification_authority = (
        _certification_authority(
            factories,
            query_runner=query_runner,
            pulse_corpus_runner=pulse_corpus_runner,
            source_revisions=inputs.manifest["scope"]["source_revisions"],
        )
        if certification
        else None
    )
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    resolved_run_id = run_id or uuid.uuid4().hex
    _require(
        type(resolved_run_id) is str and bool(resolved_run_id),
        "run_id must be non-empty text",
    )
    contexts = {
        backend: GateBackendContext(
            backend=backend,
            board_id=board_id,
            workspace=str(workspace),
            run_id=resolved_run_id,
            certification_process_authority_sha256=(
                cast(dict[str, Any], certification_authority)[
                    "process_authority_sha256"
                ]
                if certification_authority is not None
                else None
            ),
        )
        for backend in BACKENDS
    }

    backend_records = []
    for backend in BACKENDS:
        backend_records.append(
            await _run_backend_trace(inputs, factories[backend], contexts[backend])
        )
    _require_backend_trace_parity(backend_records)
    backend_records_by_name = {
        str(record["backend"]): record for record in backend_records
    }

    crash_point_receipts = await _run_crash_points(
        inputs,
        factories["ladybug"],
        contexts["ladybug"],
        backend_records_by_name["ladybug"],
    )

    query_comparisons, divergences = await _run_queries(
        inputs,
        factories,
        contexts,
        query_runner,
        backend_records_by_name,
    )
    _require(
        len(divergences)
        <= inputs.manifest["acceptance"]["maximum_unexplained_divergences"],
        f"Board result supplement found {len(divergences)} divergence(s)",
    )

    (
        pulse_comparisons,
        pulse_unexplained_divergences,
        pulse_explained_divergences,
    ) = await _run_pulse_corpus(
        inputs,
        factories,
        contexts,
        pulse_corpus_runner,
        backend_records_by_name,
    )
    _require(
        len(pulse_unexplained_divergences)
        <= inputs.manifest["acceptance"]["maximum_unexplained_divergences"],
        "Pulse corpus found "
        f"{len(pulse_unexplained_divergences)} unexplained divergence(s)",
    )

    supplements = []
    for backend in BACKENDS:
        supplements.append(
            await _run_closed_supplements(
                inputs,
                factories[backend],
                contexts[backend],
                backend_records_by_name[backend],
            )
        )
    _require_supplement_parity(supplements)

    if certification:
        ending_inputs = verify_frozen_inputs(
            inputs.manifest_path,
            pulse_corpus_path=inputs.pulse_corpus_path,
            certification=True,
        )
        _require(
            (
                ending_inputs.manifest_file_sha256,
                ending_inputs.manifest_canonical_sha256,
                ending_inputs.pulse_corpus_file_sha256,
            )
            == (
                inputs.manifest_file_sha256,
                inputs.manifest_canonical_sha256,
                inputs.pulse_corpus_file_sha256,
            ),
            "certification inputs changed while the gate was running",
        )
        ending_authority = _certification_authority(
            factories,
            query_runner=query_runner,
            pulse_corpus_runner=pulse_corpus_runner,
            source_revisions=inputs.manifest["scope"]["source_revisions"],
        )
        _require(
            canonical_sha256(ending_authority)
            == canonical_sha256(certification_authority),
            "certification source authority changed while the gate was running",
        )

    host_fingerprint, host_identity = _host_identity()
    for record in backend_records:
        record.update(
            {
                "host_fingerprint": host_fingerprint,
                "pulse_query_corpus_digest": inputs.manifest["pulse_query_corpus"][
                    "digest"
                ],
                "trace_sha256": inputs.manifest["trace"]["expanded_trace_sha256"],
            }
        )
    receipt: dict[str, Any] = {
        "acceptance": {
            "crash_point_failures": 0,
            "passed": certification,
            "pulse_corpus_explained_divergences": len(pulse_explained_divergences),
            "pulse_corpus_unexplained_divergences": 0,
            "query_timeout_failures": 0,
            "test_only": not certification,
            "unexplained_divergences": 0,
            "verify_failures": 0,
        },
        "backends": backend_records,
        "board_id": board_id,
        "board_result_comparisons": query_comparisons,
        "board_result_supplement_sha256": inputs.manifest["board_result_supplement"][
            "queries_sha256"
        ],
        "crash_points": crash_point_receipts,
        "crash_points_sha256": inputs.manifest["crash_points"]["points_sha256"],
        "certification_authority": certification_authority,
        "execution_mode": execution_mode,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "host_fingerprint": host_fingerprint,
        "host_identity": host_identity,
        "manifest_canonical_sha256": inputs.manifest_canonical_sha256,
        "manifest_file_sha256": inputs.manifest_file_sha256,
        "operation_count": len(inputs.operations),
        "pulse_query_corpus_digest": inputs.manifest["pulse_query_corpus"]["digest"],
        "pulse_query_corpus_comparisons": pulse_comparisons,
        "pulse_query_corpus_explained_divergences": pulse_explained_divergences,
        "pulse_query_corpus_file_sha256": inputs.pulse_corpus_file_sha256,
        "receipt_format": RECEIPT_FORMAT,
        "reopen_recovery_cycle_count": 3,
        "run_id": resolved_run_id,
        "source_revisions": inputs.manifest["scope"]["source_revisions"],
        "supplements": supplements,
        "trace_sha256": inputs.manifest["trace"]["expanded_trace_sha256"],
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    _write_receipt(receipt_path, receipt)
    return receipt


def resolve_factory(reference: str) -> GateBackendFactory:
    """Resolve one explicit ``module:attribute`` factory without fallback."""

    module_name, separator, attribute_path = reference.partition(":")
    if not separator or not module_name or not attribute_path:
        raise GateFailure(
            f"invalid backend factory reference {reference!r}; use module:attribute"
        )
    try:
        value: Any = importlib.import_module(module_name)
        for component in attribute_path.split("."):
            value = getattr(value, component)
    except (ImportError, AttributeError) as failure:
        raise GateFailure(
            f"cannot resolve backend factory {reference!r}: {failure}"
        ) from failure
    if not callable(value):
        raise GateFailure(f"backend factory {reference!r} is not callable")
    return cast(GateBackendFactory, value)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--pulse-corpus", type=Path)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--board-id", default="m-pulse-7-acceptance")
    parser.add_argument("--run-id")
    parser.add_argument("--ladybug-factory", required=True)
    parser.add_argument("--grafx-factory", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    try:
        _require(
            arguments.ladybug_factory == CERTIFICATION_FACTORY_REFS["ladybug"]
            and arguments.grafx_factory == CERTIFICATION_FACTORY_REFS["grafx"],
            "the certification CLI requires the exact reviewed factory references",
        )
        factories = {
            "ladybug": resolve_factory(arguments.ladybug_factory),
            "grafx": resolve_factory(arguments.grafx_factory),
        }
        receipt = asyncio.run(
            run_acceptance_gate(
                factories=factories,
                workspace=arguments.workspace,
                receipt_path=arguments.receipt,
                manifest_path=arguments.manifest,
                pulse_corpus_path=arguments.pulse_corpus,
                board_id=arguments.board_id,
                run_id=arguments.run_id,
                execution_mode="certification",
            )
        )
        _require(
            receipt["acceptance"]["passed"] is True
            and receipt["acceptance"]["test_only"] is False,
            "the certification CLI received a non-certifying receipt",
        )
    except BaseException as failure:  # noqa: BLE001 - CLI fail-closed boundary
        print(
            f"M-PULSE-7 acceptance gate FAILED: {type(failure).__name__}: {failure}",
            file=sys.stderr,
        )
        return 2
    print(
        f"M-PULSE-7 acceptance gate PASSED receipt_sha256={receipt['receipt_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
