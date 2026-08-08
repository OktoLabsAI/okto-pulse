"""Community-owned concrete relational data bootstrap steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import copy as _copy
import datetime as _datetime
import hashlib as _hashlib
import json as _json
import uuid as _uuid

from okto_pulse.core.discovery_intent_catalog import DEFAULT_DISCOVERY_INTENTS
from okto_pulse.core.ports.permission_policy import (
    PermissionPresetLineageNode,
    get_permission_flag,
    merge_permission_registry_defaults,
    normalize_agent_permission_layer,
    permission_introduction_manifests,
    resolve_effective_permissions,
    resolve_preset_lineage,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.permission_preset_reconciliation import (
    reconcile_community_permission_presets,
)

StepCallable = Callable[[], "Awaitable[object] | object"]


def _json_value(value):
    if isinstance(value, str):
        return _json.loads(value)
    return value


def _permission_audit_digest(value) -> str:
    return _hashlib.sha256(
        _json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_permission_flags_shape(
    document: dict,
    canonical: Mapping,
    *,
    agent_id: object,
    path: tuple[str, ...] = (),
) -> None:
    """Validate only canonical keys, leaving extension keys untouched."""
    for key, canonical_value in canonical.items():
        if key not in document:
            continue
        value = document[key]
        key_path = (*path, str(key))
        dotted_path = ".".join(key_path)
        if isinstance(canonical_value, Mapping):
            if not isinstance(value, Mapping):
                raise ValueError(
                    f"Agent {agent_id!r} permission_flags canonical branch "
                    f"{dotted_path!r} must be a JSON object, "
                    f"got {type(value).__name__}"
                )
            normalized_branch = _copy.deepcopy(dict(value))
            document[key] = normalized_branch
            _validate_permission_flags_shape(
                normalized_branch,
                canonical_value,
                agent_id=agent_id,
                path=key_path,
            )
        elif not isinstance(value, bool):
            raise ValueError(
                f"Agent {agent_id!r} permission_flags canonical leaf "
                f"{dotted_path!r} must be boolean, got {type(value).__name__}"
            )


def _permission_flags_document(
    value,
    *,
    agent_id: object,
    canonical: Mapping,
) -> dict | None:
    try:
        decoded = _json_value(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Agent {agent_id!r} permission_flags is not valid JSON"
        ) from exc
    # ``AgentUpdate(permission_flags=None)`` is a supported way to clear the
    # direct layer and fall back to a preset/legacy/default policy. SQLite's
    # historical JSON binding persisted that as the JSON literal ``null``.
    if decoded is None:
        return None
    if not isinstance(decoded, Mapping):
        raise ValueError(
            f"Agent {agent_id!r} permission_flags must be a JSON object, "
            f"got {type(decoded).__name__}"
        )
    document = _copy.deepcopy(dict(decoded))
    _validate_permission_flags_shape(
        document,
        canonical,
        agent_id=agent_id,
    )
    return document


async def _seed_builtin_presets() -> None:
    """Compatibility entrypoint for the unified Core reconciliation use case."""

    await reconcile_community_permission_presets()


async def _reconcile_builtin_presets() -> None:
    """Compatibility entrypoint for the unified Core reconciliation use case."""

    await reconcile_community_permission_presets()


async def _reconcile_agent_permission_flags() -> None:
    """Normalize historical agent snapshots into sparse direct overrides.

    Missing permission leaves are deliberately not materialized.  Historical
    Full Control snapshots become the trusted ``None`` sentinel, while agents
    linked to a preset retain only their direct delta.  This lets reconciled
    preset grants from every ordered permission-introduction manifest
    propagate without erasing explicit sparse overrides.
    """
    import logging

    logger = logging.getLogger("okto_pulse.migrations")

    async with get_session_factory()() as session:
        try:
            from sqlalchemy import JSON as sa_JSON
            from sqlalchemy import DateTime as sa_DateTime
            from sqlalchemy import bindparam, text as sa_text

            canonical, _ = merge_permission_registry_defaults({})
            result = await session.execute(
                sa_text(
                    "SELECT id, permission_flags, preset_id FROM agents "
                    "WHERE permission_flags IS NOT NULL"
                )
            )
            agents = list(result.mappings().all())
            preset_nodes: tuple[PermissionPresetLineageNode, ...] = ()
            if any(agent.get("preset_id") for agent in agents):
                preset_result = await session.execute(
                    sa_text(
                        "SELECT id, base_preset_id, flags "
                        "FROM permission_presets ORDER BY id"
                    )
                )
                preset_nodes = tuple(
                    PermissionPresetLineageNode(
                        id=row["id"],
                        base_preset_id=row["base_preset_id"],
                        flags=_copy.deepcopy(_json_value(row["flags"])),
                    )
                    for row in preset_result.mappings().all()
                )
            manifests = permission_introduction_manifests()
            # SQLite's CURRENT_TIMESTAMP has only second precision.  Two
            # reconciliation passes in the same second would otherwise have
            # an indeterminate audit order once UUIDs were used as the
            # tie-breaker, making the latest run appear to contain mutations.
            run_started_at = _datetime.datetime.now(_datetime.UTC)
            updated = 0
            review_required_count = 0
            audit_digests: list[tuple[str, str]] = []
            for agent in agents:
                stored_dict = _permission_flags_document(
                    agent["permission_flags"],
                    agent_id=agent["id"],
                    canonical=canonical,
                )
                if stored_dict is None:
                    continue
                preset_flags = None
                preset_id = agent.get("preset_id")
                owner_review_required = False
                review_reason = None
                classification = "preset_lineage" if preset_id else "direct"
                if preset_id:
                    lineage = resolve_preset_lineage(preset_id, preset_nodes)
                    # Invalid lineage remains untouched. Runtime resolution is
                    # all-False/review-required; rewriting direct data cannot
                    # repair an ownership decision.
                    if lineage.owner_review_required:
                        owner_review_required = True
                        review_reason = lineage.review_reason
                        classification = "invalid_preset_lineage"
                    preset_flags = lineage.flags
                candidate = normalize_agent_permission_layer(stored_dict, preset_flags)
                if preset_id is None and candidate is not None:
                    # A sparse/direct document has no durable lineage or
                    # recognized Full Control fingerprint. Preserve it for
                    # owner inspection, but keep runtime authority all-denied.
                    normalized = candidate
                    owner_review_required = True
                    review_reason = "unrecognized_direct_permissions"
                    classification = "direct_unrecognized"
                elif owner_review_required:
                    normalized = stored_dict
                else:
                    normalized = candidate
                    if preset_id is None:
                        classification = "full_control_snapshot"
                if normalized != stored_dict:
                    await session.execute(
                        sa_text(
                            "UPDATE agents SET permission_flags = :permission_flags "
                            "WHERE id = :id"
                        ).bindparams(bindparam("permission_flags", type_=sa_JSON)),
                        {
                            "id": agent["id"],
                            "permission_flags": normalized,
                        },
                    )
                    updated += 1
                effective = resolve_effective_permissions(
                    normalized,
                    preset_flags,
                    None,
                    owner_review_required=owner_review_required,
                    review_reason=review_reason,
                )
                before_digest = _permission_audit_digest(stored_dict)
                after_digest = _permission_audit_digest(normalized)
                audit_digests.append((before_digest, after_digest))
                if owner_review_required:
                    review_required_count += 1
                for manifest_order, manifest in enumerate(manifests):
                    true_count = sum(
                        get_permission_flag(effective.flags, leaf) is True
                        for leaf in manifest.leaves
                    )
                    await session.execute(
                        sa_text(
                            "INSERT INTO permission_introduction_audit "
                            "(id, manifest_version, phase, classification, "
                            "subject_id, base_preset_id, before_digest, "
                            "after_digest, introduced_true_count, "
                            "introduced_false_count, owner_review_required, "
                            "mutation_count, details, created_at) VALUES "
                            "(:id, :manifest_version, :phase, :classification, "
                            ":subject_id, :base_preset_id, :before_digest, "
                            ":after_digest, :true_count, :false_count, "
                            ":owner_review_required, :mutation_count, :details, "
                            ":created_at)"
                        ).bindparams(
                            bindparam("details", type_=sa_JSON),
                            bindparam(
                                "created_at",
                                type_=sa_DateTime(timezone=True),
                            ),
                        ),
                        {
                            "id": str(_uuid.uuid4()),
                            "manifest_version": manifest.version,
                            "phase": "agent_reconciliation",
                            "classification": classification,
                            "subject_id": agent["id"],
                            "base_preset_id": preset_id,
                            "before_digest": before_digest,
                            "after_digest": after_digest,
                            "true_count": true_count,
                            "false_count": len(manifest.leaves) - true_count,
                            "owner_review_required": owner_review_required,
                            "mutation_count": int(normalized != stored_dict),
                            "details": {
                                "review_reason": review_reason,
                                "manifest_order": manifest_order,
                            },
                            "created_at": run_started_at,
                        },
                    )
            summary_before = _permission_audit_digest(
                [before for before, _after in audit_digests]
            )
            summary_after = _permission_audit_digest(
                [after for _before, after in audit_digests]
            )
            for manifest_order, manifest in enumerate(manifests):
                await session.execute(
                    sa_text(
                        "INSERT INTO permission_introduction_audit "
                        "(id, manifest_version, phase, classification, "
                        "subject_id, base_preset_id, before_digest, "
                        "after_digest, introduced_true_count, "
                        "introduced_false_count, owner_review_required, "
                        "mutation_count, details, created_at) VALUES "
                        "(:id, :manifest_version, :phase, :classification, "
                        "NULL, NULL, :before_digest, :after_digest, 0, 0, "
                        ":owner_review_required, :mutation_count, :details, "
                        ":created_at)"
                    ).bindparams(
                        bindparam("details", type_=sa_JSON),
                        bindparam(
                            "created_at",
                            type_=sa_DateTime(timezone=True),
                        ),
                    ),
                    {
                        "id": str(_uuid.uuid4()),
                        "manifest_version": manifest.version,
                        "phase": "agent_reconciliation",
                        "classification": "run_summary",
                        "before_digest": summary_before,
                        "after_digest": summary_after,
                        "owner_review_required": bool(review_required_count),
                        "mutation_count": updated,
                        "details": {
                            "agents_considered": len(audit_digests),
                            "owner_review_required_count": (review_required_count),
                            "manifest_order": manifest_order,
                        },
                        "created_at": run_started_at,
                    },
                )
            await session.commit()
            if updated:
                logger.info(
                    f"Normalized {updated} agent permission layer(s) "
                    "to sparse direct overrides"
                )
        except Exception as e:
            logger.error(f"Agent permissions reconcile failed: {e}")
            await session.rollback()
            raise


async def _bootstrap_default_discovery_intents() -> None:
    """Upsert the core-owned Discovery intent catalog."""
    from sqlalchemy import text as sa_text

    async with get_engine().begin() as conn:
        import json as _json

        for s in DEFAULT_DISCOVERY_INTENTS:
            params_literal = (
                _json.dumps(s["params_schema"])
                if s["params_schema"] is not None
                else None
            )

            row = await conn.execute(
                sa_text(
                    "SELECT id, tool_binding, params_schema FROM discovery_intents "
                    "WHERE name = :name"
                ),
                {"name": s["name"]},
            )
            existing = row.first()
            if existing is not None:
                existing_binding = existing[1]
                existing_params = existing[2]
                if isinstance(existing_params, str):
                    try:
                        existing_params = _json.loads(existing_params)
                    except Exception:
                        existing_params = None
                needs_update = (
                    existing_binding != s["tool_binding"]
                    or existing_params != s["params_schema"]
                )
                if needs_update:
                    await conn.execute(
                        sa_text(
                            "UPDATE discovery_intents "
                            "SET tool_binding = :tool_binding, "
                            "    params_schema = :params_schema, "
                            "    updated_at = CURRENT_TIMESTAMP "
                            "WHERE name = :name AND is_seed = :is_seed"
                        ),
                        {
                            "name": s["name"],
                            "tool_binding": s["tool_binding"],
                            "params_schema": params_literal,
                            "is_seed": True,
                        },
                    )
                continue

            import uuid as _uuid

            await conn.execute(
                sa_text(
                    "INSERT INTO discovery_intents "
                    "(id, name, label, description, category, tool_binding, "
                    " params_schema, renderer, min_permission, active, is_seed) "
                    "VALUES "
                    "(:id, :name, :label, :description, :category, :tool_binding, "
                    " :params_schema, :renderer, :min_permission, :active, :is_seed)"
                ),
                {
                    "id": str(_uuid.uuid4()),
                    "name": s["name"],
                    "label": s["label"],
                    "description": s["description"],
                    "category": s["category"],
                    "tool_binding": s["tool_binding"],
                    "params_schema": params_literal,
                    "renderer": s["renderer"],
                    "min_permission": s["min_permission"],
                    "active": True,
                    "is_seed": True,
                },
            )


async def _backfill_knowledge_propagation_v2() -> str | None:
    """Resume conservative legacy classification after schema convergence."""

    from okto_pulse.community.adapters.knowledge_propagation_backfill import (
        backfill_knowledge_propagation_v2,
    )

    result = await backfill_knowledge_propagation_v2()
    return None if result.applied_targets else "skipped"


async def _bootstrap_quality_assessment_legacy_import_v1() -> str | None:
    """Resume and close the durable C7 import epoch for every board."""

    from sqlalchemy import select

    from okto_pulse.community.adapters.sqlalchemy_models import Board
    from okto_pulse.community.adapters.sqlalchemy_quality_assessment_legacy_import import (
        CommunitySqlAlchemyQualityAssessmentLegacyImport,
    )
    from okto_pulse.core.domain.quality_assessment_legacy_import import (
        LegacyImportRunRequest,
    )
    from okto_pulse.core.services.quality_assessment_legacy_import import (
        QualityAssessmentLegacyImportService,
    )

    session_factory = get_session_factory()
    async with session_factory() as session:
        board_ids = tuple(
            str(value)
            for value in (
                await session.execute(select(Board.id).order_by(Board.id))
            ).scalars()
        )
    if not board_ids:
        return "skipped"

    persistence = CommunitySqlAlchemyQualityAssessmentLegacyImport(session_factory)
    service = QualityAssessmentLegacyImportService()
    for board_id in board_ids:
        await service.run(
            LegacyImportRunRequest(board_id=board_id),
            source_port=persistence,
            persistence=persistence,
        )
    return None


DATA_BOOTSTRAP_STEP_CALLABLES: dict[str, StepCallable] = {
    "_seed_builtin_presets": _seed_builtin_presets,
    "_reconcile_builtin_presets": _reconcile_builtin_presets,
    "_reconcile_agent_permission_flags": _reconcile_agent_permission_flags,
    "_bootstrap_default_discovery_intents": _bootstrap_default_discovery_intents,
    "_backfill_knowledge_propagation_v2": _backfill_knowledge_propagation_v2,
    "_bootstrap_quality_assessment_legacy_import_v1": (
        _bootstrap_quality_assessment_legacy_import_v1
    ),
}
