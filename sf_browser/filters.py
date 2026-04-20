"""Jinja2 template filters for the SF Data Browser."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from markupsafe import Markup, escape

_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)


def format_value(value: Any, sf_type: str | None = None, *, scale: int | None = None) -> str:
    """Render a single cell value as a display string.

    ``scale`` is the SF field decimal scale (from describe). When 0, we
    render integers without the trailing ``.00``.
    """
    if value is None or value == "":
        return ""
    if sf_type == "boolean":
        return "true" if int(value) else "false"
    if sf_type in ("datetime", "date"):
        return _format_datetime(str(value))
    if sf_type in ("double", "currency", "percent", "int"):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return str(value)
        if sf_type == "int" or scale == 0:
            return f"{int(num):,}"
        return f"{num:,.2f}"
    text = str(value)
    # Truncation is handled by render_cell for textarea; plain format_value
    # still truncates for table cell context.
    if len(text) > 200:
        return text[:200] + "..."
    return text


def _format_datetime(raw: str) -> str:
    """Parse a datetime value — handles both epoch-ms strings and ISO 8601."""
    # Epoch milliseconds: pure digits, typically 13 chars (e.g. "1534515327000").
    stripped = raw.strip()
    if stripped.isdigit() and len(stripped) >= 10:
        try:
            epoch_s = int(stripped) / 1000.0
            dt = datetime.fromtimestamp(epoch_s, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            pass

    # ISO 8601 variants.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(stripped, fmt).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            continue
    return raw


def salesforce_id(value: Any) -> bool:
    """Quick heuristic: 15/18-char alphanumeric string -> SF Id."""
    if not isinstance(value, str):
        return False
    if len(value) not in (15, 18):
        return False
    return value.replace("_", "").isalnum()


def render_cell(
    value: Any,
    sf_type: str | None,
    reference_to: str | None,
    url_for_record,
    *,
    prefix_map: dict[str, str] | None = None,
    table_names: dict[str, str] | None = None,
    expand_text: bool = False,
) -> Markup:
    """Render a cell; reference fields become links to the target record.

    When *prefix_map* is provided, polymorphic references are resolved via
    key_prefix instead of blindly picking the first declared target.

    When *table_names* is provided (sf_id -> display_name), the resolved
    display name is shown alongside the ID link in table views.

    When *expand_text* is True, textarea fields render with a full-text
    expand button instead of hard truncation.
    """
    if value in (None, ""):
        return Markup("<span class='text-slate-400'>&ndash;</span>")

    if sf_type == "reference" and salesforce_id(value):
        target = None
        # Resolve via key_prefix for polymorphic references.
        if prefix_map:
            target = prefix_map.get(str(value)[:3])
        if not target and reference_to:
            target = reference_to.split(",")[0].strip()
        if target:
            href = url_for_record(target, str(value))
            resolved_name = ""
            if table_names and str(value) in table_names:
                resolved_name = table_names[str(value)]
            if resolved_name:
                return Markup(
                    f'<a class="text-blue-700 hover:underline" href="{escape(href)}">'
                    f'{escape(resolved_name)}</a> '
                    f'<code class="text-[10px] text-slate-400">{escape(str(value)[:8])}…</code>'
                )
            label = escape(str(value))
            return Markup(
                f'<a class="text-blue-700 hover:underline" href="{escape(href)}">'
                f'<code class="text-xs">{label}</code></a>'
            )

    if sf_type == "url" and isinstance(value, str) and value.startswith(("http://", "https://")):
        return Markup(
            f'<a class="text-blue-700 hover:underline" href="{escape(value)}" '
            f'target="_blank" rel="noopener">{escape(value)}</a>'
        )

    if sf_type == "email" and isinstance(value, str) and "@" in value:
        return Markup(
            f'<a class="text-blue-700 hover:underline" href="mailto:{escape(value)}">'
            f'{escape(value)}</a>'
        )

    # Expandable textarea for record detail view.
    if expand_text and sf_type == "textarea" and isinstance(value, str) and len(value) > 200:
        import hashlib
        uid = hashlib.md5(value[:50].encode()).hexdigest()[:8]
        short = escape(value[:200])
        full = escape(value)
        return Markup(
            f'<div>'
            f'<span id="short-{uid}">{short}… '
            f'<button onclick="document.getElementById(\'full-{uid}\').classList.remove(\'hidden\');'
            f'document.getElementById(\'short-{uid}\').classList.add(\'hidden\')" '
            f'class="text-blue-600 text-xs hover:underline">show all ({len(value):,} chars)</button></span>'
            f'<div id="full-{uid}" class="hidden whitespace-pre-wrap text-sm max-h-96 overflow-auto '
            f'bg-slate-50 rounded p-2 mt-1">{full}'
            f'<div class="mt-1"><button onclick="document.getElementById(\'short-{uid}\').classList.remove(\'hidden\');'
            f'document.getElementById(\'full-{uid}\').classList.add(\'hidden\')" '
            f'class="text-blue-600 text-xs hover:underline">collapse</button></div></div>'
            f'</div>'
        )

    return Markup(escape(format_value(value, sf_type)))


# --------------------------------------------------------------------------- #
# Keboola-view renderers
# --------------------------------------------------------------------------- #


def _linkify(text: str) -> Markup:
    """Escape text and turn URLs into anchor tags. Preserves newlines."""
    out_parts: list[str] = []
    last = 0
    for match in _URL_RE.finditer(text):
        out_parts.append(str(escape(text[last:match.start()])))
        url = match.group(0)
        out_parts.append(
            f'<a class="text-blue-700 hover:underline" '
            f'href="{escape(url)}" target="_blank" rel="noopener">{escape(url)}</a>'
        )
        last = match.end()
    out_parts.append(str(escape(text[last:])))
    html = "".join(out_parts).replace("\n", "<br>")
    return Markup(html)


def render_keboola(
    value: Any,
    field: dict[str, Any],
    url_for_record,
    *,
    prefix_map: dict[str, str] | None = None,
    name_cache: dict[str, tuple[str, str]] | None = None,
) -> Markup:
    """Keboola-view cell renderer — stricter, prettier than raw view.

    Differences vs. ``render_cell``:
    - Reference fields show resolved name only; ID is a hover tooltip.
    - Booleans render as ✓ (true) / — (false).
    - Integers (scale=0) render without ``.00``.
    - Textarea fields: full text with URL linkification, no truncate button.
    - Empty values render as a dim em dash.
    """
    if value in (None, ""):
        return Markup("<span class='text-slate-300'>—</span>")

    sf_type = field.get("sf_type")
    field_name = field.get("field_name", "")

    # Reference -> link with resolved name (fallback to ID).
    if sf_type == "reference" and salesforce_id(value):
        target = None
        if prefix_map:
            target = prefix_map.get(str(value)[:3])
        if not target and field.get("reference_to"):
            target = field["reference_to"].split(",")[0].strip()
        resolved_name = ""
        if name_cache and field_name in name_cache:
            _obj, resolved_name = name_cache[field_name]
        if target:
            href = url_for_record(target, str(value))
            label = escape(resolved_name) if resolved_name else escape(str(value))
            title = f"{target} · {value}"
            return Markup(
                f'<a class="text-blue-700 hover:underline font-medium" '
                f'href="{escape(href)}" title="{escape(title)}">{label}</a>'
            )
        return Markup(f'<code class="text-xs">{escape(str(value))}</code>')

    # Boolean -> checkmark or em dash.
    if sf_type == "boolean":
        try:
            is_true = bool(int(value))
        except (TypeError, ValueError):
            is_true = str(value).lower() in ("true", "1", "yes")
        if is_true:
            return Markup('<span class="text-emerald-600" aria-label="true">✓</span>')
        return Markup('<span class="text-slate-300" aria-label="false">—</span>')

    # URL / email / phone keep the behaviour from render_cell.
    if sf_type == "url" and isinstance(value, str) and value.startswith(("http://", "https://")):
        return Markup(
            f'<a class="text-blue-700 hover:underline" href="{escape(value)}" '
            f'target="_blank" rel="noopener">{escape(value)}</a>'
        )
    if sf_type == "email" and isinstance(value, str) and "@" in value:
        return Markup(
            f'<a class="text-blue-700 hover:underline" href="mailto:{escape(value)}">'
            f'{escape(value)}</a>'
        )
    if sf_type == "phone" and isinstance(value, str):
        return Markup(
            f'<a class="text-blue-700 hover:underline" '
            f'href="tel:{escape(value.replace(" ", ""))}">{escape(value)}</a>'
        )

    # Datetime / date / numeric -> reuse format_value.
    if sf_type in ("datetime", "date", "double", "currency", "percent", "int"):
        return Markup(escape(format_value(value, sf_type)))

    # Textarea / long text -> linkify URLs, preserve newlines, no truncation.
    if sf_type == "textarea" and isinstance(value, str):
        return Markup(
            f'<div class="whitespace-pre-wrap text-sm leading-relaxed">'
            f'{_linkify(value)}</div>'
        )

    # Plain string / picklist -> escape, linkify any bare URL it may contain.
    text = str(value)
    if "http://" in text or "https://" in text:
        return _linkify(text)
    return Markup(escape(text))
