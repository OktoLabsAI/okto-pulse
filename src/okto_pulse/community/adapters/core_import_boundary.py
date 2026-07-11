"""Community -> core private-import boundary audit for AF21.

The Community edition may depend on public core ports/facades. Direct imports
from core internals are temporary reach-ins and must be either removed or
explicitly ledgered with ownership and withdrawal criteria.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from okto_pulse.community.adapters.adapter_provenance import (
    PUBLIC_CORE_CONTRACT_SURFACES,
)

PUBLIC_CORE_IMPORT_ALLOWLIST: tuple[str, ...] = PUBLIC_CORE_CONTRACT_SURFACES


PRIVATE_CORE_IMPORT_PREFIXES: tuple[str, ...] = (
    "okto_pulse.core.infra.database",
    "okto_pulse.core.models.db",
    "okto_pulse.core.repositories.sqlalchemy",
    "okto_pulse.core.services.main",
    "okto_pulse.core.kg.interfaces.registry",
    "okto_pulse.core.kg.workers.deterministic_worker",
    "okto_pulse.core.kg.workers.consolidation",
    "okto_pulse.core.kg.governance",
    "okto_pulse.core.mcp.server",
)

PRIVATE_CORE_SYMBOL_IMPORTS: tuple[tuple[str, str], ...] = (
    ("okto_pulse.core.kg.interfaces", "get_kg_registry"),
)

PRIVATE_CORE_DDL_SYMBOL_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "okto_pulse.core.kg.global_discovery.schema",
        ("NODE_DDL", "REL_DDL", "VECTOR_INDEXES"),
    ),
)

PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "okto_pulse.core.infra",
        (
            "Base",
            "close_db",
            "configure_database_runtime",
            "create_database",
            "get_db",
            "get_db_session",
            "get_engine",
            "get_session_factory",
            "init_db",
            "is_database_runtime_configured",
            "reset_database_runtime_for_tests",
        ),
    ),
)

PRIVATE_CORE_SERVICE_REEXPORT_MODULE = "okto_pulse.core.services"

AF42_PRIVATE_REACH_IN_BASELINE = 0

CORE_IMPORT_PUBLIC_CONTRACT = "public_contract"
CORE_IMPORT_GOVERNED_REACH_IN = "governed_temporary_reach_in"
CORE_IMPORT_COMMUNITY_IMPLEMENTATION = "community_owned_implementation"
CORE_IMPORT_UNGOVERNED_REACH_IN = "ungoverned_private_reach_in"


@dataclass(frozen=True)
class CoreReachInLedgerEntry:
    file_path: str
    scope: str
    module: str
    symbols: tuple[str, ...]
    category: str
    owner: str
    reason: str
    target_public_surface: str
    removal_path: str
    withdrawal_criterion: str

    @property
    def key(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (self.file_path, self.scope, self.module, tuple(sorted(self.symbols)))


@dataclass(frozen=True)
class CoreReachInOccurrence:
    file_path: str
    scope: str
    module: str
    symbols: tuple[str, ...]
    line: int
    category: str

    @property
    def key(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (self.file_path, self.scope, self.module, tuple(sorted(self.symbols)))


@dataclass(frozen=True)
class CoreImportOccurrence:
    file_path: str
    scope: str
    module: str
    symbols: tuple[str, ...]
    line: int
    import_kind: str

    @property
    def key(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (self.file_path, self.scope, self.module, tuple(sorted(self.symbols)))


def _ledger(
    file_path: str,
    scope: str,
    module: str,
    symbols: Iterable[str],
    *,
    target: str,
    reason: str,
    removal_path: str,
    withdrawal_criterion: str,
    category: str = "private_core_import",
    owner: str = "okto-pulse-community/adapters",
) -> CoreReachInLedgerEntry:
    return CoreReachInLedgerEntry(
        file_path=file_path,
        scope=scope,
        module=module,
        symbols=tuple(sorted(symbols)),
        category=category,
        owner=owner,
        reason=reason,
        target_public_surface=target,
        removal_path=removal_path,
        withdrawal_criterion=withdrawal_criterion,
    )


COMMUNITY_CORE_REACH_IN_LEDGER: tuple[CoreReachInLedgerEntry, ...] = ()


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.scope_stack: list[str] = []
        self.core_imports: list[CoreImportOccurrence] = []
        self.occurrences: list[CoreReachInOccurrence] = []
        self.core_aliases: dict[str, str] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.startswith("okto_pulse.core.") and alias.asname:
                self.core_aliases[alias.asname] = alias.name
            self._capture_core_import(
                node.lineno,
                alias.name,
                ("*",),
                import_kind="import",
            )
            self._capture(node.lineno, alias.name, ("*",))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            if node.module.startswith("okto_pulse.core."):
                for alias in node.names:
                    bound_name = alias.asname or alias.name
                    self.core_aliases[bound_name] = f"{node.module}.{alias.name}"
            self._capture_core_import(
                node.lineno,
                node.module,
                tuple(sorted(alias.name for alias in node.names)),
                import_kind="import_from",
            )
            self._capture(
                node.lineno,
                node.module,
                tuple(sorted(alias.name for alias in node.names)),
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._capture_dynamic_private_access(node)
        self.generic_visit(node)

    def _capture_dynamic_private_access(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
            target, symbol_node = node.args[0], node.args[1]
            if isinstance(target, ast.Name) and target.id in self.core_aliases:
                if isinstance(symbol_node, ast.Constant) and isinstance(
                    symbol_node.value, str
                ):
                    symbol = symbol_node.value
                else:
                    symbol = f"<dynamic:{_safe_unparse(symbol_node)}>"
                self._capture(
                    node.lineno,
                    self.core_aliases[target.id],
                    (symbol,),
                    category="dynamic_getattr",
                )
            return

        import_module = (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        ) or (
            isinstance(func, ast.Name)
            and func.id == "import_module"
        )
        if not import_module or not node.args:
            return
        module_node = node.args[0]
        if not (
            isinstance(module_node, ast.Constant)
            and isinstance(module_node.value, str)
        ):
            return
        module = module_node.value
        self._capture_core_import(
            node.lineno,
            module,
            ("*",),
            import_kind="dynamic_import_module",
        )
        self._capture(node.lineno, module, ("*",), category="dynamic_import_module")

    def _capture_core_import(
        self,
        line: int,
        module: str,
        symbols: tuple[str, ...],
        *,
        import_kind: str,
    ) -> None:
        if not _is_core_import_reference(module, symbols):
            return
        self.core_imports.append(
            CoreImportOccurrence(
                file_path=self.file_path,
                scope=".".join(self.scope_stack) if self.scope_stack else "<module>",
                module=module,
                symbols=tuple(sorted(symbols)),
                line=line,
                import_kind=import_kind,
            )
        )

    def _capture(
        self,
        line: int,
        module: str,
        symbols: tuple[str, ...],
        *,
        category: str | None = None,
    ) -> None:
        resolved_category = category or _classify_private_core_access(module, symbols)
        if not resolved_category or not _is_private_core_import(module, symbols):
            return
        self.occurrences.append(
            CoreReachInOccurrence(
                file_path=self.file_path,
                scope=".".join(self.scope_stack) if self.scope_stack else "<module>",
                module=module,
                symbols=tuple(sorted(symbols)),
                line=line,
                category=resolved_category,
            )
        )


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive fallback for future AST nodes
        return node.__class__.__name__


def _classify_private_core_access(module: str, symbols: tuple[str, ...]) -> str | None:
    is_core_module = module == "okto_pulse.core" or module.startswith(
        "okto_pulse.core."
    )
    private_prefix = any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in PRIVATE_CORE_IMPORT_PREFIXES
    )
    private_core_module_by_name = is_core_module and any(
        _is_private_symbol_name(part) for part in module.split(".")
    )
    private_parent_submodule = any(
        _resolves_to_private_core_module(module, symbol)
        for symbol in symbols
        if symbol != "*"
    )
    private_symbol = any(
        module == symbol_module and (symbol in symbols or symbols == ("*",))
        for symbol_module, symbol in PRIVATE_CORE_SYMBOL_IMPORTS
    )
    private_symbol_by_name = is_core_module and any(
        _is_private_symbol_name(symbol) for symbol in symbols if symbol != "*"
    )
    private_ddl_symbol = any(
        module == ddl_module
        and (symbols == ("*",) or any(symbol in ddl_symbols for symbol in symbols))
        for ddl_module, ddl_symbols in PRIVATE_CORE_DDL_SYMBOL_IMPORTS
    )
    private_reexport_symbol = any(
        module == reexport_module
        and (symbols == ("*",) or any(symbol in reexported for symbol in symbols))
        for reexport_module, reexported in PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS
    )
    private_service_reexport = (
        module == PRIVATE_CORE_SERVICE_REEXPORT_MODULE
        and (symbols == ("*",) or any(symbol.endswith("Service") for symbol in symbols))
    )
    if private_ddl_symbol:
        return "leaked_ddl_symbol"
    if private_reexport_symbol:
        return "private_reexport_symbol"
    if private_service_reexport:
        return "private_service_reexport"
    if private_symbol or private_symbol_by_name:
        return "private_symbol_import"
    if private_prefix or private_core_module_by_name or private_parent_submodule:
        return "private_namespace_import"
    return None


def _is_core_import_reference(module: str, symbols: tuple[str, ...]) -> bool:
    if module == "okto_pulse.core" or module.startswith("okto_pulse.core."):
        return True
    return module == "okto_pulse" and "core" in symbols


def _is_private_core_import(module: str, symbols: tuple[str, ...]) -> bool:
    return _classify_private_core_access(module, symbols) is not None


def _expanded_core_references(
    module: str, symbols: tuple[str, ...]
) -> tuple[str, ...]:
    references = [module]
    if symbols != ("*",):
        references.extend(f"{module}.{symbol}" for symbol in symbols)
    return tuple(references)


def _is_public_core_contract(
    module: str,
    symbols: tuple[str, ...],
    public_allowlist: tuple[str, ...],
) -> bool:
    return any(
        reference == allowed or reference.startswith(allowed + ".")
        for reference in _expanded_core_references(module, symbols)
        for allowed in public_allowlist
    )


def _classify_core_import_for_inventory(
    occurrence: CoreImportOccurrence,
    *,
    ledger_by_key: dict[tuple[str, str, str, tuple[str, ...]], CoreReachInLedgerEntry],
    public_allowlist: tuple[str, ...],
) -> str:
    if _is_private_core_import(occurrence.module, occurrence.symbols):
        if occurrence.key in ledger_by_key:
            return CORE_IMPORT_GOVERNED_REACH_IN
        return CORE_IMPORT_UNGOVERNED_REACH_IN
    if _is_public_core_contract(
        occurrence.module, occurrence.symbols, public_allowlist
    ):
        return CORE_IMPORT_PUBLIC_CONTRACT
    return CORE_IMPORT_UNGOVERNED_REACH_IN


def _invalid_public_allowlist_entries(
    allowlist: tuple[str, ...],
) -> tuple[dict[str, str], ...]:
    invalid: list[dict[str, str]] = []
    for module in allowlist:
        broad_core = module in {
            "okto_pulse.core",
            "okto_pulse.core.kg",
            "okto_pulse.core.infra",
            "okto_pulse.core.services",
        }
        private_prefix = any(
            module == prefix or module.startswith(prefix + ".")
            for prefix in PRIVATE_CORE_IMPORT_PREFIXES
        )
        if broad_core or private_prefix:
            invalid.append(
                {
                    "module": module,
                    "category": "invalid_public_core_allowlist",
                    "reason": "broad_or_private_public_allowlist",
                    "remediation_hint": (
                        "Replace broad/private allowlist entries with explicit "
                        "public facades or ledgered temporary exceptions carrying "
                        "owner and withdrawal criteria."
                    ),
                }
            )
    return tuple(invalid)


def _is_private_symbol_name(symbol: str) -> bool:
    return symbol.startswith("_") and not symbol.startswith("__")


def _resolves_to_private_core_module(module: str, symbol: str) -> bool:
    fully_qualified = f"{module}.{symbol}"
    return any(
        fully_qualified == prefix or fully_qualified.startswith(prefix + ".")
        for prefix in PRIVATE_CORE_IMPORT_PREFIXES
    )


def _community_package_root(source_root: Path) -> Path:
    candidate = source_root / "src" / "okto_pulse" / "community"
    if candidate.exists():
        return candidate
    return source_root


def _scan_imports(
    source_root: Path,
) -> tuple[tuple[CoreImportOccurrence, ...], tuple[CoreReachInOccurrence, ...]]:
    package_root = _community_package_root(source_root)
    core_imports: list[CoreImportOccurrence] = []
    private_occurrences: list[CoreReachInOccurrence] = []
    for py_file in sorted(package_root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            rel = py_file.relative_to(source_root).as_posix()
            raise ValueError(f"Cannot parse {rel}: {exc}") from exc
        try:
            rel_path = py_file.relative_to(source_root).as_posix()
        except ValueError:
            rel_path = py_file.as_posix()
        visitor = _ImportVisitor(rel_path)
        visitor.visit(tree)
        core_imports.extend(visitor.core_imports)
        private_occurrences.extend(visitor.occurrences)
    return tuple(core_imports), tuple(private_occurrences)


def _scan_core_imports(source_root: Path) -> tuple[CoreImportOccurrence, ...]:
    return _scan_imports(source_root)[0]


def _scan_private_imports(source_root: Path) -> tuple[CoreReachInOccurrence, ...]:
    return _scan_imports(source_root)[1]


def _cluster_for_file_path(file_path: str) -> str:
    normalized = file_path.replace("\\", "/")
    if normalized.endswith("/cli.py"):
        return "cli_lifecycle"
    if normalized.endswith("/main.py"):
        return "server_lifecycle"
    if normalized.endswith("/seed.py"):
        return "seed_lifecycle"
    if normalized.endswith("/mcp_auth.py"):
        return "mcp_auth"
    if normalized.endswith("/data_bootstrap_steps.py"):
        return "bootstrap_steps"
    if normalized.endswith("/relational_schema_steps.py"):
        return "schema_steps"
    if normalized.endswith("/sqlalchemy_audit_repo.py") or normalized.endswith(
        "/sqlite_outbox_event_bus.py"
    ):
        return "audit_outbox_repository"
    if normalized.endswith("/coordination.py") or normalized.endswith(
        "/kg_operational.py"
    ):
        return "kg_operational_repository"
    if normalized.endswith("/sqlalchemy_repositories.py") or normalized.endswith(
        "/relational_effects.py"
    ):
        return "sqlalchemy_repository_adapter"
    return "community_composition"


def _build_core_import_inventory(
    core_imports: tuple[CoreImportOccurrence, ...],
    *,
    ledger_by_key: dict[tuple[str, str, str, tuple[str, ...]], CoreReachInLedgerEntry],
    public_allowlist: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    inventory: list[dict[str, object]] = []
    for occurrence in sorted(
        core_imports,
        key=lambda item: (
            item.file_path,
            item.line,
            item.module,
            item.symbols,
            item.import_kind,
        ),
    ):
        classification = _classify_core_import_for_inventory(
            occurrence,
            ledger_by_key=ledger_by_key,
            public_allowlist=public_allowlist,
        )
        entry: dict[str, object] = {
            **asdict(occurrence),
            "classification": classification,
            "cluster": _cluster_for_file_path(occurrence.file_path),
        }
        ledger_entry = ledger_by_key.get(occurrence.key)
        if ledger_entry is not None:
            entry.update(
                {
                    "owner": ledger_entry.owner,
                    "reason": ledger_entry.reason,
                    "target_public_surface": ledger_entry.target_public_surface,
                    "removal_path": ledger_entry.removal_path,
                    "withdrawal_criterion": ledger_entry.withdrawal_criterion,
                }
            )
        inventory.append(entry)
    return tuple(inventory)


def _private_reach_in_baseline_violations(
    *,
    occurrence_count: int,
    ledger_count: int,
    baseline: int | None,
) -> tuple[dict[str, object], ...]:
    if baseline is None or occurrence_count <= baseline:
        return ()
    return (
        {
            "category": "private_reach_in_baseline_growth",
            "reason": "private_reach_in_count_exceeds_af42_baseline",
            "baseline": baseline,
            "occurrence_count": occurrence_count,
            "ledger_count": ledger_count,
            "remediation_hint": (
                "Burn the reach-in down, or record an explicit decision with "
                "owner, target_public_surface, removal_path and withdrawal_criterion "
                "before raising the governed baseline."
            ),
        },
    )


def audit_community_core_import_boundary(
    source_root: Path,
    *,
    ledger: tuple[CoreReachInLedgerEntry, ...] = COMMUNITY_CORE_REACH_IN_LEDGER,
    public_allowlist: tuple[str, ...] = PUBLIC_CORE_IMPORT_ALLOWLIST,
    private_reach_in_baseline: int | None = AF42_PRIVATE_REACH_IN_BASELINE,
) -> dict[str, object]:
    core_imports, occurrences = _scan_imports(source_root)
    ledger_by_key = {entry.key: entry for entry in ledger}
    occurrence_by_key = {occurrence.key: occurrence for occurrence in occurrences}
    invalid_allowlist_entries = _invalid_public_allowlist_entries(public_allowlist)
    full_inventory = _build_core_import_inventory(
        core_imports,
        ledger_by_key=ledger_by_key,
        public_allowlist=public_allowlist,
    )
    inventory_by_classification = dict(
        sorted(Counter(entry["classification"] for entry in full_inventory).items())
    )
    baseline_violations = _private_reach_in_baseline_violations(
        occurrence_count=len(occurrences),
        ledger_count=len(ledger),
        baseline=private_reach_in_baseline,
    )

    violations = [
        {
            **asdict(occurrence),
            "reason": "missing_community_core_reach_in_ledger",
            "remediation_hint": (
                "Route through a public core facade/port, or add a ledger entry "
                "with owner, reason, target_public_surface, removal_path and "
                "withdrawal_criterion."
            ),
        }
        for occurrence in occurrences
        if occurrence.key not in ledger_by_key
    ]
    known_private_keys = {occurrence.key for occurrence in occurrences}
    violations.extend(
        {
            "file_path": entry["file_path"],
            "scope": entry["scope"],
            "module": entry["module"],
            "symbols": entry["symbols"],
            "line": entry["line"],
            "category": "undeclared_core_surface",
            "reason": "core_import_is_not_a_declared_public_contract",
            "remediation_hint": (
                "Depend on an explicit public Core contract or remove the Core "
                "implementation reach-in."
            ),
        }
        for entry in full_inventory
        if entry["classification"] == CORE_IMPORT_UNGOVERNED_REACH_IN
        and (
            entry["file_path"],
            entry["scope"],
            entry["module"],
            tuple(sorted(entry["symbols"])),
        )
        not in known_private_keys
    )
    stale_ledger = [
        asdict(entry) for entry in ledger if entry.key not in occurrence_by_key
    ]
    ledgered = [
        {
            **asdict(ledger_by_key[occurrence.key]),
            "line": occurrence.line,
            "cluster": _cluster_for_file_path(occurrence.file_path),
        }
        for occurrence in occurrences
        if occurrence.key in ledger_by_key
    ]
    incomplete_ledger = [
        asdict(entry)
        for entry in ledger
        if not all(
            (
                entry.category,
                entry.owner,
                entry.reason,
                entry.target_public_surface,
                entry.removal_path,
                entry.withdrawal_criterion,
            )
        )
    ]

    return {
        "ok": not violations
        and not stale_ledger
        and not incomplete_ledger
        and not invalid_allowlist_entries
        and not baseline_violations,
        "public_core_import_allowlist": public_allowlist,
        "private_prefixes": PRIVATE_CORE_IMPORT_PREFIXES,
        "private_ddl_symbol_imports": PRIVATE_CORE_DDL_SYMBOL_IMPORTS,
        "private_reexport_symbol_imports": PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS,
        "private_symbol_imports": PRIVATE_CORE_SYMBOL_IMPORTS,
        "private_service_reexport_module": PRIVATE_CORE_SERVICE_REEXPORT_MODULE,
        "private_reach_in_baseline": private_reach_in_baseline,
        "baseline_violations": baseline_violations,
        "inventory_count": len(full_inventory),
        "inventory_by_classification": inventory_by_classification,
        "full_inventory": full_inventory,
        "occurrence_count": len(occurrences),
        "ledger_count": len(ledger),
        "ledgered": ledgered,
        "violations": violations,
        "stale_ledger": stale_ledger,
        "incomplete_ledger": incomplete_ledger,
        "invalid_allowlist_entries": invalid_allowlist_entries,
    }


__all__ = [
    "AF42_PRIVATE_REACH_IN_BASELINE",
    "COMMUNITY_CORE_REACH_IN_LEDGER",
    "CORE_IMPORT_COMMUNITY_IMPLEMENTATION",
    "CORE_IMPORT_GOVERNED_REACH_IN",
    "CORE_IMPORT_PUBLIC_CONTRACT",
    "CORE_IMPORT_UNGOVERNED_REACH_IN",
    "PRIVATE_CORE_IMPORT_PREFIXES",
    "PRIVATE_CORE_DDL_SYMBOL_IMPORTS",
    "PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS",
    "PRIVATE_CORE_SERVICE_REEXPORT_MODULE",
    "PRIVATE_CORE_SYMBOL_IMPORTS",
    "PUBLIC_CORE_IMPORT_ALLOWLIST",
    "CoreImportOccurrence",
    "CoreReachInLedgerEntry",
    "audit_community_core_import_boundary",
]
