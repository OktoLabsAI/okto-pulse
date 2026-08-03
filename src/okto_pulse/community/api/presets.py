"""Permission preset API endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.import_export import (
    KIND_PRESETS,
    EnvelopeError,
    ExportPresetsCommand,
    ExportPresetsUseCase,
    ImportItemError,
    ImportPresetsCommand,
    ImportPresetsUseCase,
    parse_import_envelope,
    validate_items,
)
from okto_pulse.core.application.use_cases.permission_presets import (
    ClonePermissionPresetCommand,
    ClonePermissionPresetUseCase,
    CreatePermissionPresetCommand,
    CreatePermissionPresetUseCase,
    DeletePermissionPresetCommand,
    DeletePermissionPresetUseCase,
    ListPermissionPresetsCommand,
    ListPermissionPresetsUseCase,
    UpdatePermissionPresetCommand,
    UpdatePermissionPresetUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.ports.permission_policy import (
    PermissionContractViolation,
    validate_permission_flag_values,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PresetCreate(BaseModel):
    name: str
    description: str = ""
    flags: dict | None = None

    @field_validator("flags")
    @classmethod
    def validate_flags(cls, value: dict | None) -> dict | None:
        try:
            validate_permission_flag_values(value)
        except PermissionContractViolation as exc:
            raise ValueError(str(exc)) from exc
        return value


class PresetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    flags: dict | None = None

    @field_validator("flags")
    @classmethod
    def validate_flags(cls, value: dict | None) -> dict | None:
        try:
            validate_permission_flag_values(value)
        except PermissionContractViolation as exc:
            raise ValueError(str(exc)) from exc
        return value


class PresetImportItem(PresetCreate):
    id: str | None = Field(None, min_length=1)
    description: str | None = None
    is_builtin: bool = False
    base_preset_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class PresetResponse(BaseModel):
    id: str
    owner_id: str | None
    name: str
    description: str | None
    is_builtin: bool
    base_preset_id: str | None
    flags: dict | None
    owner_review_required: bool = False
    review_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PresetResponse])
async def list_presets(
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all presets: built-in + custom owned by the user."""
    result = await ListPermissionPresetsUseCase().execute(
        ListPermissionPresetsCommand(),
        actor=RESTAdapterContract.actor(user_id),
        uow=uow,
    )
    return result.presets


@router.post("", response_model=PresetResponse, status_code=status.HTTP_201_CREATED)
async def create_preset(
    data: PresetCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a custom preset."""
    result = await CreatePermissionPresetUseCase().execute(
        CreatePermissionPresetCommand(
            name=data.name,
            description=data.description,
            flags=data.flags,
        ),
        actor=RESTAdapterContract.actor(user_id),
        uow=uow,
    )
    return result.preset


@router.get("/export")
async def export_presets(
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Export every preset visible to the user (built-in + own custom) as a
    schema_version-1 envelope (kind=presets)."""
    return await ExportPresetsUseCase().execute(
        ExportPresetsCommand(),
        actor=RESTAdapterContract.actor(user_id),
        uow=uow,
    )


@router.get("/{preset_id}/export")
async def export_preset(
    preset_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Export one permission preset as a one-item envelope."""
    try:
        return await ExportPresetsUseCase().execute(
            ExportPresetsCommand(preset_id=preset_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Preset not found") from None


@router.post("/import")
async def import_presets(
    envelope: dict,
    dry_run: bool = False,
    replace_existing: bool = False,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Import presets, replacing matching custom ids only after confirmation."""
    try:
        raw_items = parse_import_envelope(envelope, kind=KIND_PRESETS)
    except EnvelopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_envelope", "message": str(exc)},
        )
    parsed, errors = validate_items(raw_items, PresetImportItem.model_validate)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"created": 0, "skipped": [], "errors": errors},
        )
    try:
        result = await ImportPresetsUseCase().execute(
            ImportPresetsCommand(
                items=parsed,
                dry_run=dry_run,
                replace_existing=replace_existing,
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ImportItemError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "created": 0,
                "skipped": [],
                "errors": [{"index": exc.index, "detail": exc.detail}],
            },
        )
    return result.payload(dry_run=dry_run)


@router.post("/{preset_id}/clone", response_model=PresetResponse, status_code=status.HTTP_201_CREATED)
async def clone_preset(
    preset_id: str,
    data: PresetCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Clone an existing preset (built-in or custom) as a new custom preset."""
    try:
        result = await ClonePermissionPresetUseCase().execute(
            ClonePermissionPresetCommand(
                preset_id=preset_id,
                name=data.name,
                description=data.description,
                flags=data.flags,
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Source preset not found")
    return result.preset


@router.put("/{preset_id}", response_model=PresetResponse)
async def update_preset(
    preset_id: str,
    data: PresetUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a custom preset. Built-in presets cannot be modified."""
    try:
        result = await UpdatePermissionPresetUseCase().execute(
            UpdatePermissionPresetCommand(
                preset_id=preset_id,
                name=data.name,
                description=data.description,
                flags=data.flags,
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Preset not found")
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.message)
    return result.preset


@router.delete("/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_preset(
    preset_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a custom preset. Built-in presets cannot be deleted."""
    try:
        await DeletePermissionPresetUseCase().execute(
            DeletePermissionPresetCommand(preset_id=preset_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Preset not found")
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=exc.message)
