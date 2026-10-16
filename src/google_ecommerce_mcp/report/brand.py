# Derived from OpenGSC (MIT), src/lib/reports/branding.ts; ported to Python
"""The client's brand: display name, accent colour and logo, validated before anything reaches the HTML.

Only the client's brand is ever shown, never the report author's (no "Made with" line exists at all).

* accent: ``^#[0-9A-Fa-f]{6}$`` only; anything else falls back to DEFAULT_ACCENT with an issue recorded.
  Text drawn in the accent colour on white is darkened until it reaches WCAG AA (4.5:1, ``accent_text``);
  strokes, bars, borders and the initials block use ``accent_graphic`` (at least 3:1 on white), so a yellow or
  white accent never gives an invisible chart; text on that block is white or ink, whichever contrasts more.
* logo: a ``data:`` URI only, PNG, JPEG or SVG, strict base64, at most 256 KiB decoded. PNG and JPEG must
  start with their magic bytes. An SVG is refused when it contains a script, an event handler, a
  ``javascript:`` URL, a foreignObject, an entity declaration, a character reference (``&#``), an
  ``<animate>`` / ``<set>`` element, text before its first tag, or any reference that is not a local ``#id``.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ACCENT = "#1F6F5B"
INK = "#14201B"
WHITE = "#FFFFFF"
LOGO_MAX_BYTES = 256 * 1024
GRAPHIC_RATIO = 3.0  # graphics and borders on white (WCAG 1.4.11)
TEXT_RATIO = 4.5     # text on white (WCAG 1.4.3)
NAME_MAX = 120

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
LOGO_RE = re.compile(r"^data:(image/png|image/jpeg|image/svg\+xml);base64,([A-Za-z0-9+/]+={0,2})$")
_SVG_FORBIDDEN = re.compile(
    r"<\s*script|\bon[a-z]+\s*=|javascript\s*:|foreignobject|<!\s*(?:entity|doctype)|@import|<\s*iframe"
    r"|<\s*embed|<\s*object|data\s*:|&#|<\s*(?:animate|set)\b", re.IGNORECASE)
_SVG_REF = re.compile(r"""(?:xlink:)?href\s*=\s*["']\s*([^"']*)["']""", re.IGNORECASE)
_SVG_URL = re.compile(r"""url\(\s*['"]?\s*([^)'"]*)""", re.IGNORECASE)
_MAGIC = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}


@dataclass(frozen=True)
class Brand:
    name: str
    accent: str = DEFAULT_ACCENT
    logo_data_uri: str = ""
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def accent_text(self) -> str:
        """The accent darkened (if needed) to reach 4.5:1 on white, for headings and links."""
        return readable_on_white(self.accent)

    @property
    def accent_graphic(self) -> str:
        """The accent darkened (if needed) to reach 3:1 on white (WCAG non-text contrast): chart strokes, bars,
        borders and the initials block. A yellow or white accent never yields an invisible chart."""
        return readable_on_white(self.accent, GRAPHIC_RATIO)

    @property
    def on_accent(self) -> str:
        """White or ink, whichever contrasts more with the accent_graphic background (initials text)."""
        bg = self.accent_graphic
        return WHITE if contrast(WHITE, bg) >= contrast(INK, bg) else INK

    @property
    def initials(self) -> str:
        words = [w for w in re.split(r"\s+", self.name) if w]
        return "".join(w[0].upper() for w in words[:2]) or "?"


def _channel(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    """WCAG relative luminance of #RRGGBB."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two #RRGGBB colours (1 to 21)."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable_on_white(hex_color: str, ratio: float = 4.5) -> str:
    """Darken ``hex_color`` in 10 % steps until it reaches ``ratio`` against white."""
    color = hex_color.upper()
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    for _ in range(20):
        if contrast(color, WHITE) >= ratio:
            return color
        r, g, b = int(r * 0.9), int(g * 0.9), int(b * 0.9)
        color = f"#{r:02X}{g:02X}{b:02X}"
    return INK


def validate_accent(value: Any) -> tuple[str, str | None]:
    """(accent, issue): the value when it matches #RRGGBB, else DEFAULT_ACCENT and 'accent_invalid'."""
    if value is None or value == "":
        return DEFAULT_ACCENT, None
    if isinstance(value, str) and HEX_RE.match(value.strip()):
        return value.strip().upper(), None
    return DEFAULT_ACCENT, "accent_invalid"


def _svg_safe(raw: bytes) -> bool:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text.lstrip("\ufeff \t\r\n").startswith("<") or "<svg" not in text.lower() or _SVG_FORBIDDEN.search(text):
        return False
    for ref in _SVG_REF.findall(text) + _SVG_URL.findall(text):
        if not ref.strip().startswith("#"):
            return False
    return True


def validate_logo(value: Any) -> tuple[str, str | None]:
    """(data URI, issue): the URI when it passes every check, else "" and the reason."""
    if value is None or value == "":
        return "", None
    if not isinstance(value, str):
        return "", "logo_bad_format"
    value = value.strip()
    if len(value) > LOGO_MAX_BYTES * 4 // 3 + 64:
        return "", "logo_too_large"
    match = LOGO_RE.match(value)
    if not match:
        return "", "logo_bad_format"
    mime, payload = match.groups()
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return "", "logo_bad_base64"
    if len(raw) > LOGO_MAX_BYTES:
        return "", "logo_too_large"
    if mime in _MAGIC and not raw.startswith(_MAGIC[mime]):
        return "", "logo_bad_format"
    if mime == "image/svg+xml" and not _svg_safe(raw):
        return "", "logo_unsafe_svg"
    return value, None


def logo_from_bytes(raw: bytes, filename: str = "") -> str:
    """Build a data URI from a logo file's bytes (PNG, JPEG or SVG, detected by content then extension)."""
    lower = filename.lower()
    if raw.startswith(_MAGIC["image/png"]):
        mime = "image/png"
    elif raw.startswith(_MAGIC["image/jpeg"]):
        mime = "image/jpeg"
    elif lower.endswith(".svg") or b"<svg" in raw[:2048].lower():
        mime = "image/svg+xml"
    else:
        raise ValueError("logo must be a PNG, JPEG or SVG file")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def build_brand(raw: Any, client: Any = "", default_name: str = "Client") -> Brand:
    """Validate ``meta.brand`` into a Brand. Name: brand.name, else meta.client, else ``default_name``."""
    src = raw if isinstance(raw, dict) else {}
    issues: list[str] = []
    name = src.get("name") if isinstance(src.get("name"), str) and src.get("name").strip() else client
    name = name.strip() if isinstance(name, str) and name.strip() else default_name
    if len(name) > NAME_MAX:
        name = name[:NAME_MAX]
        issues.append("name_too_long")
    accent, issue = validate_accent(src.get("accent"))
    if issue:
        issues.append(issue)
    logo, issue = validate_logo(src.get("logo_data_uri"))
    if issue:
        issues.append(issue)
    return Brand(name=name, accent=accent, logo_data_uri=logo, issues=tuple(issues))
