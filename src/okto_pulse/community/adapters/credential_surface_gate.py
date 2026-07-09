"""Community credential surface gate.

This gate protects the local-first bootstrap/export surfaces that may briefly
hold a plaintext MCP API key. The key may be exposed only as a reveal-once value
on first boot/init or as explicitly governed legacy plaintext during `.mcp.json`
export. Non-recoverable markers/hashes must not become recoverable secrets.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_SENSITIVE_NAMES = frozenset({"api_key", "secret", "token"})
_CREDENTIAL_LITERAL_MARKERS = (
    "api_key=",
    "API Key",
    "MCP URL",
)

_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "okto_pulse/community/seed.py",
        "seed_community_defaults",
        "return_secret",
    ): "first boot returns plaintext only to the caller that immediately reveals it",
    (
        "okto_pulse/community/cli.py",
        "cmd_init._init",
        "stdout_secret",
    ): "init prints the freshly seeded reveal-once key",
    (
        "okto_pulse/community/cli.py",
        "cmd_init._init",
        "credential_interpolation",
    ): "init formats the freshly seeded reveal-once key for stdout",
    (
        "okto_pulse/community/cli.py",
        "_exportable_credential_from_legacy_agent",
        "persisted_agent_api_key_read",
    ): "single governed legacy plaintext classifier before export",
    (
        "okto_pulse/community/cli.py",
        "_generate_mcp_json",
        "credential_interpolation",
    ): ".mcp.json export receives only classified exportable credentials",
    (
        "okto_pulse/community/main.py",
        "create_community_app.combined_lifespan",
        "stdout_secret",
    ): "serve first boot prints the freshly seeded reveal-once key",
    (
        "okto_pulse/community/main.py",
        "create_community_app.combined_lifespan",
        "credential_interpolation",
    ): "serve first boot prints the freshly seeded reveal-once MCP URL",
}


@dataclass(frozen=True)
class CredentialSurfaceFinding:
    file: str
    line: int
    function: str
    kind: str
    symbol: str
    allowlisted: bool
    reason: str | None = None


@dataclass(frozen=True)
class CredentialSurfaceReport:
    findings: tuple[CredentialSurfaceFinding, ...]
    violations: tuple[CredentialSurfaceFinding, ...]
    allowlist: tuple[tuple[str, str, str, str], ...]
    scanned_root: str

    @property
    def ok(self) -> bool:
        return not self.violations


def _default_source_root() -> Path:
    # src/okto_pulse/community/adapters/credential_surface_gate.py -> src/
    return Path(__file__).resolve().parents[3]


def _function_stack(parent_map: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(current.name)
        current = parent_map.get(current)
    return ".".join(reversed(names)) or "<module>"


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_agent_model_column(node: ast.Attribute) -> bool:
    return isinstance(node.value, ast.Name) and node.value.id == "Agent"


def _sensitive_refs(node: ast.AST) -> set[str]:
    refs: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in _SENSITIVE_NAMES:
            refs.add(child.id)
        elif isinstance(child, ast.Attribute) and child.attr in _SENSITIVE_NAMES:
            refs.add(child.attr)
    return refs


def _formatted_refs(node: ast.JoinedStr) -> set[str]:
    refs: set[str] = set()
    for value in node.values:
        if isinstance(value, ast.FormattedValue):
            refs.update(_sensitive_refs(value.value))
            name = _name_of(value.value)
            if name is not None:
                refs.add(name)
    return refs


def _joined_str_contains_credential_literal(node: ast.JoinedStr) -> bool:
    text_parts = [
        value.value
        for value in node.values
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    ]
    joined = "".join(text_parts)
    return any(marker in joined for marker in _CREDENTIAL_LITERAL_MARKERS)


def _print_call(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Name) and func.id == "print"


def _api_key_field_call(node: ast.Call) -> bool:
    func = node.func
    if not (isinstance(func, ast.Name) and func.id == "_field"):
        return False
    if len(node.args) < 2:
        return False
    key = node.args[1]
    return isinstance(key, ast.Constant) and key.value == "api_key"


def _allowlist_reason(file: str, function: str, kind: str) -> str | None:
    exact = _ALLOWLIST.get((file, function, kind))
    if exact is not None:
        return exact
    # Nested callbacks may be emitted from an inner helper within an allowlisted
    # public function. Keep matching explicit: only a prefix ending with ".".
    for (allowed_file, allowed_function, allowed_kind), reason in _ALLOWLIST.items():
        if file == allowed_file and kind == allowed_kind and function.startswith(
            f"{allowed_function}."
        ):
            return reason
    return None


def _finding(
    *,
    rel: str,
    parent_map: dict[ast.AST, ast.AST],
    node: ast.AST,
    kind: str,
    symbol: str,
) -> CredentialSurfaceFinding:
    function = _function_stack(parent_map, node)
    reason = _allowlist_reason(rel, function, kind)
    return CredentialSurfaceFinding(
        file=rel,
        line=getattr(node, "lineno", 0),
        function=function,
        kind=kind,
        symbol=symbol,
        allowlisted=reason is not None,
        reason=reason,
    )


def _iter_python_files(root: Path) -> Iterable[Path]:
    community = root / "okto_pulse" / "community"
    for py in sorted(community.rglob("*.py")):
        parts = set(py.parts)
        if "__pycache__" in parts or "frontend_dist" in parts:
            continue
        yield py


def run_community_credential_surface_gate(
    source_root: Path | None = None,
) -> CredentialSurfaceReport:
    """Scan Community Python sources for plaintext credential exposure.

    ``source_root`` is the directory that contains ``okto_pulse/``. The default
    scans the installed/local package source tree.
    """
    root = source_root or _default_source_root()
    root = Path(root)
    findings: list[CredentialSurfaceFinding] = []

    for py in _iter_python_files(root):
        rel = py.relative_to(root).as_posix()
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        parent_map = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "api_key":
                if not _is_agent_model_column(node):
                    findings.append(
                        _finding(
                            rel=rel,
                            parent_map=parent_map,
                            node=node,
                            kind="persisted_agent_api_key_read",
                            symbol="api_key",
                        )
                    )
            elif isinstance(node, ast.Call) and _api_key_field_call(node):
                findings.append(
                    _finding(
                        rel=rel,
                        parent_map=parent_map,
                        node=node,
                        kind="persisted_agent_api_key_read",
                        symbol="api_key",
                    )
                )
            elif isinstance(node, ast.Call) and _print_call(node):
                refs: set[str] = set()
                for arg in node.args:
                    refs.update(_sensitive_refs(arg))
                    if isinstance(arg, ast.JoinedStr) and _joined_str_contains_credential_literal(arg):
                        refs.update(_formatted_refs(arg))
                for kw in node.keywords:
                    if kw.value is not None:
                        refs.update(_sensitive_refs(kw.value))
                for symbol in sorted(refs):
                    findings.append(
                        _finding(
                            rel=rel,
                            parent_map=parent_map,
                            node=node,
                            kind="stdout_secret",
                            symbol=symbol,
                        )
                    )
            elif isinstance(node, ast.JoinedStr) and _joined_str_contains_credential_literal(node):
                refs = _formatted_refs(node)
                if refs:
                    for symbol in sorted(refs):
                        findings.append(
                            _finding(
                                rel=rel,
                                parent_map=parent_map,
                                node=node,
                                kind="credential_interpolation",
                                symbol=symbol,
                            )
                        )
            elif isinstance(node, ast.Return):
                refs = _sensitive_refs(node.value) if node.value is not None else set()
                refs = {ref for ref in refs if ref == "api_key"}
                for symbol in sorted(refs):
                    findings.append(
                        _finding(
                            rel=rel,
                            parent_map=parent_map,
                            node=node,
                            kind="return_secret",
                            symbol=symbol,
                        )
                    )

    violations = tuple(f for f in findings if not f.allowlisted)
    allowlist = tuple(
        (file, function, kind, reason)
        for (file, function, kind), reason in sorted(_ALLOWLIST.items())
    )
    scanned = (root / "okto_pulse" / "community").relative_to(root).as_posix()
    return CredentialSurfaceReport(
        findings=tuple(findings),
        violations=violations,
        allowlist=allowlist,
        scanned_root=scanned,
    )


__all__ = [
    "CredentialSurfaceFinding",
    "CredentialSurfaceReport",
    "run_community_credential_surface_gate",
]
