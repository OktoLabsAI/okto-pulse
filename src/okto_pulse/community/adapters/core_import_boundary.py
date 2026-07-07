"""Community -> core private-import boundary audit for AF21.

The Community edition may depend on public core ports/facades. Direct imports
from core internals are temporary reach-ins and must be either removed or
explicitly ledgered with ownership and withdrawal criteria.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


PUBLIC_CORE_IMPORT_ALLOWLIST: tuple[str, ...] = (
    "okto_pulse.core.ports",
    "okto_pulse.core.kg.interfaces",
    "okto_pulse.core.kg.board_source_store",
    "okto_pulse.core.kg.tier_power",
    "okto_pulse.core.kg.global_discovery.schema",
    "okto_pulse.core.kg.schema_contract",
    "okto_pulse.core.application",
    "okto_pulse.core.domain",
)


PRIVATE_CORE_IMPORT_PREFIXES: tuple[str, ...] = (
    "okto_pulse.core.infra.database",
    "okto_pulse.core.models.db",
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


COMMUNITY_CORE_REACH_IN_LEDGER: tuple[CoreReachInLedgerEntry, ...] = (
    _ledger(
        "src/okto_pulse/community/adapters/board_source_reader.py",
        "resolve_pulse_db_path",
        "okto_pulse.core.infra.database",
        ("get_engine",),
        target="Community relational source-reader adapter",
        reason="Local-first reader resolves the active SQLite database URL from the current core engine.",
        removal_path="Move SQLite path resolution behind an edition-owned relational runtime facade.",
        withdrawal_criterion="Board source readers no longer import core.infra.database.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/coordination.py",
        "<module>",
        "okto_pulse.core.models.db",
        (
            "ConsolidationQueue",
            "DomainEventHandlerExecution",
            "DomainEventRow",
            "GlobalUpdateOutbox",
        ),
        target="Community SQLAlchemy coordination repositories",
        reason="Local SQLite adapters still persist core-owned coordination rows through SQLAlchemy models.",
        removal_path="Replace direct ORM imports with repository DTOs or generated table mappings.",
        withdrawal_criterion="Community coordination adapters import no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/mcp_auth.py",
        "CommunityMcpAuthenticator.authenticate",
        "okto_pulse.core.services.main",
        ("AgentService",),
        target="Core auth/application service facade",
        reason="Community MCP authentication delegates board/API-key policy to the existing core service.",
        removal_path="Expose an auth/session application facade that hides services.main.",
        withdrawal_criterion="Community MCP auth imports no core.services.main symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/mcp_auth.py",
        "make_community_mcp_authenticator",
        "okto_pulse.core.infra.database",
        ("get_session_factory",),
        target="Community composition-owned session provider",
        reason="Local MCP authenticator factory defaults to the active SQLite session factory.",
        removal_path="Require the session factory to be injected by the composition root.",
        withdrawal_criterion="Community MCP auth imports no core.infra.database symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/mcp_auth.py",
        "MCPAuthContext.get_accessible_boards",
        "okto_pulse.core.services.main",
        ("AgentService",),
        target="Core auth/application service facade",
        reason="Community MCP request context delegates board ACL policy to the existing core service.",
        removal_path="Expose an auth/session application facade that hides services.main.",
        withdrawal_criterion="Community MCP auth imports no core.services.main symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/sqlalchemy_audit_repo.py",
        "CommunityAuditRepository.get_latest_for_artifact",
        "okto_pulse.core.models.db",
        ("ConsolidationAudit",),
        target="Community SQLAlchemy audit repository",
        reason="Local-first audit adapter persists current core audit rows.",
        removal_path="Move audit persistence behind an edition-neutral repository port.",
        withdrawal_criterion="Community audit repository imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/sqlalchemy_audit_repo.py",
        "CommunityAuditRepository.get_audit_by_session",
        "okto_pulse.core.models.db",
        ("ConsolidationAudit",),
        target="Community SQLAlchemy audit repository",
        reason="Local-first audit adapter persists current core audit rows.",
        removal_path="Move audit persistence behind an edition-neutral repository port.",
        withdrawal_criterion="Community audit repository imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/sqlalchemy_audit_repo.py",
        "CommunityAuditRepository.commit_consolidation_records",
        "okto_pulse.core.models.db",
        ("ConsolidationAudit", "GlobalUpdateOutbox", "KuzuNodeRef"),
        target="Community SQLAlchemy audit repository",
        reason="Local-first audit adapter persists current audit/outbox/node-ref rows.",
        removal_path="Move audit/outbox persistence behind edition-neutral repository ports.",
        withdrawal_criterion="Community audit repository imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/sqlalchemy_audit_repo.py",
        "CommunityAuditRepository.mark_audit_undone",
        "okto_pulse.core.models.db",
        ("ConsolidationAudit",),
        target="Community SQLAlchemy audit repository",
        reason="Local-first audit adapter updates current core audit rows.",
        removal_path="Move audit persistence behind an edition-neutral repository port.",
        withdrawal_criterion="Community audit repository imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/sqlalchemy_audit_repo.py",
        "CommunityAuditRepository.purge_by_board",
        "okto_pulse.core.models.db",
        ("ConsolidationAudit",),
        target="Community SQLAlchemy audit repository",
        reason="Local-first audit adapter deletes current core audit rows for board cleanup.",
        removal_path="Move audit persistence behind an edition-neutral repository port.",
        withdrawal_criterion="Community audit repository imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/sqlalchemy_repositories.py",
        "<module>",
        "okto_pulse.core.models.db",
        ("Board", "Ideation", "Spec"),
        target="Community SQLAlchemy UnitOfWork repositories",
        reason="Local repository adapters still adapt the current SQLAlchemy ORM entities.",
        removal_path="Keep ORM confined to the Community relational adapter layer or replace with DTO mappers.",
        withdrawal_criterion="Community repository adapters no longer import core.models.db at module top.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/sqlite_outbox_event_bus.py",
        "CommunityOutboxEventBus.publish",
        "okto_pulse.core.models.db",
        ("GlobalUpdateOutbox",),
        target="Community SQLite outbox event bus",
        reason="Local event bus persists outbox rows into the current SQLAlchemy schema.",
        removal_path="Move outbox persistence behind a storage-neutral event bus repository port.",
        withdrawal_criterion="Community outbox adapter imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/sqlalchemy_database.py",
        "configure_community_database",
        "okto_pulse.core.infra.database",
        ("configure_database_runtime",),
        target="Community SQLAlchemy runtime adapter",
        reason="Community owns engine/session construction and injects the runtime into the core facade.",
        removal_path="Expose runtime injection through a public core composition facade.",
        withdrawal_criterion="Community SQLAlchemy database adapter imports no core.infra.database symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/relational_schema_steps.py",
        "<module>",
        "okto_pulse.core.infra.database",
        ("Base", "get_engine", "get_session_factory"),
        target="Community relational schema lifecycle adapter",
        reason="Community owns concrete schema lifecycle execution against the current core ORM Base/runtime facade.",
        removal_path="Move Base/runtime access behind public relational lifecycle/runtime ports.",
        withdrawal_criterion="Community schema steps import no core.infra.database symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/relational_schema_steps.py",
        "_migrate_heal_task_validation_field_names",
        "okto_pulse.core.models.db",
        ("Card",),
        target="Community relational schema lifecycle adapter",
        reason="This migration heals persisted task-validation JSON through the current Card ORM model.",
        removal_path="Replace direct ORM access with a schema-step data access facade.",
        withdrawal_criterion="Community schema steps import no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/relational_schema_steps.py",
        "_migrate_agent_permissions",
        "okto_pulse.core.models.db",
        ("Agent",),
        target="Community relational schema lifecycle adapter",
        reason="This migration converts legacy agent permission rows through the current Agent ORM model.",
        removal_path="Replace direct ORM access with a schema-step data access facade.",
        withdrawal_criterion="Community schema steps import no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/data_bootstrap_steps.py",
        "<module>",
        "okto_pulse.core.infra.database",
        ("get_engine", "get_session_factory"),
        target="Community data-bootstrap lifecycle adapter",
        reason="Community owns concrete seed/reconcile SQL against the current relational runtime facade.",
        removal_path="Move runtime access behind public data-bootstrap/runtime ports.",
        withdrawal_criterion="Community data bootstrap steps import no core.infra.database symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/data_bootstrap_steps.py",
        "_seed_builtin_presets",
        "okto_pulse.core.models.db",
        ("PermissionPreset",),
        target="Community data-bootstrap lifecycle adapter",
        reason="Built-in preset seed rows are persisted through the current PermissionPreset ORM model.",
        removal_path="Replace direct ORM access with a data-bootstrap repository facade.",
        withdrawal_criterion="Community data bootstrap steps import no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/data_bootstrap_steps.py",
        "_reconcile_builtin_presets",
        "okto_pulse.core.models.db",
        ("PermissionPreset",),
        target="Community data-bootstrap lifecycle adapter",
        reason="Built-in preset reconcile updates current PermissionPreset ORM rows.",
        removal_path="Replace direct ORM access with a data-bootstrap repository facade.",
        withdrawal_criterion="Community data bootstrap steps import no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/data_bootstrap_steps.py",
        "_reconcile_agent_permission_flags",
        "okto_pulse.core.models.db",
        ("Agent",),
        target="Community data-bootstrap lifecycle adapter",
        reason="Agent permission flag reconcile updates current Agent ORM rows.",
        removal_path="Replace direct ORM access with a data-bootstrap repository facade.",
        withdrawal_criterion="Community data bootstrap steps import no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/adapters/relational_effects.py",
        "<module>",
        "okto_pulse.core.models.db",
        ("Board", "ConsolidationQueue", "KGTickRun"),
        target="Community SQLAlchemy relational effects adapter",
        reason="Community owns concrete SQLAlchemy persistence for queue/tick side effects requested by core ports.",
        removal_path="Replace direct ORM imports with edition-owned table mappings or repository DTOs.",
        withdrawal_criterion="Community relational effects adapter imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/cli.py",
        "cmd_init",
        "okto_pulse.core.infra.database",
        ("close_db", "get_session_factory", "init_db"),
        target="Community CLI database lifecycle adapter",
        reason="Local init command still drives the current SQLite-backed database lifecycle.",
        removal_path="Move CLI lifecycle calls behind a public startup/schema lifecycle facade.",
        withdrawal_criterion="Community CLI init imports no core.infra.database symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/cli.py",
        "cmd_init",
        "okto_pulse.core.models.db",
        ("Board",),
        target="Community CLI local seed/read adapter",
        reason="Local init command reads seeded board rows from the current SQLAlchemy schema.",
        removal_path="Replace direct ORM reads with public query/read-model facades.",
        withdrawal_criterion="Community CLI init imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/cli.py",
        "_generate_mcp_json",
        "okto_pulse.core.infra.database",
        ("close_db", "get_session_factory", "init_db"),
        target="Community CLI database lifecycle adapter",
        reason="Local MCP config generation opens the current SQLite database.",
        removal_path="Move CLI lifecycle calls behind a public startup/schema lifecycle facade.",
        withdrawal_criterion="Community MCP config generation imports no core.infra.database symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/cli.py",
        "_generate_mcp_json",
        "okto_pulse.core.models.db",
        ("Agent",),
        target="Community CLI local agent read adapter",
        reason="Local MCP config generation reads agent credentials from the current SQLAlchemy schema.",
        removal_path="Replace direct ORM reads with public query/read-model facades.",
        withdrawal_criterion="Community MCP config generation imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/cli.py",
        "cmd_verify_pipeline",
        "okto_pulse.core.infra.database",
        ("close_db", "get_session_factory", "init_db"),
        target="Community CLI database lifecycle adapter",
        reason="Local KG pipeline verification opens the current SQLite database.",
        removal_path="Move CLI lifecycle calls behind a public startup/schema lifecycle facade.",
        withdrawal_criterion="Community verify-pipeline imports no core.infra.database symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/cli.py",
        "cmd_kg_backfill",
        "okto_pulse.core.infra.database",
        ("close_db", "get_session_factory", "init_db"),
        target="Community CLI database lifecycle adapter",
        reason="Local KG backfill dry-run opens the current SQLite database.",
        removal_path="Move CLI lifecycle calls behind a public startup/schema lifecycle facade.",
        withdrawal_criterion="Community KG backfill imports no core.infra.database lifecycle symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/cli.py",
        "cmd_kg_backfill",
        "okto_pulse.core.models.db",
        ("Card", "Spec", "Sprint"),
        target="Community CLI local artifact read adapter",
        reason="Local KG dry-run loads current SQLAlchemy artifact rows for deterministic projection.",
        removal_path="Replace direct ORM reads with public query/read-model facades.",
        withdrawal_criterion="Community KG backfill dry-run imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/cli.py",
        "_apply_backfill",
        "okto_pulse.core.infra.database",
        ("close_db", "get_session_factory", "init_db"),
        target="Community CLI database lifecycle adapter",
        reason="Local KG backfill apply path opens the current SQLite database.",
        removal_path="Move CLI lifecycle calls behind a public startup/schema lifecycle facade.",
        withdrawal_criterion="Community KG backfill apply imports no core.infra.database lifecycle symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/cli.py",
        "_apply_backfill",
        "okto_pulse.core.models.db",
        ("ConsolidationQueue",),
        target="Community CLI local queue read adapter",
        reason="Local KG backfill apply path checks queue failures in the current SQLAlchemy schema.",
        removal_path="Replace direct ORM reads with a public queue status facade.",
        withdrawal_criterion="Community KG backfill apply imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/main.py",
        "<module>",
        "okto_pulse.core.infra.database",
        ("close_db", "get_session_factory", "init_db"),
        target="Community server database lifecycle adapter",
        reason="Local API lifespan drives the current SQLite-backed database lifecycle.",
        removal_path="Move server lifecycle calls behind a public startup/schema lifecycle facade.",
        withdrawal_criterion="Community main module imports no core.infra.database symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/main.py",
        "create_community_app.combined_lifespan",
        "okto_pulse.core.services.main",
        ("backfill_qa_answered_at",),
        target="Core startup/self-heal facade",
        reason="Community replaces the core lifespan and must run the same Q&A self-heal.",
        removal_path="Expose this self-heal through a public startup maintenance facade.",
        withdrawal_criterion="Community main lifespan imports no core.services.main symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/seed.py",
        "<module>",
        "okto_pulse.core.models.db",
        ("Agent", "AgentBoard", "Board"),
        target="Community local seed adapter",
        reason="Community first-boot seed creates rows in the current SQLAlchemy schema.",
        removal_path="Move first-boot seed writes behind a public seed/use-case facade.",
        withdrawal_criterion="Community seed module imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/seed.py",
        "_seed_demo_board",
        "okto_pulse.core.models.db",
        ("Card", "Spec"),
        target="Community demo seed adapter",
        reason="Community demo seed creates sample artifact rows in the current SQLAlchemy schema.",
        removal_path="Move demo seed writes behind a public seed/use-case facade.",
        withdrawal_criterion="Community demo seed imports no core.models.db symbols.",
    ),
    _ledger(
        "src/okto_pulse/community/seed.py",
        "_commit_demo_graph",
        "okto_pulse.core.infra.database",
        ("get_session_factory",),
        target="Community seed composition-owned session provider",
        reason="Community demo graph commit opens the current local SQLite session factory.",
        removal_path="Inject the session factory from the seed composition root.",
        withdrawal_criterion="Community seed imports no core.infra.database symbols.",
    ),
)


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.scope_stack: list[str] = []
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
            self._capture(node.lineno, alias.name, ("*",))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            if node.module.startswith("okto_pulse.core."):
                for alias in node.names:
                    bound_name = alias.asname or alias.name
                    self.core_aliases[bound_name] = f"{node.module}.{alias.name}"
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
        self._capture(node.lineno, module, ("*",), category="dynamic_import_module")

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


def _is_private_core_import(module: str, symbols: tuple[str, ...]) -> bool:
    return _classify_private_core_access(module, symbols) is not None


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


def _scan_private_imports(source_root: Path) -> tuple[CoreReachInOccurrence, ...]:
    package_root = _community_package_root(source_root)
    occurrences: list[CoreReachInOccurrence] = []
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
        occurrences.extend(visitor.occurrences)
    return tuple(occurrences)


def audit_community_core_import_boundary(
    source_root: Path,
    *,
    ledger: tuple[CoreReachInLedgerEntry, ...] = COMMUNITY_CORE_REACH_IN_LEDGER,
    public_allowlist: tuple[str, ...] = PUBLIC_CORE_IMPORT_ALLOWLIST,
) -> dict[str, object]:
    occurrences = _scan_private_imports(source_root)
    ledger_by_key = {entry.key: entry for entry in ledger}
    occurrence_by_key = {occurrence.key: occurrence for occurrence in occurrences}
    invalid_allowlist_entries = _invalid_public_allowlist_entries(public_allowlist)

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
    stale_ledger = [
        asdict(entry) for entry in ledger if entry.key not in occurrence_by_key
    ]
    ledgered = [
        {**asdict(ledger_by_key[occurrence.key]), "line": occurrence.line}
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
        and not invalid_allowlist_entries,
        "public_core_import_allowlist": public_allowlist,
        "private_prefixes": PRIVATE_CORE_IMPORT_PREFIXES,
        "private_ddl_symbol_imports": PRIVATE_CORE_DDL_SYMBOL_IMPORTS,
        "private_reexport_symbol_imports": PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS,
        "private_symbol_imports": PRIVATE_CORE_SYMBOL_IMPORTS,
        "private_service_reexport_module": PRIVATE_CORE_SERVICE_REEXPORT_MODULE,
        "occurrence_count": len(occurrences),
        "ledger_count": len(ledger),
        "ledgered": ledgered,
        "violations": violations,
        "stale_ledger": stale_ledger,
        "incomplete_ledger": incomplete_ledger,
        "invalid_allowlist_entries": invalid_allowlist_entries,
    }


__all__ = [
    "COMMUNITY_CORE_REACH_IN_LEDGER",
    "PRIVATE_CORE_IMPORT_PREFIXES",
    "PRIVATE_CORE_DDL_SYMBOL_IMPORTS",
    "PRIVATE_CORE_REEXPORT_SYMBOL_IMPORTS",
    "PRIVATE_CORE_SERVICE_REEXPORT_MODULE",
    "PRIVATE_CORE_SYMBOL_IMPORTS",
    "PUBLIC_CORE_IMPORT_ALLOWLIST",
    "CoreReachInLedgerEntry",
    "audit_community_core_import_boundary",
]
