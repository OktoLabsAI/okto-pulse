"""Attachment API endpoints."""

import hashlib
import secrets
from email.utils import formatdate

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from starlette.responses import StreamingResponse

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.download_headers import (
    attachment_content_disposition,
)
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.application.use_cases.card_collaboration import (
    AttachmentNotFoundError,
    AttachmentStorageError,
    CardNotFoundError,
    CardNotFoundInBoardError,
    DeleteCardAttachmentCommand,
    DeleteCardAttachmentUseCase,
    GetCardAttachmentCommand,
    GetCardAttachmentUseCase,
    InvalidAttachmentFilenameError,
    UploadCardAttachmentCommand,
    UploadCardAttachmentUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core import StorageObjectStat, get_settings, get_storage_provider
from okto_pulse.core.application.errors import CardOperationError
from okto_pulse.core.models import AttachmentResponse
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()

#: Streamed-download chunk size — mirrors Starlette's file-download chunk size so
#: the provider-backed response chunks identically to the prior filesystem response.
_DOWNLOAD_CHUNK_SIZE = 64 * 1024
#: Bound each application-level upload read.  The multipart parser may spool the
#: request before this endpoint runs; this prevents a second, unbounded heap
#: allocation when the attachment is materialised for the Core use case.
_UPLOAD_CHUNK_SIZE = 64 * 1024


def _attachment_http_error(
    error: InvalidAttachmentFilenameError | AttachmentStorageError,
    *,
    status_code: int,
) -> HTTPException:
    """Return a typed public envelope without adapter or local-path details."""

    return HTTPException(
        status_code=status_code,
        detail={"error": error.code, "message": str(error)},
    )


def _configured_max_upload_size() -> int:
    """Resolve the live Community upload limit without freezing startup state."""

    configured = getattr(
        get_settings(),
        "max_upload_size",
        CommunitySettings.model_fields["max_upload_size"].default,
    )
    return max(0, int(configured))


def _attachment_too_large_http_error(max_upload_size: int) -> HTTPException:
    return HTTPException(
        # Literal keeps compatibility with the supported Starlette 0.35 floor;
        # newer releases renamed and deprecated the original status constant.
        status_code=413,
        detail={
            "error": "attachment_too_large",
            "message": (
                "Attachment exceeds the configured maximum upload size of "
                f"{max_upload_size} bytes"
            ),
        },
    )


async def _read_upload_content(
    file: UploadFile,
    *,
    max_upload_size: int,
) -> bytes:
    """Read at most the configured limit plus one overflow-detection byte."""

    reported_size = getattr(file, "size", None)
    if reported_size is not None and reported_size > max_upload_size:
        raise _attachment_too_large_http_error(max_upload_size)

    content = bytearray()
    while True:
        remaining = max_upload_size - len(content)
        chunk = await file.read(min(_UPLOAD_CHUNK_SIZE, remaining + 1))
        if not chunk:
            return bytes(content)
        if len(chunk) > remaining:
            raise _attachment_too_large_http_error(max_upload_size)
        content.extend(chunk)


def _content_type(media_type: str) -> str:
    """Replicate Starlette's media-type handling: a ``text/*`` type without an
    explicit charset gets ``; charset=utf-8`` appended — matching the header the
    prior filesystem response (which passed ``media_type``) produced."""
    if media_type.startswith("text/") and "charset=" not in media_type.lower():
        return f"{media_type}; charset=utf-8"
    return media_type


def _stat_headers(meta: StorageObjectStat) -> dict[str, str]:
    """Reproduce the prior filesystem response's stat headers EXACTLY: always
    ``Content-Length``; ``Last-Modified`` + ``ETag`` derived from (mtime, size)
    with the same formula when the provider exposes a modification clock. When the
    provider has no mtime the baseline had no such headers either, so they are
    omitted (documented parity, not degradation — TR5)."""
    headers = {"content-length": str(meta.size)}
    if meta.modified_time is not None:
        headers["last-modified"] = formatdate(meta.modified_time, usegmt=True)
        etag_base = f"{meta.modified_time}-{meta.size}"
        headers["etag"] = (
            f'"{hashlib.md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
        )
    return headers


def _parse_byte_ranges(range_header: str | None, size: int):
    """Parse a ``bytes=`` Range header with the prior filesystem response's
    observable contract plus RFC-compatible suffix clamping and mixed-range
    filtering.

    Returns one of:
      ``("single", start, end_exclusive)`` — one satisfiable range -> 206;
      ``("multiple", [(start, end_exclusive), ...])`` — merged+sorted -> 206
        multipart/byteranges (overlaps that merge down to one collapse to single);
      ``("unsatisfiable", size)`` — every range out of bounds -> 416;
      ``("malformed",)`` — bad unit / no numeric range -> 400 (baseline parity);
      ``None`` — no Range header.
    """
    if not range_header:
        return None
    try:
        units, spec = range_header.split("=", 1)
    except ValueError:
        return ("malformed",)
    if units.strip().lower() != "bytes":
        return ("malformed",)

    ranges: list[tuple[int, int]] = []
    saw_numeric_range = False
    saw_unsatisfiable_range = False
    for part in spec.split(","):
        part = part.strip()
        if not part or part == "-" or "-" not in part:
            continue
        start_s, end_s = part.split("-", 1)
        start_s, end_s = start_s.strip(), end_s.strip()
        try:
            if start_s:
                start = int(start_s)
                requested_end = int(end_s) if end_s else None
                saw_numeric_range = True
                if requested_end is not None and requested_end < start:
                    return ("malformed",)
                end = (
                    min(requested_end + 1, size) if requested_end is not None else size
                )
            else:
                suffix_length = int(end_s)
                saw_numeric_range = True
                if suffix_length <= 0:
                    saw_unsatisfiable_range = True
                    continue
                start = max(size - suffix_length, 0)
                end = size
        except ValueError:
            continue

        if not (0 <= start < size):
            saw_unsatisfiable_range = True
            continue
        if start >= end:
            return ("malformed",)
        ranges.append((start, end))

    if not ranges:
        if saw_numeric_range and saw_unsatisfiable_range:
            return ("unsatisfiable", size)
        return ("malformed",)
    if len(ranges) == 1:
        return ("single", ranges[0][0], ranges[0][1])

    ranges.sort()
    merged: list[tuple[int, int]] = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    if len(merged) == 1:
        return ("single", merged[0][0], merged[0][1])
    return ("multiple", merged)


def _multipart_content_length(
    ranges: list[tuple[int, int]], boundary: str, size: int, content_type: str
) -> int:
    """Byte length of the ``multipart/byteranges`` body — IDENTICAL formula to the
    baseline's multipart generator (the constant 49 folds each part's fixed header
    chars plus the trailing CRLF after its data)."""
    static = 49 + len(boundary) + len(content_type) + len(str(size))
    return sum(
        (len(str(start)) + len(str(end - 1)) + static) + (end - start)
        for start, end in ranges
    ) + (4 + len(boundary))


def _multipart_part_header(
    boundary: str, start: int, end: int, size: int, content_type: str
) -> bytes:
    return (
        f"--{boundary}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Range: bytes {start}-{end - 1}/{size}\r\n"
        "\r\n"
    ).encode("latin-1")


async def _multipart_stream(storage, path, ranges, boundary, size, content_type):
    """Stream a ``multipart/byteranges`` body byte-for-byte like the baseline:
    per range a part header + the range's bytes (THROUGH the provider) + CRLF, then
    the closing ``--boundary--``. Each range is read via the provider's offloaded
    chunked stream — the whole file is never materialised (AC6)."""
    for start, end in ranges:
        yield _multipart_part_header(boundary, start, end, size, content_type)
        async for chunk in storage.open_stream(
            path, start=start, end=end, chunk_size=_DOWNLOAD_CHUNK_SIZE
        ):
            yield chunk
        yield b"\r\n"
    yield f"--{boundary}--".encode("latin-1")


@router.post(
    "/{board_id}/{card_id}",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    board_id: str,
    card_id: str,
    file: UploadFile = File(...),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Upload a file attachment to a card."""
    content = await _read_upload_content(
        file,
        max_upload_size=_configured_max_upload_size(),
    )
    try:
        result = await UploadCardAttachmentUseCase().execute(
            UploadCardAttachmentCommand(
                board_id=board_id,
                card_id=card_id,
                filename=file.filename or "unnamed",
                content=content,
                mime_type=file.content_type or "application/octet-stream",
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
    except CardNotFoundInBoardError as exc:
        raise RESTAdapterContract.http_error(
            exc, not_found_detail="Card not found in this board"
        )
    except CardNotFoundError as exc:
        raise RESTAdapterContract.http_error(exc, not_found_detail="Card not found")
    except CardOperationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.to_dict(),
        ) from exc
    except InvalidAttachmentFilenameError as exc:
        raise _attachment_http_error(
            exc,
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from exc
    except AttachmentStorageError as exc:
        raise _attachment_http_error(
            exc,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    return result.attachment


@router.get("/{board_id}/{card_id}/{attachment_id}")
async def download_attachment(
    board_id: str,
    card_id: str,
    attachment_id: str,
    request: Request,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Download an attachment through the registered StorageProvider.

    R02 (FR1/FR2/AC1-AC6): the core NEVER touches a concrete filesystem path. It
    reads metadata + bytes through the provider and reproduces the prior
    filesystem response's observable HTTP contract — Content-Type (incl. text/*
    charset), Content-Disposition, Content-Length, Last-Modified, ETag,
    Accept-Ranges, single-range 206, multi-range 206 multipart/byteranges, 416 and
    malformed 400 — while STREAMING the body in chunks (never materialising the
    whole file by default, AC6). Fails closed (503) when no provider is registered;
    never falls back to a local path (AC3/TR1).
    """
    try:
        result = await GetCardAttachmentUseCase().execute(
            GetCardAttachmentCommand(board_id, card_id, attachment_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
    except CardNotFoundInBoardError as exc:
        raise RESTAdapterContract.http_error(
            exc, not_found_detail="Card not found in this board"
        )
    except AttachmentNotFoundError as exc:
        raise RESTAdapterContract.http_error(
            exc, not_found_detail="Attachment not found"
        )
    attachment = result.attachment

    # FR1/AC3/TR1: serve through the registered provider — fail closed if absent.
    try:
        storage = get_storage_provider()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage provider not configured",
        ) from exc

    # Read-side metadata (size + optional mtime) THROUGH the provider — no path.
    try:
        meta = await storage.stat(attachment.path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attachment file not found",
        ) from exc

    base_headers = {
        "content-type": _content_type(attachment.mime_type),
        "content-disposition": attachment_content_disposition(
            attachment.original_filename
        ),
        "accept-ranges": "bytes",
    }
    stat_headers = _stat_headers(meta)
    etag = stat_headers.get("etag")
    last_modified = stat_headers.get("last-modified")

    # Honour Range only when there is no If-Range or its validator still matches
    # (mirrors the prior filesystem response's If-Range semantics).
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    use_range = range_header is not None and (
        if_range is None or if_range == etag or if_range == last_modified
    )
    parsed = _parse_byte_ranges(range_header, meta.size) if use_range else None

    if parsed is not None and parsed[0] == "unsatisfiable":
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="Requested Range Not Satisfiable",
            headers={"content-range": f"bytes */{meta.size}"},
        )

    if parsed is not None and parsed[0] == "malformed":
        # The baseline filesystem response answers a malformed Range with 400.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed range header.",
        )

    if parsed is not None and parsed[0] == "single":
        _, start, end = parsed
        headers = {**base_headers, **stat_headers}
        headers["content-length"] = str(end - start)
        headers["content-range"] = f"bytes {start}-{end - 1}/{meta.size}"
        return StreamingResponse(
            storage.open_stream(
                attachment.path, start=start, end=end, chunk_size=_DOWNLOAD_CHUNK_SIZE
            ),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
        )

    if parsed is not None and parsed[0] == "multiple":
        _, ranges = parsed
        boundary = secrets.token_hex(13)
        content_type = base_headers["content-type"]
        headers = {**base_headers, **stat_headers}
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(
            _multipart_content_length(ranges, boundary, meta.size, content_type)
        )
        return StreamingResponse(
            _multipart_stream(
                storage, attachment.path, ranges, boundary, meta.size, content_type
            ),
            status_code=status.HTTP_206_PARTIAL_CONTENT,
            headers=headers,
        )

    # Full 200 — no Range header, or an If-Range whose validator no longer matches.
    return StreamingResponse(
        storage.open_stream(attachment.path, chunk_size=_DOWNLOAD_CHUNK_SIZE),
        status_code=status.HTTP_200_OK,
        headers={**base_headers, **stat_headers},
    )


@router.delete(
    "/{board_id}/{card_id}/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_attachment(
    board_id: str,
    card_id: str,
    attachment_id: str,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete an attachment."""
    try:
        await DeleteCardAttachmentUseCase().execute(
            DeleteCardAttachmentCommand(board_id, card_id, attachment_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
    except CardNotFoundInBoardError as exc:
        raise RESTAdapterContract.http_error(
            exc, not_found_detail="Card not found in this board"
        )
    except AttachmentNotFoundError as exc:
        raise RESTAdapterContract.http_error(
            exc, not_found_detail="Attachment not found"
        )
    except CardOperationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.to_dict(),
        ) from exc
    except AttachmentStorageError as exc:
        raise _attachment_http_error(
            exc,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
