"""Community SQLAlchemy adapter for Core permission-preset reconciliation."""

from __future__ import annotations

import json
import hashlib
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import JSON, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from okto_pulse.core.application.use_cases.permission_preset_reconciliation import (
    ReconcilePermissionPresetsResult,
    ReconcilePermissionPresetsUseCase,
)
from okto_pulse.core.domain.permission_presets import (
    PersistedPermissionPreset,
    ReconciliationAction,
    ReconciliationCommand,
)
from okto_pulse.core.ports.permission_preset_reconciliation import (
    PermissionPresetReconciliationRepository,
)
from okto_pulse.core.ports.permission_policy import (
    get_permission_flag,
    ska_permission_introduction_v1,
)
from okto_pulse.community.adapters.sqlalchemy_database import get_session_factory


FailureHook = Callable[[int, ReconciliationCommand], None]


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _audit_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preset_audit_snapshot(
    presets: Sequence[PersistedPermissionPreset],
) -> list[dict[str, Any]]:
    return [
        {
            "id": preset.id,
            "name": preset.name,
            "is_builtin": preset.is_builtin,
            "base": preset.base_preset_name,
            "flags": preset.flags,
        }
        for preset in sorted(presets, key=lambda item: (item.name, item.id))
    ]


class CommunityPermissionPresetReconciliationRepository:
    """Apply a Core plan through one edition-owned SQLAlchemy session."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        before_command: FailureHook | None = None,
    ) -> None:
        self._session = session
        self._before_command = before_command

    async def list_permission_presets(self) -> Sequence[PersistedPermissionPreset]:
        result = await self._session.execute(
            text(
                "SELECT id, owner_id, name, description, is_builtin, "
                "base_preset_id, flags FROM permission_presets ORDER BY name, id"
            )
        )
        rows = list(result.mappings().all())
        names_by_id = {row["id"]: row["name"] for row in rows}
        return tuple(
            PersistedPermissionPreset(
                id=row["id"],
                owner_id=row["owner_id"],
                name=row["name"],
                description=row["description"],
                is_builtin=bool(row["is_builtin"]),
                flags=_json_value(row["flags"]) or {},
                base_preset_name=names_by_id.get(row["base_preset_id"]),
            )
            for row in rows
        )

    async def _base_preset_id(self, name: str | None) -> str | None:
        if name is None:
            return None
        result = await self._session.execute(
            text(
                "SELECT id FROM permission_presets WHERE name = :name "
                "ORDER BY is_builtin DESC LIMIT 1"
            ),
            {"name": name},
        )
        return result.scalar_one_or_none()

    async def apply_permission_preset_commands(
        self,
        commands: Sequence[ReconciliationCommand],
    ) -> None:
        for index, command in enumerate(commands):
            if self._before_command is not None:
                self._before_command(index, command)
            definition = command.definition
            base_preset_id = await self._base_preset_id(definition.base_preset_name)
            if definition.base_preset_name and base_preset_id is None:
                raise ValueError(
                    f"base preset not found: {definition.base_preset_name}"
                )

            if command.action is ReconciliationAction.INSERT:
                await self._session.execute(
                    text(
                        "INSERT INTO permission_presets "
                        "(id, owner_id, name, description, is_builtin, "
                        "base_preset_id, flags) VALUES "
                        "(:id, NULL, :name, :description, :is_builtin, "
                        ":base_preset_id, :flags)"
                    ).bindparams(bindparam("flags", type_=JSON)),
                    {
                        "id": str(uuid.uuid4()),
                        "name": definition.name,
                        "description": definition.description,
                        "is_builtin": True,
                        "base_preset_id": base_preset_id,
                        "flags": definition.flags,
                    },
                )
                continue

            result = await self._session.execute(
                text(
                    "UPDATE permission_presets SET description = :description, "
                    "base_preset_id = :base_preset_id, flags = :flags, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id AND is_builtin = :is_builtin"
                ).bindparams(bindparam("flags", type_=JSON)),
                {
                    "id": command.preset_id,
                    "description": definition.description,
                    "base_preset_id": base_preset_id,
                    "flags": definition.flags,
                    "is_builtin": True,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"built-in preset update lost identity: {command.preset_id}"
                )


async def reconcile_community_permission_presets(
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    before_command: FailureHook | None = None,
) -> ReconcilePermissionPresetsResult:
    """Execute the Core plan atomically in the Community relational adapter."""

    factory = session_factory or get_session_factory()
    async with factory() as session:
        async with session.begin():
            repository = CommunityPermissionPresetReconciliationRepository(
                session,
                before_command=before_command,
            )
            if not isinstance(repository, PermissionPresetReconciliationRepository):
                raise TypeError("Community preset repository violates the Core port")
            before = tuple(await repository.list_permission_presets())
            result = await ReconcilePermissionPresetsUseCase().execute(repository)
            after = tuple(await repository.list_permission_presets())
            manifest = ska_permission_introduction_v1()
            builtins = tuple(item for item in after if item.is_builtin)
            introduced_true_count = sum(
                get_permission_flag(item.flags, leaf) is True
                for item in builtins
                for leaf in manifest.leaves
            )
            introduced_total = len(builtins) * len(manifest.leaves)
            await session.execute(
                text(
                    "INSERT INTO permission_introduction_audit "
                    "(id, manifest_version, phase, classification, subject_id, "
                    "base_preset_id, before_digest, after_digest, "
                    "introduced_true_count, introduced_false_count, "
                    "owner_review_required, mutation_count, details) VALUES "
                    "(:id, :manifest_version, :phase, :classification, NULL, "
                    "NULL, :before_digest, :after_digest, :true_count, "
                    ":false_count, :owner_review_required, :mutation_count, "
                    ":details)"
                ).bindparams(bindparam("details", type_=JSON)),
                {
                    "id": str(uuid.uuid4()),
                    "manifest_version": manifest.version,
                    "phase": "preset_reconciliation",
                    "classification": "builtin_catalog",
                    "before_digest": _audit_digest(
                        _preset_audit_snapshot(before)
                    ),
                    "after_digest": _audit_digest(
                        _preset_audit_snapshot(after)
                    ),
                    "true_count": introduced_true_count,
                    "false_count": introduced_total - introduced_true_count,
                    "owner_review_required": False,
                    "mutation_count": len(result.commands),
                    "details": {
                        "actions": [
                            {
                                "action": command.action.value,
                                "preset": command.definition.name,
                            }
                            for command in result.commands
                        ],
                    },
                },
            )
            return result


__all__ = [
    "CommunityPermissionPresetReconciliationRepository",
    "reconcile_community_permission_presets",
]
