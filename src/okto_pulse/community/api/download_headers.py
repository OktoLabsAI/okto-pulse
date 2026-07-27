"""Shared response-header helpers for downloadable API resources."""

from urllib.parse import quote


def _safe_ascii_filename(filename: str) -> str:
    sanitized = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "-_.")
        else "_"
        for character in filename
    )
    return sanitized or "download"


def attachment_content_disposition(
    filename: str,
    *,
    fallback_filename: str | None = None,
) -> str:
    """Build a Latin-1-safe attachment disposition.

    The default reproduces Starlette ``FileResponse``: a simple ASCII filename
    uses ``filename=`` and any name requiring encoding uses RFC 5987
    ``filename*=``. Callers that need a legacy-client fallback may additionally
    provide an ASCII filename; the UTF-8 name remains authoritative.
    """

    quoted = quote(filename)
    if quoted != filename:
        encoded = f"filename*=utf-8''{quoted}"
        if fallback_filename is not None:
            fallback = _safe_ascii_filename(fallback_filename)
            return f'attachment; filename="{fallback}"; {encoded}'
        return f"attachment; {encoded}"
    return f'attachment; filename="{filename}"'


__all__ = ["attachment_content_disposition"]
