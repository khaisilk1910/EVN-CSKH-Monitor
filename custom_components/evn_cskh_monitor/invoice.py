"""Helpers for discovering official invoice attachments in EVN responses.

EVN regional gateways do not use one common schema for invoice files.  This
module keeps all schema-tolerant parsing in small, pure helpers so it can be
unit-tested without Home Assistant or network access.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from html import unescape
from html.parser import HTMLParser
import re
import unicodedata
from typing import Any
from urllib.parse import urljoin

INVOICE_PDF = "pdf"
INVOICE_PNG = "png"

_MONTH_KEYS = {
    "thang",
    "month",
    "thang_hdon",
    "thanghdon",
    "thang_hoa_don",
    "thanghoadon",
    "ky",
    "ky_hdon",
    "kyhdon",
    "ky_hoa_don",
    "kyhoadon",
}
_YEAR_KEYS = {
    "nam",
    "year",
    "nam_hdon",
    "namhdon",
    "nam_hoa_don",
    "namhoadon",
}
_ATTACHMENT_KEY_TOKENS = (
    "pdf",
    "png",
    "file",
    "tep",
    "url",
    "uri",
    "link",
    "download",
    "attachment",
    "dinhkem",
    "dinh_kem",
    "hoadon",
    "hoa_don",
    "invoice",
    "image",
    "hinhanh",
    "hinh_anh",
    "path",
    "src",
    "href",
    "document",
    "chungtu",
    "chung_tu",
    "hddt",
    "viewer",
    "resource",
)
_INVOICE_TEXT_TOKENS = (
    "hóa đơn",
    "hoa don",
    "tiền điện",
    "tien dien",
    "invoice",
    "bill",
)

_EXPLICIT_PERIOD_RE = re.compile(
    r"(?:th[aá]ng|thang|k[yỳ]|ky)\s*[:#-]?\s*(0?[1-9]|1[0-2])"
    r"\s*(?:[/\-.]|\s+n[aă]m\s+)\s*(20\d{2})",
    re.IGNORECASE,
)
_MONTH_YEAR_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])\s*[/_.\-]\s*(20\d{2})(?!\d)")
_YEAR_MONTH_RE = re.compile(r"(?<!\d)(20\d{2})\s*[/_.\-]\s*(0?[1-9]|1[0-2])(?!\d)")
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_\-\s]+$")
_COMPACT_YEAR_MONTH_RE = re.compile(r"^(20\d{2})(0[1-9]|1[0-2])$")
_COMPACT_MONTH_YEAR_RE = re.compile(r"^(0[1-9]|1[0-2])(20\d{2})$")


class _InvoiceLinkParser(HTMLParser):
    """Collect links from a small HTML invoice/viewer response."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key.lower(): value for key, value in attrs if value}
        if tag.lower() in {"a", "iframe", "embed", "img", "source", "object"}:
            value = attrs_dict.get("href") or attrs_dict.get("src") or attrs_dict.get("data")
            if value:
                self.links.append(value)
        if tag.lower() == "meta" and attrs_dict.get("http-equiv", "").lower() == "refresh":
            content = attrs_dict.get("content", "")
            match = re.search(r"url\s*=\s*([^;]+)$", content, re.IGNORECASE)
            if match:
                self.links.append(match.group(1).strip(" \"'"))


def normalize_key(value: Any) -> str:
    """Normalize an EVN field name for tolerant matching, including accents."""
    text = str(value or "").strip().lower().replace("đ", "d")
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )
    return re.sub(r"[^a-z0-9_]", "", text)


def is_invoice_notification(record: Any) -> bool:
    """Return True when a notification clearly refers to an electricity bill."""
    if not isinstance(record, dict):
        return False
    category = str(
        record.get("notificationType")
        or record.get("loai")
        or record.get("type")
        or ""
    ).upper()
    if category.startswith("HOADON") or "INVOICE" in category:
        return True
    text = " ".join(
        str(record.get(key) or "")
        for key in (
            "title",
            "summary",
            "strTieuDe",
            "TieuDe",
            "tieuDe",
            "strNoiDung",
            "noiDung",
            "content",
        )
    ).casefold()
    return any(token in text for token in _INVOICE_TEXT_TOKENS)


def infer_invoice_period(record: Any, *, allow_generic: bool = True) -> tuple[int, int] | None:
    """Infer (month, year) from a bill/notification without inventing a period."""
    if isinstance(record, str):
        return _period_from_text(record, generic=allow_generic)
    if not isinstance(record, (dict, list)):
        return None

    # Prefer explicit month/year fields from the same object.
    if isinstance(record, dict):
        month: int | None = None
        year: int | None = None
        for key, value in record.items():
            norm = normalize_key(key)
            if norm in _MONTH_KEYS and month is None:
                month = _parse_month(value)
                if month is None and isinstance(value, str):
                    parsed = _period_from_text(value, generic=True)
                    if parsed:
                        month, year = parsed
            if norm in _YEAR_KEYS and year is None:
                year = _parse_year(value)
        if month is not None and year is not None:
            return month, year

    # Explicit wording such as "hóa đơn tháng 7/2026" is safe even in a
    # notification which also contains its own creation date.
    for text in _iter_scalar_strings(record):
        match = _EXPLICIT_PERIOD_RE.search(text)
        if match:
            return int(match.group(1)), int(match.group(2))

    # Bill records often store the period in a generic KY/THANG_NAM style field.
    if isinstance(record, dict):
        for key, value in record.items():
            norm = normalize_key(key)
            if any(token in norm for token in ("thang", "month", "ky", "period", "hoadon", "invoice")):
                text = str(value or "").strip()
                parsed = _period_from_text(text, generic=True)
                if parsed:
                    return parsed
                compact = re.sub(r"\D", "", text)
                match = _COMPACT_YEAR_MONTH_RE.fullmatch(compact)
                if match:
                    return int(match.group(2)), int(match.group(1))
                match = _COMPACT_MONTH_YEAR_RE.fullmatch(compact)
                if match:
                    return int(match.group(1)), int(match.group(2))

    # On an actual bill object (not a generic notification), a bare MM/YYYY is a
    # reasonable final fallback. Callers can disable this for notification feeds.
    if allow_generic:
        for text in _iter_scalar_strings(record):
            parsed = _period_from_text(text, generic=True)
            if parsed:
                return parsed
    return None


def iter_attachment_candidates(record: Any) -> Iterator[tuple[str, str]]:
    """Yield unique (kind, value) candidates from a regional EVN payload.

    Kinds are ``url`` and ``base64``. A URL does not need to end in .pdf/.png;
    many EVN gateways expose opaque download endpoints. The downloaded bytes are
    magic-sniffed later before anything is written to disk.
    """
    seen: set[tuple[str, str]] = set()

    def walk(value: Any, key_hint: str = "") -> Iterator[tuple[str, str]]:
        if isinstance(value, dict):
            # Prioritize attachment-looking keys first, but still recurse through
            # every nested container because region schemas change frequently.
            items = list(value.items())
            items.sort(
                key=lambda item: 0 if _attachment_key(str(item[0])) else 1
            )
            for key, child in items:
                yield from walk(child, str(key))
            return
        if isinstance(value, list):
            for child in value:
                yield from walk(child, key_hint)
            return
        if not isinstance(value, str):
            return

        text = unescape(value.strip())
        if not text:
            return
        key_relevant = _attachment_key(key_hint)
        lowered = text.lower()

        # A few EVN gateways wrap attachment metadata as a JSON string inside a
        # normal JSON field. Recurse into small JSON-looking values so an
        # embedded file/link is not missed.
        if len(text) <= 512 * 1024 and text[:1] in {"{", "["}:
            try:
                nested = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                nested = None
            if isinstance(nested, (dict, list)):
                yield from walk(nested, key_hint)

        if lowered.startswith("data:") and ";base64," in lowered[:160]:
            candidate = ("base64", text)
            if candidate not in seen:
                seen.add(candidate)
                yield candidate
            return

        for match in _URL_RE.findall(text):
            candidate = ("url", match.rstrip("),.;"))
            if candidate not in seen:
                seen.add(candidate)
                yield candidate

        if lowered.startswith(("http://", "https://")):
            candidate = ("url", text)
            if candidate not in seen:
                seen.add(candidate)
                yield candidate
            return

        # Relative/opaque download locations are only accepted from attachment
        # fields, preventing arbitrary bill text from being treated as a path.
        if key_relevant and len(text) <= 4096 and not any(ch.isspace() for ch in text) and (
            text.startswith(("/", "./", "../"))
            or ".pdf" in lowered
            or ".png" in lowered
            or "download" in lowered
            or "invoice" in lowered
            or "hoadon" in lowered
            or "hddt" in lowered
            or "?" in text
            or "=" in text
        ):
            candidate = ("url", text)
            if candidate not in seen:
                seen.add(candidate)
                yield candidate

        # A long base64 scalar is safe to inspect regardless of its key name:
        # it is yielded only when decoding produces an actual PDF/PNG signature.
        if len(text) >= 128 and _BASE64_RE.fullmatch(text):
            content = decode_base64_payload(text)
            if detect_invoice_type(content) is not None:
                candidate = ("base64", text)
                if candidate not in seen:
                    seen.add(candidate)
                    yield candidate

    yield from walk(record)


def decode_base64_payload(value: str) -> bytes | None:
    """Decode a plain or data-URL base64 invoice payload."""
    text = value.strip()
    if text.lower().startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    text = re.sub(r"\s+", "", text)
    if not text:
        return None
    # Some APIs emit URL-safe base64 or omit padding.
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text.replace("-", "+").replace("_", "/"), validate=False)
    except (ValueError, TypeError):
        return None


def detect_invoice_type(content: bytes | None) -> str | None:
    """Return pdf/png only when the payload has the expected file signature."""
    if not content:
        return None
    head = content[:32].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if head.startswith(b"%PDF-"):
        return INVOICE_PDF
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return INVOICE_PNG
    return None


def extract_invoice_links_from_html(content: bytes, base_url: str) -> list[str]:
    """Extract likely PDF/PNG/download links from an HTML invoice viewer."""
    if not content or len(content) > 4 * 1024 * 1024:
        return []
    head = content[:512].lstrip().lower()
    if not (head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<body" in head):
        return []
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return []
    parser = _InvoiceLinkParser()
    try:
        parser.feed(text)
    except Exception:
        return []

    # Some EVN viewer pages put the signed file location in a small script
    # instead of an <a>/<iframe>. Collect quoted paths conservatively and apply
    # the same attachment-token filter below.
    parser.links.extend(
        match.group(1)
        for match in re.finditer(
            r"[\"']([^\"'\r\n]*(?:\.pdf|\.png|download|invoice|hoadon|file)[^\"'\r\n]*)[\"']",
            text,
            re.IGNORECASE,
        )
    )

    result: list[str] = []
    seen: set[str] = set()
    for raw in parser.links:
        lower = raw.lower()
        if not any(token in lower for token in (".pdf", ".png", "download", "invoice", "hoadon", "file")):
            continue
        absolute = urljoin(base_url, raw)
        if absolute not in seen:
            seen.add(absolute)
            result.append(absolute)
    return result[:8]


def _attachment_key(key: str) -> bool:
    norm = normalize_key(key)
    return any(token in norm for token in _ATTACHMENT_KEY_TOKENS)


def _parse_month(value: Any) -> int | None:
    try:
        month = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return month if 1 <= month <= 12 else None


def _parse_year(value: Any) -> int | None:
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return year if 2000 <= year <= 2100 else None


def _period_from_text(text: str, *, generic: bool) -> tuple[int, int] | None:
    if not text:
        return None
    match = _EXPLICIT_PERIOD_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    if not generic:
        return None
    match = _MONTH_YEAR_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = _YEAR_MONTH_RE.search(text)
    if match:
        return int(match.group(2)), int(match.group(1))
    return None


def _iter_scalar_strings(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_scalar_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_scalar_strings(child)
    elif isinstance(value, str):
        text = value.strip()
        if text:
            yield text
