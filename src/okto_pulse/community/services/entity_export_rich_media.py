"""Passive rich-media projection for human-facing entity exports.

This module deliberately does not execute stored HTML or Mermaid.  Mockups are
projected to an inline, allowlisted visual fragment with no controls, document
embedding, navigation or resource loading.  Architecture diagrams are
projected to renderer-owned static SVG; Markdown receives a conservative
Mermaid source without click, style, initialization, or HTML directives.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from html import escape
from html.parser import HTMLParser
import math
import re
from typing import Any


RICH_MEDIA_CSS = """
.rich-media-list{display:grid;gap:12px}.rich-media-item{border:1px solid var(--line);border-radius:12px;background:var(--panel-soft);overflow:hidden}
.rich-media-item>summary{display:flex;align-items:center;justify-content:space-between;gap:12px;cursor:pointer;padding:15px 17px;font-weight:750}
.rich-media-item[open]>summary{border-bottom:1px solid var(--line)}.rich-media-body{padding:16px}.rich-media-description{color:var(--muted);margin:0 0 13px;white-space:pre-wrap}
.rich-media-badge{margin-left:auto;border-radius:999px;background:var(--accent-soft);color:#ddd6fe;padding:3px 9px;font-size:.7rem;text-transform:uppercase;white-space:nowrap}
.mockup-preview{overflow:auto;border:1px solid var(--line);border-radius:10px;background:#eef2f7;padding:18px;color:#172033}.mockup-surface{max-width:980px;min-height:260px;margin:auto;background:#fff;border:1px solid #cbd5e1;border-radius:12px;box-shadow:0 12px 30px rgba(15,23,42,.13);padding:20px;font:14px/1.45 system-ui,sans-serif}.mockup-surface header,.mockup-surface footer,.mockup-surface nav,.mockup-surface main,.mockup-surface section,.mockup-surface aside{display:block;margin:9px 0;padding:11px;border:1px solid #dbe3ee;border-radius:8px}.mockup-surface header,.mockup-surface nav{background:#f8fafc}.mockup-surface h1,.mockup-surface h2,.mockup-surface h3,.mockup-surface h4,.mockup-surface p{margin:.45em 0}.mockup-surface ul,.mockup-surface ol{padding-left:22px}.mockup-surface table{width:100%;border-collapse:collapse}.mockup-surface th,.mockup-surface td{padding:7px;border:1px solid #dbe3ee}.mockup-surface img{display:block;max-width:100%;height:auto}.mockup-control{display:inline-flex;align-items:center;min-height:34px;margin:3px;padding:6px 11px;border:1px solid #94a3b8;border-radius:7px;background:#f8fafc;color:#172033}.mockup-button{background:#6d4aff;color:#fff;border-color:#6d4aff;font-weight:700}.mockup-input{min-width:170px;justify-content:flex-start;color:#64748b}.mockup-link{color:#5b36d6;text-decoration:underline}.mockup-form{display:block;margin:9px 0;padding:11px;border:1px dashed #94a3b8;border-radius:8px}.mockup-visual-block{display:block}.mockup-visual-inline{display:inline}.mockup-empty{color:#64748b;font-style:italic}
.architecture-diagrams{display:grid;gap:16px}.architecture-diagram{display:grid;gap:10px}.architecture-diagram+.architecture-diagram{border-top:1px solid var(--line);padding-top:16px}.architecture-diagram-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px}.architecture-diagram-heading h4{margin:0}.architecture-diagram-format{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em}.architecture-diagram-description{margin:0;color:var(--muted)}
.diagram-figure{margin:0}.diagram-canvas{display:block;width:100%;height:auto;min-height:220px;border:1px solid var(--line);border-radius:10px;background:#f8fafc}
.diagram-source{margin-top:12px}.diagram-source>summary{cursor:pointer;color:var(--muted);font-size:.78rem;font-weight:700}.diagram-source pre,.knowledge-content{max-height:560px;overflow:auto;background:#0b1424;border:1px solid var(--line);border-radius:9px;padding:13px;white-space:pre-wrap;overflow-wrap:anywhere}
.rich-media-fallback{border:1px dashed var(--line);border-radius:9px;padding:16px;color:var(--muted)}
@media(max-width:760px){.mockup-preview{padding:8px}.mockup-surface{padding:12px}}
@media print{.rich-media-item>summary{list-style:none}.rich-media-item>summary::-webkit-details-marker{display:none}.rich-media-item>.rich-media-body{display:block!important}.diagram-source{display:none}.mockup-preview{overflow:visible}.mockup-surface{box-shadow:none}}
""".strip()


_MERMAID_HEADER = re.compile(
    r"^\s*(?:flowchart|graph)\s+(?:TB|TD|BT|RL|LR)\s*$", re.IGNORECASE
)
_MERMAID_UNSAFE_LINE = re.compile(
    r"(?:^\s*%%|\bclick\b|\bhref\b|\bclassDef\b|\bclass\s+|\bstyle\b|"
    r"\blinkStyle\b|\bcallback\b|\bjavascript\s*:|<\s*/?\s*(?:script|iframe|object|embed|style|link|meta)\b)",
    re.IGNORECASE,
)
_MERMAID_EDGE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:--(?:\s*\"([^\"]*)\"\s*)?-->|-->|---|-.->|==>)\s*([A-Za-z][A-Za-z0-9_]*)"
)
_MERMAID_NODE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:\[\s*\"?([^\]\"]+)\"?\s*\]|\(\s*\"?([^\)\"]+)\"?\s*\)|\{\s*\"?([^\}\"]+)\"?\s*\})"
)
_MERMAID_NODE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_]*)\s*(?:"
    r"\[\s*(?:\"([^\"]*)\"|([^\]]+))\s*\]|"
    r"\(\s*(?:\"([^\"]*)\"|([^\)]+))\s*\)|"
    r"\{\s*(?:\"([^\"]*)\"|([^\}]+))\s*\})"
)
_MERMAID_EDGE_WITH_NODES = re.compile(
    r"^\s*(?P<source>[A-Za-z][A-Za-z0-9_]*)"
    r"(?:\s*(?:\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}))?\s*"
    r"(?:--(?:\s*\"(?P<label>[^\"]*)\"\s*)?-->|-->|---|-.->|==>)\s*"
    r"(?P<target>[A-Za-z][A-Za-z0-9_]*)"
    r"(?:\s*(?:\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}))?"
)
_SAFE_ID = re.compile(r"[^A-Za-z0-9_]+")
_MOCKUP_ALLOWED_TAGS = frozenset(
    {
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "code",
        "dd",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "img",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "small",
        "span",
        "strong",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_MOCKUP_VOID_TAGS = frozenset({"br", "hr", "img"})
_MOCKUP_VISUAL_CONTROL_TAGS = {
    "a": ("span", "mockup-control mockup-link"),
    "button": ("span", "mockup-control mockup-button"),
    "form": ("div", "mockup-form"),
    "label": ("span", "mockup-visual-inline"),
    "option": ("span", "mockup-visual-inline"),
    "select": ("span", "mockup-control mockup-input"),
    "textarea": ("span", "mockup-control mockup-input"),
}
_MOCKUP_DROPPED_TAGS = frozenset(
    {
        "audio",
        "base",
        "canvas",
        "embed",
        "iframe",
        "link",
        "math",
        "meta",
        "object",
        "script",
        "style",
        "svg",
        "template",
        "video",
    }
)
_MOCKUP_DROPPED_CONTAINERS = _MOCKUP_DROPPED_TAGS - {"base", "embed", "link", "meta"}
_UNSAFE_CSS = re.compile(
    r"(?:@import|expression\s*\(|url\s*\(|javascript\s*:|behavior\s*:|-moz-binding\s*:)",
    re.IGNORECASE,
)
_SAFE_CSS_PROPERTIES = frozenset(
    {
        "align-items",
        "background",
        "background-color",
        "border",
        "border-color",
        "border-radius",
        "border-style",
        "border-width",
        "color",
        "display",
        "flex-direction",
        "flex-wrap",
        "font-family",
        "font-size",
        "font-style",
        "font-weight",
        "gap",
        "grid-template-columns",
        "height",
        "justify-content",
        "line-height",
        "margin",
        "margin-bottom",
        "margin-left",
        "margin-right",
        "margin-top",
        "max-height",
        "max-width",
        "min-height",
        "min-width",
        "overflow",
        "padding",
        "padding-bottom",
        "padding-left",
        "padding-right",
        "padding-top",
        "text-align",
        "text-decoration",
        "width",
    }
)
_SAFE_CSS_VALUE = re.compile(r"^[a-z0-9#.,()%+\- /]*$", re.IGNORECASE)
_SAFE_RASTER_DATA_URL = re.compile(
    r"data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/]+={0,2})",
    re.IGNORECASE,
)
_MAX_INLINE_RASTER_BYTES = 2 * 1024 * 1024
_OWNED_MOCKUP_CLASSES = frozenset(
    {
        "mockup-control",
        "mockup-button",
        "mockup-form",
        "mockup-input",
        "mockup-link",
        "mockup-visual-block",
        "mockup-visual-inline",
    }
)


def _sanitize_inline_style(value: str) -> str | None:
    if len(value) > 2_048:
        return None
    safe: list[str] = []
    for declaration in value.split(";"):
        property_name, separator, property_value = declaration.partition(":")
        name = property_name.strip().casefold()
        normalized_value = property_value.strip()
        if (
            not separator
            or name not in _SAFE_CSS_PROPERTIES
            or not normalized_value
            or len(normalized_value) > 120
            or "!important" in normalized_value.casefold()
            or _UNSAFE_CSS.search(normalized_value)
            or not _SAFE_CSS_VALUE.fullmatch(normalized_value)
        ):
            continue
        safe.append(f"{name}:{normalized_value}")
    return ";".join(safe) or None


class _MockupHTMLSanitizer(HTMLParser):
    """Project authored markup to a closed, non-interactive visual vocabulary."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._dropped_depth = 0
        self._open_tags: list[tuple[str, str]] = []

    @staticmethod
    def _safe_url(value: str) -> bool:
        if len(value) > (_MAX_INLINE_RASTER_BYTES * 4 // 3) + 128:
            return False
        match = _SAFE_RASTER_DATA_URL.fullmatch(value)
        if match is None:
            return False
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except ValueError:
            return False
        if not decoded or len(decoded) > _MAX_INLINE_RASTER_BYTES:
            return False
        kind = match.group(1).casefold()
        return bool(
            (kind == "png" and decoded.startswith(b"\x89PNG\r\n\x1a\n"))
            or (kind == "jpeg" and decoded.startswith(b"\xff\xd8\xff"))
            or (kind == "gif" and decoded.startswith((b"GIF87a", b"GIF89a")))
            or (
                kind == "webp"
                and len(decoded) >= 12
                and decoded.startswith(b"RIFF")
                and decoded[8:12] == b"WEBP"
            )
        )

    @staticmethod
    def _bounded_integer(value: str, *, maximum: int) -> str | None:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return str(parsed) if 1 <= parsed <= maximum else None

    def _safe_attributes(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        generated_class: str,
    ) -> str:
        values = {name.casefold(): value or "" for name, value in attrs}
        existing_classes = [
            token
            for token in values.get("class", "").split()
            if token in _OWNED_MOCKUP_CLASSES
        ]
        class_tokens = list(dict.fromkeys(existing_classes or generated_class.split()))
        safe: list[str] = [f' class="{" ".join(class_tokens)}"'] if class_tokens else []
        for name in ("alt", "title"):
            if values.get(name):
                safe.append(f' {name}="{escape(values[name][:500], quote=True)}"')
        if tag == "img" and self._safe_url(values.get("src", "")):
            safe.append(f' src="{escape(values["src"], quote=True)}"')
        for name, maximum in (("colspan", 24), ("rowspan", 100)):
            bounded = self._bounded_integer(values.get(name, ""), maximum=maximum)
            if bounded:
                safe.append(f' {name}="{bounded}"')
        style = _sanitize_inline_style(values.get("style", ""))
        if style:
            safe.append(f' style="{escape(style, quote=True)}"')
        return "".join(safe)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if self._dropped_depth:
            if normalized in _MOCKUP_DROPPED_CONTAINERS:
                self._dropped_depth += 1
            return
        if normalized in _MOCKUP_DROPPED_TAGS:
            if normalized in _MOCKUP_DROPPED_CONTAINERS:
                self._dropped_depth = 1
            return
        if normalized == "input":
            values = {name.casefold(): value or "" for name, value in attrs}
            label = values.get("value") or values.get("placeholder") or "Input"
            self.parts.append(
                '<span class="mockup-control mockup-input">'
                + escape(label[:300], quote=False)
                + "</span>"
            )
            return
        output_tag: str
        generated_class: str
        if normalized in _MOCKUP_VISUAL_CONTROL_TAGS:
            output_tag, generated_class = _MOCKUP_VISUAL_CONTROL_TAGS[normalized]
        elif normalized in _MOCKUP_ALLOWED_TAGS:
            output_tag = normalized
            generated_class = (
                "mockup-visual-inline"
                if normalized in {"code", "em", "small", "span", "strong"}
                else "mockup-visual-block"
            )
        else:
            return
        safe_attrs = self._safe_attributes(
            output_tag,
            attrs,
            generated_class=generated_class,
        )
        self.parts.append(f"<{output_tag}{safe_attrs}>")
        if output_tag not in _MOCKUP_VOID_TAGS:
            self._open_tags.append((normalized, output_tag))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if self._dropped_depth:
            if normalized in _MOCKUP_DROPPED_CONTAINERS:
                self._dropped_depth -= 1
            return
        if normalized == "input":
            return
        for index in range(len(self._open_tags) - 1, -1, -1):
            source_tag, output_tag = self._open_tags[index]
            if source_tag != normalized:
                continue
            for _, dangling_output in reversed(self._open_tags[index:]):
                self.parts.append(f"</{dangling_output}>")
            del self._open_tags[index:]
            break

    def handle_data(self, data: str) -> None:
        if self._dropped_depth:
            return
        self.parts.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self._dropped_depth:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self._dropped_depth:
            self.parts.append(f"&#{name};")

    def close(self) -> None:
        super().close()
        for _, output_tag in reversed(self._open_tags):
            self.parts.append(f"</{output_tag}>")
        self._open_tags.clear()


def _sanitize_mockup_html(value: str) -> str:
    sanitizer = _MockupHTMLSanitizer()
    sanitizer.feed(value)
    sanitizer.close()
    return "".join(sanitizer.parts)


def seal_screen_mockups(value: Any) -> Any:
    """Seal stored mockup HTML without dropping its visual representation.

    The result remains JSON-native and deterministic.  The sealed body is an
    already sanitized HTML *fragment*, never a document.  Rendering applies
    the same allowlist again before inserting that fragment inline.
    """

    if not isinstance(value, list):
        return value
    sealed: list[Any] = []
    for raw_item in value:
        if not isinstance(raw_item, Mapping):
            sealed.append(raw_item)
            continue
        item = dict(raw_item)
        html_content = item.get("html_content")
        if (
            isinstance(html_content, Mapping)
            and html_content.get("state") == "unavailable"
        ):
            sealed.append(item)
            continue
        source_size_bytes: int | None = None
        if isinstance(html_content, Mapping) and html_content.get("state") in {
            "passive_inline_ready",
            "sandbox_ready",
        }:
            try:
                html_content = base64.b64decode(
                    str(html_content.get("payload") or ""), validate=True
                ).decode("utf-8")
                source_size_bytes = len(html_content.encode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                html_content = None
        if html_content in (None, ""):
            item["html_content"] = {
                "state": "unavailable",
                "reason": "No valid mockup markup was stored.",
            }
        elif isinstance(html_content, str):
            sanitized = _sanitize_mockup_html(html_content)
            if not sanitized.strip():
                item["html_content"] = {
                    "state": "unavailable",
                    "reason": "The mockup had no passive visual content.",
                }
                sealed.append(item)
                continue
            encoded = sanitized.encode("utf-8")
            item["html_content"] = {
                "state": "passive_inline_ready",
                "media_type": "text/html-fragment",
                "encoding": "base64",
                "payload": base64.b64encode(encoded).decode("ascii"),
                "size_bytes": len(encoded),
                "source_size_bytes": source_size_bytes
                or len(html_content.encode("utf-8")),
                "execution_policy": "passive_inline_allowlist",
            }
        else:
            item["html_content"] = {
                "state": "unavailable",
                "reason": "Stored mockup markup is not textual HTML.",
            }
        sealed.append(item)
    return sealed


def _key(value: object) -> str:
    return str(value or "").strip().casefold().replace("-", "_")


def _html(value: Any) -> str:
    escaped = escape(str(value if value is not None else ""), quote=True)
    escaped = re.sub(
        r"(?i)\b(on[a-z0-9_-]+)=",
        lambda match: match.group(1) + "&#61;",
        escaped,
    )
    return re.sub(r"(?i)\bjavascript:", "javascript&#58;", escaped)


def _md(value: Any) -> str:
    text = str(value if value is not None else "").replace("\\", "\\\\")
    for token in ("`", "*", "_", "[", "]", "<", ">"):
        text = text.replace(token, "\\" + token)
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _md_block(value: Any) -> str:
    """Preserve authored Markdown layout while neutralizing active HTML/URLs."""

    text = (
        str(value if value is not None else "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    text = text.replace("<", "\\<").replace(">", "\\>")
    return re.sub(r"(?i)\bjavascript:", "javascript\\:", text)


def _records(value: Any, names: set[str]) -> list[Mapping[str, Any]]:
    """Read one logical record group from canonical or direct payloads."""

    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        return []
    for envelope in ("embedded", "records"):
        groups = value.get(envelope)
        if not isinstance(groups, Mapping):
            continue
        for name, group in groups.items():
            if _key(name) in names and isinstance(group, list):
                return [item for item in group if isinstance(item, Mapping)]
    for name, group in value.items():
        if _key(name) in names and isinstance(group, list):
            return [item for item in group if isinstance(item, Mapping)]
    if any(
        name in value
        for name in (
            "title",
            "html_content",
            "global_description",
            "entities",
            "content",
        )
    ):
        return [value]
    return []


def _architecture_designs(value: Any) -> list[Mapping[str, Any]]:
    designs = _records(value, {"architecture", "architecture_designs"})
    payload_rows = _records(value, {"architecture_diagram_payloads"})
    if not payload_rows:
        return designs
    by_ref = {
        str(row.get("id")): row
        for row in payload_rows
        if row.get("id") not in (None, "")
    }
    by_diagram = {
        (str(row.get("design_id")), str(row.get("diagram_id"))): row
        for row in payload_rows
        if row.get("design_id") not in (None, "")
        and row.get("diagram_id") not in (None, "")
    }
    enriched: list[Mapping[str, Any]] = []
    for design in designs:
        copy = dict(design)
        diagrams: list[Any] = []
        for raw_diagram in design.get("diagrams") or []:
            if not isinstance(raw_diagram, Mapping):
                diagrams.append(raw_diagram)
                continue
            diagram = dict(raw_diagram)
            payload = by_ref.get(str(diagram.get("adapter_payload_ref")))
            if payload is None:
                payload = by_diagram.get(
                    (str(design.get("id")), str(diagram.get("id")))
                )
            if payload is not None:
                body = payload.get("adapter_payload_json")
                if body is None:
                    body = payload.get("payload_text")
                if body is not None:
                    diagram["adapter_payload"] = body
                if not diagram.get("format") and payload.get("format"):
                    diagram["format"] = payload["format"]
            diagrams.append(diagram)
        copy["diagrams"] = diagrams
        enriched.append(copy)
    return enriched


def _mockup_fragment(envelope: Any) -> str | None:
    if not isinstance(envelope, Mapping) or envelope.get("state") not in {
        "passive_inline_ready",
        "sandbox_ready",
    }:
        return None
    if envelope.get("encoding") != "base64" or not isinstance(
        envelope.get("payload"), str
    ):
        return None
    try:
        raw = base64.b64decode(envelope["payload"], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    sanitized = _sanitize_mockup_html(raw)
    return sanitized if sanitized.strip() else None


def _mockups_html(value: Any) -> str:
    items = _records(value, {"mockups", "screen_mockups"})
    if not items:
        return '<p class="rich-media-fallback">No screen mockups are available.</p>'
    rendered: list[str] = []
    for index, item in enumerate(items, start=1):
        title = item.get("title") or f"Screen mockup {index}"
        badge = item.get("screen_type") or "mockup"
        description = item.get("description")
        fragment = _mockup_fragment(item.get("html_content"))
        if fragment is None:
            preview = '<p class="rich-media-fallback">Visual preview is not available for this mockup.</p>'
        else:
            preview = (
                f'<div class="mockup-preview" role="group" aria-label="{_html(title)}">'
                f'<div class="mockup-surface">{fragment}</div></div>'
            )
        description_html = (
            f'<p class="rich-media-description">{_html(description)}</p>'
            if description
            else ""
        )
        rendered.append(
            '<details class="rich-media-item mockup-item">'
            f'<summary><span>{_html(title)}</span><span class="rich-media-badge">{_html(badge)}</span></summary>'
            f'<div class="rich-media-body">{description_html}{preview}</div></details>'
        )
    return '<div class="rich-media-list">' + "".join(rendered) + "</div>"


def _safe_mermaid_id(value: Any, index: int) -> str:
    normalized = _SAFE_ID.sub("_", str(value or "")).strip("_")
    if not normalized:
        normalized = f"node_{index}"
    if not normalized[0].isalpha():
        normalized = "n_" + normalized
    return normalized[:80]


def _mermaid_label(value: Any) -> str:
    return (
        str(value or "Unnamed")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("&", "#amp;")
        .replace('"', "#quot;")
        .replace("<", "#lt;")
        .replace(">", "#gt;")
        .replace("[", "#91;")
        .replace("]", "#93;")
        .strip()
        or "Unnamed"
    )


def _canonical_ref(value: Any) -> str:
    return str(value or "").strip().casefold()


def _custom_or_top_level(item: Mapping[str, Any], key: str) -> Any:
    if key in item:
        return item.get(key)
    custom_data = item.get("customData")
    return custom_data.get(key) if isinstance(custom_data, Mapping) else None


def _linked_interface_refs(item: Mapping[str, Any]) -> list[str]:
    values: list[Any] = []
    legacy = _custom_or_top_level(item, "linkedInterfaceId")
    if legacy not in (None, ""):
        values.append(legacy)
    multiple = _custom_or_top_level(item, "linkedInterfaceIds")
    if isinstance(multiple, Sequence) and not isinstance(multiple, (str, bytes)):
        values.extend(multiple)
    refs: list[str] = []
    seen: set[str] = set()
    for value in values:
        ref = str(value or "").strip()
        canonical = _canonical_ref(ref)
        if canonical and canonical not in seen:
            seen.add(canonical)
            refs.append(ref)
    return refs


def _unique_mermaid_id(value: Any, index: int, used: set[str]) -> str:
    identifier = _safe_mermaid_id(value, index)
    base = identifier
    suffix = 2
    while identifier in used:
        identifier = f"{base}_{suffix}"
        suffix += 1
    used.add(identifier)
    return identifier


def _float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _excalidraw_mermaid(
    design: Mapping[str, Any], diagram: Mapping[str, Any]
) -> tuple[str, dict[str, tuple[float, float, float, float]]] | None:
    """Project the authoritative Excalidraw topology to passive Mermaid facts.

    Architecture interfaces intentionally do not own their endpoints.  Nodes and
    connections are resolved from ``linkedEntityId`` / ``sourceElementId`` /
    ``targetElementId`` and contracts from ``linkedInterfaceIds``.  The returned
    layout lets the static HTML renderer retain the authored Pulse positions;
    Markdown receives the equivalent renderer-independent Mermaid projection.
    """

    payload = diagram.get("adapter_payload")
    if not isinstance(payload, Mapping):
        return None
    raw_elements = payload.get("elements")
    if not isinstance(raw_elements, Sequence) or isinstance(raw_elements, (str, bytes)):
        return None
    elements = [item for item in raw_elements if isinstance(item, Mapping)]
    if not elements:
        return None

    entities = [
        item for item in design.get("entities") or [] if isinstance(item, Mapping)
    ]
    entity_by_ref: dict[str, Mapping[str, Any]] = {}
    for entity in entities:
        for ref in (entity.get("id"), entity.get("name")):
            canonical = _canonical_ref(ref)
            if canonical:
                entity_by_ref[canonical] = entity

    interfaces = [
        item for item in design.get("interfaces") or [] if isinstance(item, Mapping)
    ]
    interface_by_ref: dict[str, Mapping[str, Any]] = {}
    for interface in interfaces:
        for ref in (interface.get("id"), interface.get("name")):
            canonical = _canonical_ref(ref)
            if canonical:
                interface_by_ref[canonical] = interface

    connections = [
        item
        for item in elements
        if _key(item.get("type")) in {"arrow", "line"}
        or _custom_or_top_level(item, "sourceElementId") not in (None, "")
        or _custom_or_top_level(item, "targetElementId") not in (None, "")
    ]
    referenced_element_ids = {
        _canonical_ref(ref)
        for item in connections
        for ref in (
            _custom_or_top_level(item, "sourceElementId"),
            _custom_or_top_level(item, "targetElementId"),
        )
        if _canonical_ref(ref)
    }

    nodes: list[tuple[str, str]] = []
    layout: dict[str, tuple[float, float, float, float]] = {}
    element_nodes: dict[str, str] = {}
    used: set[str] = set()
    for index, element in enumerate(elements, start=1):
        element_id = str(element.get("id") or "").strip()
        canonical_element_id = _canonical_ref(element_id)
        if not canonical_element_id:
            continue
        linked_entity_ref = _custom_or_top_level(element, "linkedEntityId")
        if (
            linked_entity_ref in (None, "")
            and canonical_element_id not in referenced_element_ids
        ):
            # Decorative/text elements are not architecture nodes.
            continue
        entity = entity_by_ref.get(_canonical_ref(linked_entity_ref))
        label = (
            entity.get("name")
            if entity is not None
            else _custom_or_top_level(element, "text")
            or _custom_or_top_level(element, "label")
            or linked_entity_ref
            or element_id
        )
        preferred_id = (
            entity.get("id")
            if entity is not None and entity.get("id") not in (None, "")
            else linked_entity_ref or element_id
        )
        identifier = _unique_mermaid_id(preferred_id, index, used)
        nodes.append((identifier, str(label)))
        element_nodes[canonical_element_id] = identifier
        width = max(80.0, min(420.0, abs(_float(element.get("width"), 190.0))))
        height = max(48.0, min(220.0, abs(_float(element.get("height"), 82.0))))
        layout[identifier] = (
            _float(element.get("x"), float((index - 1) * 250)),
            _float(element.get("y"), 0.0),
            width,
            height,
        )

    if not nodes:
        return None

    edges: list[tuple[str, str, str]] = []
    for connection in connections:
        source = element_nodes.get(
            _canonical_ref(_custom_or_top_level(connection, "sourceElementId"))
        )
        target = element_nodes.get(
            _canonical_ref(_custom_or_top_level(connection, "targetElementId"))
        )
        if not source or not target:
            continue
        linked_interfaces = [
            interface_by_ref.get(_canonical_ref(ref))
            for ref in _linked_interface_refs(connection)
        ]
        resolved_interfaces = [item for item in linked_interfaces if item is not None]
        labels: list[str] = []
        for interface, fallback_ref in zip(
            linked_interfaces,
            _linked_interface_refs(connection),
            strict=True,
        ):
            candidate = interface.get("name") if interface is not None else fallback_ref
            text = str(candidate or "").strip()
            if text and text not in labels:
                labels.append(text)
        directions = {
            _key(item.get("direction"))
            for item in resolved_interfaces
            if item.get("direction") not in (None, "")
        }
        if directions == {"target_to_source"}:
            source, target = target, source
        edges.append((source, target, " · ".join(labels)))

    x_values = [position[0] for position in layout.values()]
    y_values = [position[1] for position in layout.values()]
    direction = (
        "TB"
        if x_values
        and y_values
        and max(y_values) - min(y_values) > max(x_values) - min(x_values)
        else "LR"
    )
    lines = [
        f"flowchart {direction}",
        *(f'  {identifier}["{_mermaid_label(label)}"]' for identifier, label in nodes),
    ]
    for source, target, label in edges:
        if label:
            lines.append(f'  {source} -- "{_mermaid_label(label)}" --> {target}')
        else:
            lines.append(f"  {source} --> {target}")
    return "\n".join(lines) + "\n", layout


def _semantic_mermaid(design: Mapping[str, Any]) -> str | None:
    entities = [
        item for item in design.get("entities") or [] if isinstance(item, Mapping)
    ]
    if not entities:
        return None
    aliases: dict[str, str] = {}
    nodes: list[tuple[str, str]] = []
    used: set[str] = set()
    for index, entity in enumerate(entities, start=1):
        identifier = _unique_mermaid_id(
            entity.get("id") or entity.get("name"), index, used
        )
        label = str(entity.get("name") or entity.get("id") or f"Entity {index}")
        nodes.append((identifier, label))
        for alias in (entity.get("id"), entity.get("name")):
            if alias not in (None, ""):
                aliases[str(alias)] = identifier
                aliases[str(alias).casefold()] = identifier
    lines = [
        "flowchart LR",
        *(f'  {identifier}["{_mermaid_label(label)}"]' for identifier, label in nodes),
    ]
    for interface in design.get("interfaces") or []:
        if not isinstance(interface, Mapping):
            continue
        participants = interface.get("participants")
        if (
            not isinstance(participants, Sequence)
            or isinstance(participants, (str, bytes))
            or len(participants) < 2
        ):
            continue
        source = aliases.get(str(participants[0])) or aliases.get(
            str(participants[0]).casefold()
        )
        target = aliases.get(str(participants[1])) or aliases.get(
            str(participants[1]).casefold()
        )
        if not source or not target:
            continue
        label = (
            interface.get("name")
            or interface.get("endpoint")
            or interface.get("protocol")
            or "connects"
        )
        lines.append(f'  {source} -- "{_mermaid_label(label)}" --> {target}')
    return "\n".join(lines) + "\n"


def sanitize_mermaid_source(value: Any) -> str | None:
    """Retain a non-interactive flowchart subset suitable for Markdown."""

    if not isinstance(value, str):
        return None
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = normalized.splitlines()
    if not raw_lines or not _MERMAID_HEADER.fullmatch(raw_lines[0].strip()):
        return None
    safe: list[str] = [raw_lines[0].strip()]
    for raw_line in raw_lines[1:]:
        line = raw_line.rstrip()
        stripped = line.strip()
        if (
            not stripped
            or "```" in stripped
            or "@{" in stripped
            or _MERMAID_UNSAFE_LINE.search(line)
        ):
            continue
        if not (
            stripped.casefold() == "end"
            or stripped.casefold().startswith("subgraph ")
            or stripped.casefold().startswith("direction ")
            or _MERMAID_NODE.search(line)
            or _MERMAID_EDGE.search(line)
        ):
            continue
        # Mermaid HTML labels are deliberately converted to text entities.
        line = line.replace("<", "#lt;").replace(">", "#gt;")
        safe.append(line)
    if len(safe) == 1:
        return None
    return "\n".join(safe) + "\n"


def _ordered_diagrams(design: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    diagrams = [
        item for item in design.get("diagrams") or [] if isinstance(item, Mapping)
    ]
    return [
        item
        for _, item in sorted(
            enumerate(diagrams),
            key=lambda pair: (
                _float(pair[1].get("order_index"), float(pair[0])),
                pair[0],
            ),
        )
    ]


def _diagram_projection(
    design: Mapping[str, Any], diagram: Mapping[str, Any]
) -> tuple[str, dict[str, tuple[float, float, float, float]] | None] | None:
    format_key = _key(diagram.get("format"))
    if format_key == "excalidraw_json":
        return _excalidraw_mermaid(design, diagram)
    if format_key != "mermaid":
        return None
    payload = diagram.get("adapter_payload")
    if isinstance(payload, Mapping):
        payload = (
            payload.get("source") or payload.get("content") or payload.get("mermaid")
        )
    safe = sanitize_mermaid_source(payload)
    return (safe, None) if safe else None


def _diagram_source(design: Mapping[str, Any]) -> str | None:
    for diagram in _ordered_diagrams(design):
        projection = _diagram_projection(design, diagram)
        if projection is not None:
            return projection[0]
    return _semantic_mermaid(design)


def _parse_mermaid_graph(
    source: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    labels: dict[str, str] = {}
    edges: list[tuple[str, str, str]] = []
    order: list[str] = []
    for line in source.splitlines()[1:]:
        for match in _MERMAID_NODE_TOKEN.finditer(line):
            identifier = match.group(1)
            label = next(
                (part.strip() for part in match.groups()[1:] if part is not None),
                identifier,
            )
            if identifier not in labels:
                order.append(identifier)
            labels[identifier] = label
        edge_with_nodes = _MERMAID_EDGE_WITH_NODES.search(line)
        edge = edge_with_nodes or _MERMAID_EDGE.search(line)
        if edge:
            if edge_with_nodes:
                source_id = edge_with_nodes.group("source")
                edge_label = edge_with_nodes.group("label")
                target_id = edge_with_nodes.group("target")
            else:
                source_id, edge_label, target_id = edge.groups()
            for identifier in (source_id, target_id):
                if identifier not in labels:
                    labels[identifier] = identifier
                    order.append(identifier)
            edges.append((source_id, target_id, edge_label or ""))
    return [(identifier, labels[identifier]) for identifier in order], edges


def _wrapped_lines(value: str, width: int = 24, maximum: int = 3) -> list[str]:
    words = value.replace("#quot;", '"').replace("#amp;", "&").split()
    if not words:
        return ["Unnamed"]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if current and len(candidate) > width:
            lines.append(current)
            current = word
            if len(lines) == maximum - 1:
                break
        else:
            current = candidate
    if current and len(lines) < maximum:
        lines.append(current)
    consumed = " ".join(lines)
    if len(consumed) < len(" ".join(words)):
        lines[-1] = lines[-1].rstrip("…")[: max(1, width - 1)] + "…"
    return lines


def _normalize_authored_layout(
    nodes: Sequence[tuple[str, str]],
    authored: Mapping[str, tuple[float, float, float, float]],
    *,
    margin: float,
) -> tuple[dict[str, tuple[float, float, float, float]], float, float] | None:
    if not nodes or any(identifier not in authored for identifier, _ in nodes):
        return None
    raw = {identifier: authored[identifier] for identifier, _ in nodes}
    min_x = min(item[0] for item in raw.values())
    min_y = min(item[1] for item in raw.values())
    max_x = max(item[0] + item[2] for item in raw.values())
    max_y = max(item[1] + item[3] for item in raw.values())
    raw_width = max(1.0, max_x - min_x)
    raw_height = max(1.0, max_y - min_y)
    scale = min(1.0, 1120.0 / raw_width, 720.0 / raw_height)
    normalized = {
        identifier: (
            margin + (x - min_x) * scale,
            margin + (y - min_y) * scale,
            width * scale,
            height * scale,
        )
        for identifier, (x, y, width, height) in raw.items()
    }
    return (
        normalized,
        margin * 2 + raw_width * scale,
        margin * 2 + raw_height * scale,
    )


def _box_edge_point(
    box: tuple[float, float, float, float], target: tuple[float, float]
) -> tuple[float, float]:
    x, y, width, height = box
    center_x, center_y = x + width / 2, y + height / 2
    delta_x, delta_y = target[0] - center_x, target[1] - center_y
    if abs(delta_x) < 0.001 and abs(delta_y) < 0.001:
        return center_x, center_y
    x_scale = (width / 2) / abs(delta_x) if abs(delta_x) >= 0.001 else math.inf
    y_scale = (height / 2) / abs(delta_y) if abs(delta_y) >= 0.001 else math.inf
    scale = min(x_scale, y_scale)
    return center_x + delta_x * scale, center_y + delta_y * scale


def _arrow_points(
    x1: float, y1: float, x2: float, y2: float
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]] | None:
    delta_x, delta_y = x2 - x1, y2 - y1
    length = math.hypot(delta_x, delta_y)
    if length < 0.001:
        return None
    unit_x, unit_y = delta_x / length, delta_y / length
    size = 10.0
    base_x, base_y = x2 - unit_x * size * 1.45, y2 - unit_y * size * 1.45
    perp_x, perp_y = -unit_y * size * 0.62, unit_x * size * 0.62
    return (
        (x2, y2),
        (base_x + perp_x, base_y + perp_y),
        (base_x - perp_x, base_y - perp_y),
    )


def mermaid_to_static_svg(
    source: str,
    *,
    title: str,
    authored_layout: Mapping[str, tuple[float, float, float, float]] | None = None,
) -> str | None:
    """Render the safe flowchart subset as deterministic, scriptless SVG."""

    nodes, edges = _parse_mermaid_graph(source)
    if not nodes:
        return None
    default_node_width, default_node_height = 210.0, 76.0
    gap_x, gap_y, margin = 70.0, 80.0, 38.0
    normalized = (
        _normalize_authored_layout(nodes, authored_layout, margin=margin)
        if authored_layout
        else None
    )
    positions: dict[str, tuple[float, float, float, float]] = {}
    if normalized is not None:
        positions, width, height = normalized
    else:
        columns = min(3, max(1, math.ceil(math.sqrt(len(nodes)))))
        rows = math.ceil(len(nodes) / columns)
        width = margin * 2 + columns * default_node_width + max(0, columns - 1) * gap_x
        height = margin * 2 + rows * default_node_height + max(0, rows - 1) * gap_y
        for index, (identifier, _) in enumerate(nodes):
            column, row = index % columns, index // columns
            positions[identifier] = (
                margin + column * (default_node_width + gap_x),
                margin + row * (default_node_height + gap_y),
                default_node_width,
                default_node_height,
            )

    node_parts: list[str] = []
    for identifier, label in nodes:
        x, y, node_width, node_height = positions[identifier]
        wrap_width = max(12, min(34, int(node_width / 8.5)))
        text_lines = _wrapped_lines(label, width=wrap_width)
        start_y = y + node_height / 2 - (len(text_lines) - 1) * 10
        text = "".join(
            f'<tspan x="{x + node_width / 2:.1f}" y="{start_y + offset * 20:.1f}">{_html(line)}</tspan>'
            for offset, line in enumerate(text_lines)
        )
        node_parts.append(
            f'<rect class="diagram-node" x="{x:.1f}" y="{y:.1f}" width="{node_width:.1f}" height="{node_height:.1f}" rx="12" fill="#ffffff" stroke="#7c3aed" stroke-width="2"/>'
            f'<text text-anchor="middle" font-family="system-ui,sans-serif" font-size="14" font-weight="650" fill="#172033">{text}</text>'
        )
    edge_parts: list[str] = []
    for source_id, target_id, label in edges:
        if source_id not in positions or target_id not in positions:
            continue
        source_box = positions[source_id]
        target_box = positions[target_id]
        source_center = (
            source_box[0] + source_box[2] / 2,
            source_box[1] + source_box[3] / 2,
        )
        target_center = (
            target_box[0] + target_box[2] / 2,
            target_box[1] + target_box[3] / 2,
        )
        x1, y1 = _box_edge_point(source_box, target_center)
        x2, y2 = _box_edge_point(target_box, source_center)
        edge_parts.append(
            f'<line class="diagram-edge" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#64748b" stroke-width="2"/>'
        )
        arrow = _arrow_points(x1, y1, x2, y2)
        if arrow is not None:
            points = " ".join(f"{x:.1f},{y:.1f}" for x, y in arrow)
            edge_parts.append(
                f'<polygon class="diagram-arrow" points="{points}" fill="#64748b"/>'
            )
        if label:
            label_lines = _wrapped_lines(label, width=30, maximum=2)
            label_width = min(
                250.0, max(64.0, max(len(line) for line in label_lines) * 6.4 + 14)
            )
            label_height = len(label_lines) * 15.0 + 8.0
            label_x, label_y = (x1 + x2) / 2, (y1 + y2) / 2
            edge_parts.append(
                f'<rect class="diagram-edge-label-bg" x="{label_x - label_width / 2:.1f}" y="{label_y - label_height / 2:.1f}" width="{label_width:.1f}" height="{label_height:.1f}" rx="4" fill="#f8fafc" fill-opacity=".96"/>'
            )
            label_text = "".join(
                f'<tspan x="{label_x:.1f}" y="{label_y - (len(label_lines) - 1) * 7.5 + index * 15:.1f}">{_html(line)}</tspan>'
                for index, line in enumerate(label_lines)
            )
            edge_parts.append(
                f'<text class="diagram-edge-label" text-anchor="middle" font-family="system-ui,sans-serif" font-size="11" fill="#475569">{label_text}</text>'
            )
    return (
        f'<svg class="diagram-canvas" role="img" aria-label="{_html(title)}" viewBox="0 0 {width:.1f} {height:.1f}">'
        f"<title>{_html(title)}</title>"
        + "".join(edge_parts)
        + "".join(node_parts)
        + "</svg>"
    )


def _architecture_diagram_html(
    design: Mapping[str, Any],
    diagram: Mapping[str, Any] | None,
    *,
    index: int,
    design_title: str,
) -> str:
    if diagram is None:
        diagram_title = "Semantic architecture overview"
        diagram_description = None
        format_key = "semantic_projection"
        source = _semantic_mermaid(design)
        authored_layout = None
    else:
        diagram_title = str(diagram.get("title") or f"Architecture diagram {index}")
        diagram_description = diagram.get("description")
        format_key = _key(diagram.get("format")) or "unknown"
        projection = _diagram_projection(design, diagram)
        source, authored_layout = projection if projection is not None else (None, None)
    accessible_title = f"{design_title} — {diagram_title}"
    svg = (
        mermaid_to_static_svg(
            source,
            title=accessible_title,
            authored_layout=authored_layout,
        )
        if source
        else None
    )
    visual = (
        f'<figure class="diagram-figure">{svg}</figure>'
        if svg
        else '<p class="rich-media-fallback">A static preview could not be generated for this diagram.</p>'
    )
    source_html = (
        '<details class="diagram-source">'
        f"<summary>{'Mermaid source' if format_key == 'mermaid' else 'Mermaid projection'}</summary>"
        f"<pre><code>{_html(source)}</code></pre></details>"
        if source
        else ""
    )
    description_html = (
        f'<p class="architecture-diagram-description">{_html(diagram_description)}</p>'
        if diagram_description
        else ""
    )
    format_label = format_key.replace("_", " ")
    return (
        '<section class="architecture-diagram">'
        '<header class="architecture-diagram-heading">'
        f"<h4>{_html(diagram_title)}</h4>"
        f'<span class="architecture-diagram-format">{_html(format_label)}</span>'
        "</header>"
        f"{description_html}{visual}{source_html}</section>"
    )


def _architecture_html(value: Any) -> str:
    designs = _architecture_designs(value)
    if not designs:
        return (
            '<p class="rich-media-fallback">No architecture designs are available.</p>'
        )
    rendered: list[str] = []
    for index, design in enumerate(designs, start=1):
        title = str(design.get("title") or f"Architecture design {index}")
        description = design.get("global_description") or design.get("description")
        diagrams = _ordered_diagrams(design)
        diagrams_html = "".join(
            _architecture_diagram_html(
                design,
                diagram,
                index=diagram_index,
                design_title=title,
            )
            for diagram_index, diagram in enumerate(diagrams, start=1)
        )
        if not diagrams:
            diagrams_html = _architecture_diagram_html(
                design,
                None,
                index=1,
                design_title=title,
            )
        description_html = (
            f'<p class="rich-media-description">{_html(description)}</p>'
            if description
            else ""
        )
        rendered.append(
            '<details class="rich-media-item architecture-item">'
            f'<summary><span>{_html(title)}</span><span class="rich-media-badge">Architecture</span></summary>'
            f'<div class="rich-media-body">{description_html}<div class="architecture-diagrams">{diagrams_html}</div></div></details>'
        )
    return '<div class="rich-media-list">' + "".join(rendered) + "</div>"


def _knowledge_html(value: Any) -> str:
    items = _records(
        value,
        {
            "knowledge",
            "knowledge_bases",
            "ideation_knowledge_bases",
            "refinement_knowledge_bases",
            "spec_knowledge_bases",
        },
    )
    if not items:
        return (
            '<p class="rich-media-fallback">No knowledge-base items are available.</p>'
        )
    rendered: list[str] = []
    for index, item in enumerate(items, start=1):
        title = item.get("title") or f"Knowledge-base item {index}"
        description = item.get("description")
        content = item.get("content")
        description_html = (
            f'<p class="rich-media-description">{_html(description)}</p>'
            if description
            else ""
        )
        content_html = (
            f'<pre class="knowledge-content">{_html(content)}</pre>'
            if content not in (None, "")
            else '<p class="rich-media-fallback">No content is available.</p>'
        )
        rendered.append(
            '<details class="rich-media-item knowledge-item">'
            f'<summary><span>{_html(title)}</span><span class="rich-media-badge">Knowledge</span></summary>'
            f'<div class="rich-media-body">{description_html}{content_html}</div></details>'
        )
    return '<div class="rich-media-list">' + "".join(rendered) + "</div>"


def render_rich_media_html(value: Any, *, field_key: str) -> str | None:
    """Return a complete passive projection for a recognized rich-media group."""

    key = _key(field_key)
    if key in {"mockups", "screen_mockups"}:
        return _mockups_html(value)
    if key in {"architecture", "architecture_designs"}:
        return _architecture_html(value)
    if key in {"knowledge", "knowledge_bases"}:
        return _knowledge_html(value)
    return None


def _mockups_markdown(value: Any) -> list[str]:
    items = _records(value, {"mockups", "screen_mockups"})
    if not items:
        return ["_No screen mockups are available._"]
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        title = item.get("title") or f"Screen mockup {index}"
        badge = item.get("screen_type") or "mockup"
        lines.extend(
            ["<details>", f"<summary>{_md(title)} · {_md(badge)}</summary>", ""]
        )
        if item.get("description"):
            lines.extend([_md(item["description"]), ""])
        if _mockup_fragment(item.get("html_content")) is not None:
            lines.append(
                "_A passive inline visual preview is available in the HTML export._"
            )
        else:
            lines.append("_Visual preview unavailable._")
        lines.extend(["", "</details>", ""])
    return lines


def _architecture_markdown(value: Any) -> list[str]:
    designs = _architecture_designs(value)
    if not designs:
        return ["_No architecture designs are available._"]
    lines: list[str] = []
    for index, design in enumerate(designs, start=1):
        title = design.get("title") or f"Architecture design {index}"
        lines.extend(["<details>", f"<summary>{_md(title)}</summary>", ""])
        description = design.get("global_description") or design.get("description")
        if description:
            lines.extend([_md(description), ""])
        diagrams = _ordered_diagrams(design)
        if not diagrams:
            source = _semantic_mermaid(design)
            if source:
                lines.extend(
                    [
                        "#### Semantic architecture overview",
                        "",
                        "```mermaid",
                        source.rstrip(),
                        "```",
                        "",
                    ]
                )
            else:
                lines.extend(["_Diagram preview unavailable._", ""])
        for diagram_index, diagram in enumerate(diagrams, start=1):
            diagram_title = (
                diagram.get("title") or f"Architecture diagram {diagram_index}"
            )
            lines.extend([f"#### {_md(diagram_title)}", ""])
            if diagram.get("description"):
                lines.extend([_md(diagram["description"]), ""])
            projection = _diagram_projection(design, diagram)
            if projection is not None:
                lines.extend(["```mermaid", projection[0].rstrip(), "```", ""])
            else:
                format_label = str(diagram.get("format") or "stored format")
                lines.extend(
                    [f"_Static preview unavailable for {_md(format_label)}._", ""]
                )
        lines.extend(["</details>", ""])
    return lines


def _knowledge_markdown(value: Any) -> list[str]:
    items = _records(
        value,
        {
            "knowledge",
            "knowledge_bases",
            "ideation_knowledge_bases",
            "refinement_knowledge_bases",
            "spec_knowledge_bases",
        },
    )
    if not items:
        return ["_No knowledge-base items are available._"]
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        title = item.get("title") or f"Knowledge-base item {index}"
        lines.extend(["<details>", f"<summary>{_md(title)}</summary>", ""])
        if item.get("description"):
            lines.extend([_md(item["description"]), ""])
        if item.get("content"):
            lines.extend([_md_block(item["content"]), ""])
        lines.extend(["</details>", ""])
    return lines


def render_rich_media_markdown(value: Any, *, field_key: str) -> list[str] | None:
    """Return Markdown parallel to the passive HTML rich-media projection."""

    key = _key(field_key)
    if key in {"mockups", "screen_mockups"}:
        return _mockups_markdown(value)
    if key in {"architecture", "architecture_designs"}:
        return _architecture_markdown(value)
    if key in {"knowledge", "knowledge_bases"}:
        return _knowledge_markdown(value)
    return None


__all__ = [
    "RICH_MEDIA_CSS",
    "mermaid_to_static_svg",
    "render_rich_media_html",
    "render_rich_media_markdown",
    "sanitize_mermaid_source",
    "seal_screen_mockups",
]
